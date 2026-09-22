#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tests/test_siglip.py — SigLIP 双塔（Phase 7 T7-1：从板端实验树搬进仓库）

跑法:
    python tests/test_siglip.py

分层
    纯逻辑层（不依赖 NPU、不依赖 tokenizers、不依赖模型文件）：配置、分词封装、
    相似度/排序/缓存 —— 通过**注入**假 runtime / 假 tokenizer backend 实现，
    宿主机上也能全绿。
    板端集成层（需要模型 + tokenizer + NPU）：形状、确定性、判别力回归 ——
    缺东西时自动 skip（`python3 tests/test_siglip.py` 在板端会真的跑）。

⚠ 这个文件是"搬运后的守门人"
    实现在板端实验树 `sig/` 里被实测验证过；搬进仓库时**行为不能变**。
    所以除了逻辑测试，这里还钉住:
      · 模型契约常量（256/64/pad=1/uint8/768/cosine）—— 它们是"改错不报错只是变笨"的那类值
      · `validate()` 拦得住自相矛盾的配置
      · 板端那侧另有一条**对齐测试**（`tests/board/siglip_align.py`）：同一张图，
        仓库版与 sig/ 原树的 embedding 余弦 ≥ 0.999
"""

import os
import sys
import unittest
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from agent.vision.siglip import (  # noqa: E402
    EMBED_DIM,
    IMAGE_SIZE,
    TEXT_LEN,
    SiglipConfig,
    SiglipConfigError,
    SiglipInputError,
    SiglipModel,
    SiglipTokenizer,
    SiglipTokenizeError,
)
from agent.vision.siglip import config as sig_config  # noqa: E402

try:
    import numpy as np
except ImportError:                                   # pragma: no cover
    np = None

#: 这台机器有没有 numpy。⚠ **开发机（PC）上没有**（仓库里的 conda/ 也没有），
#: 所以走 numpy 的用例在 PC 上 skip、在板端真跑 —— 与 tests/test_vision.py 同一口径。
#: 配置层与分词层的**纯逻辑**部分不依赖 numpy，两边都能跑。
needs_numpy = unittest.skipIf(np is None, "需要 numpy（开发机上没有，板端才有）")


# --------------------------------------------------------------- 测试替身 ---
class _FakeEncoding(object):
    def __init__(self, ids):
        self.ids = ids


class FakeTokenizerBackend(object):
    """极简分词替身：按空格切，方便断言 padding/截断。"""

    def __init__(self, table=None):
        self.table = table or {}

    def encode(self, text):
        return _FakeEncoding(self.table.get(text, list(range(10, 10 + len(text.split())))))

    def decode(self, ids, skip_special_tokens=False):
        return " ".join(str(i) for i in ids)

    def get_vocab_size(self):
        return 32000


class FakeRuntime(object):
    """假运行时：按可预测规则造 embedding，便于断言数学与调用次数。"""

    def __init__(self, img_vec=None, txt_vec=None, embed_dim=EMBED_DIM):
        self.embed_dim = embed_dim
        self.img_vec = np.asarray(img_vec if img_vec is not None else np.ones(embed_dim),
                                  dtype=np.float32)
        self.txt_vec = np.asarray(txt_vec if txt_vec is not None else np.ones(embed_dim),
                                  dtype=np.float32)
        self.calls = 0
        self.closed = False
        self.seen_images = []
        self.seen_ids = []

    def run(self, image, input_ids):
        self.calls += 1
        self.seen_images.append(np.asarray(image).copy())
        self.seen_ids.append(np.asarray(input_ids).copy())
        return self.img_vec.reshape(1, -1), self.txt_vec.reshape(1, -1)

    def close(self):
        self.closed = True


def make_tokenizer(text_len=TEXT_LEN, pad_id=1, table=None, truncate=True):
    return SiglipTokenizer("/tmp/none.json", text_len=text_len, pad_id=pad_id,
                           truncate=truncate, backend=FakeTokenizerBackend(table))


def _cfg(**over):
    """构造一份合法配置（**不读磁盘**；模型/tokenizer 只在真正推理时才需要）。"""
    section = {"model_path": "/tmp/model.rknn", "tokenizer_path": "/tmp/tokenizer.json"}
    section.update(over)
    return SiglipConfig.from_config(section)


# ------------------------------------------------------------------ 配置 ---
class TestConfig(unittest.TestCase):
    def test_defaults_are_the_board_paths(self):
        # 不配 `vision:` 段也能跑：默认就是板端布局
        c = SiglipConfig.from_config(None)
        self.assertEqual(c.model_path, sig_config.DEFAULT_MODEL_PATH)
        self.assertEqual(c.tokenizer_path, sig_config.DEFAULT_TOKENIZER_PATH)
        self.assertEqual(c.runtime_lib, sig_config.DEFAULT_RUNTIME_LIB)
        self.assertFalse(c.verbose)
        self.assertEqual(c.warmup_runs, 1)
        self.assertEqual(c.source, "config.yaml#vision")

    def test_configurable_keys_are_read(self):
        c = SiglipConfig.from_config({
            "model_path": "/data/sig.rknn", "tokenizer_path": "/data/tok.json",
            "runtime_lib": "/opt/librknnrt.so", "verbose": True, "warmup_runs": 4,
        })
        self.assertEqual(c.model_path, "/data/sig.rknn")
        self.assertEqual(c.tokenizer_path, "/data/tok.json")
        self.assertEqual(c.runtime_lib, "/opt/librknnrt.so")
        self.assertTrue(c.verbose)
        self.assertEqual(c.warmup_runs, 4)

    def test_model_contract_constants_are_frozen(self):
        # 这些值"改错不报错、只是变笨"——所以钉死在测试里，改它们必须是有意识的动作
        c = SiglipConfig.from_config(None)
        self.assertEqual((c.image_width, c.image_height), (IMAGE_SIZE, IMAGE_SIZE))
        self.assertEqual((c.image_width, c.image_height, c.image_channel), (256, 256, 3))
        self.assertEqual(c.image_layout, "nhwc")
        self.assertEqual(c.image_dtype, "uint8")       # 不是 0-1 浮点
        self.assertEqual((c.text_len, c.text_pad_id), (TEXT_LEN, 1))
        self.assertEqual(c.text_input_dtype, "int64")
        self.assertEqual(c.embed_dim, EMBED_DIM)
        self.assertEqual(c.similarity, "cosine")
        self.assertEqual(c.npu_core, "auto")
        self.assertFalse(c.normalize, "归一化已烧进图，Python 侧必须关")
        self.assertTrue(c.l2_normalize)

    def test_bad_bool_and_int_are_rejected(self):
        with self.assertRaises(SiglipConfigError) as ctx:
            _cfg(verbose="maybe")
        self.assertIn("verbose", str(ctx.exception))
        for bad in ("abc", True, -1):
            with self.assertRaises(SiglipConfigError):
                SiglipConfig.from_config({"warmup_runs": bad})

    def test_blank_paths_fall_back_to_defaults(self):
        c = SiglipConfig.from_config({"model_path": "  ", "tokenizer_path": None})
        self.assertEqual(c.model_path, sig_config.DEFAULT_MODEL_PATH)
        self.assertEqual(c.tokenizer_path, sig_config.DEFAULT_TOKENIZER_PATH)

    def test_validate_rejects_self_contradictions(self):
        """validate() 拦的是"有人手改常量改出自相矛盾的一组值"。"""
        base = dict(
            image_layout="nhwc", image_channel=3, image_dtype="uint8", text_len=64,
            text_pad_id=1, embed_dim=768, similarity="cosine", npu_core="auto",
            normalize=False,
        )

        def broken(**over):
            kw = dict(base)
            kw.update(over)
            return SiglipConfig(**kw)

        for bad in (dict(image_layout="nchw"), dict(image_channel=1),
                    dict(image_dtype="float32"), dict(text_len=0), dict(text_pad_id=-1),
                    dict(embed_dim=0), dict(similarity="sigmoid"),
                    dict(npu_core="0_1_2_3"), dict(normalize=True)):
            with self.assertRaises(SiglipConfigError):
                broken(**bad).validate()

    def test_describe_mentions_the_contract(self):
        text = _cfg().describe()
        for piece in ("256x256x3", "nhwc", "uint8", "text_len=64", "pad=1", "out=768"):
            self.assertIn(piece, text)


# ------------------------------------------------------------------ 分词 ---
class TestTokenizerPure(unittest.TestCase):
    """不碰 numpy 的那部分分词行为（PC 上也跑）。"""

    def test_rejects_empty_and_wrong_type(self):
        tok = make_tokenizer(text_len=8)
        for bad in ("", "   ", None, 123):
            with self.assertRaises(SiglipTokenizeError):
                tok.encode(bad)

    def test_tokens_has_no_padding(self):
        tok = make_tokenizer()
        self.assertEqual(tok.tokens("a b"), [10, 11])

    def test_vocab_size_comes_from_the_backend(self):
        self.assertEqual(make_tokenizer().vocab_size, 32000)

    def test_missing_file_is_config_error(self):
        with self.assertRaises(SiglipConfigError):
            SiglipTokenizer("/tmp/definitely_missing_tokenizer.json")

    def test_bad_text_len_is_config_error(self):
        for bad in (0, -1, "8"):
            with self.assertRaises(SiglipConfigError):
                SiglipTokenizer("/tmp/none.json", text_len=bad,
                                backend=FakeTokenizerBackend())


@needs_numpy
class TestTokenizer(unittest.TestCase):
    def test_padding_and_truncation(self):
        tok = make_tokenizer(text_len=8, pad_id=1)
        arr = tok.encode("a b c")                      # 3 个 token
        self.assertEqual(arr.shape, (1, 8))
        self.assertEqual(arr.dtype, np.int64)
        self.assertEqual(arr[0].tolist()[:3], [10, 11, 12])
        self.assertEqual(arr[0].tolist()[3:], [1] * 5)

        long_ids = list(range(100, 130))
        tok2 = make_tokenizer(text_len=8, pad_id=1, table={"long": long_ids})
        arr2 = tok2.encode("long")
        self.assertEqual(arr2.shape, (1, 8))
        self.assertEqual(arr2[0].tolist(), long_ids[:8])

    def test_no_truncate_raises(self):
        tok = make_tokenizer(text_len=2, pad_id=1, table={"abc": [1, 2, 3]}, truncate=False)
        with self.assertRaises(SiglipTokenizeError):
            tok.encode("abc")

    def test_pads_to_the_model_text_len(self):
        self.assertEqual(len(make_tokenizer().encode("a b")[0]), TEXT_LEN)

    def test_batch(self):
        tok = make_tokenizer(text_len=4, pad_id=7)
        arr = tok.encode_batch(["a", "b b"])
        self.assertEqual(arr.shape, (2, 4))
        self.assertEqual(arr[0].tolist(), [10, 7, 7, 7])      # 1 token + 3 pad
        self.assertEqual(arr[1].tolist(), [10, 11, 7, 7])     # 2 token + 2 pad


# ------------------------------------------------------------------ 门面 ---
@needs_numpy
class TestModelWithFakeRuntime(unittest.TestCase):
    def setUp(self):
        self.img = np.zeros((IMAGE_SIZE, IMAGE_SIZE, 3), dtype=np.uint8)
        self.rt = FakeRuntime()
        self.m = SiglipModel(_cfg(), runtime=self.rt, tokenizer=make_tokenizer(text_len=8))

    def test_encode_image_shape_and_unit_norm(self):
        v = self.m.encode_image(self.img)
        self.assertEqual(v.shape, (EMBED_DIM,))
        self.assertEqual(v.dtype, np.float32)
        self.assertAlmostEqual(float(np.linalg.norm(v)), 1.0, places=5)

    def test_encode_image_accepts_batched_input(self):
        self.assertEqual(self.m.encode_image(self.img[None, ...]).shape, (EMBED_DIM,))

    def test_encode_image_rejects_bad_shape_and_float(self):
        with self.assertRaises(SiglipInputError):
            self.m.encode_image(np.zeros((128, 128, 3), dtype=np.uint8))
        with self.assertRaises(SiglipInputError) as ctx:
            self.m.encode_image(np.zeros((IMAGE_SIZE, IMAGE_SIZE, 3), dtype=np.float32))
        self.assertIn("0-1", str(ctx.exception))       # 提示必须喂 0-255

    def test_text_cache_avoids_extra_calls(self):
        self.m.encode_text("hello")
        after_first = self.rt.calls
        self.m.encode_text("hello")
        self.assertEqual(self.rt.calls, after_first)

    def test_each_call_gets_a_fresh_data_type_list(self):
        """回归：runtime 每次必须收到新建的 data_type list（见 runtime.py 坑 1）。"""
        rt = FakeRuntime()
        m = SiglipModel(_cfg(), runtime=rt, tokenizer=make_tokenizer(text_len=8))
        m.encode_text("a")
        m.encode_text("b")
        self.assertEqual(rt.calls, 2)                  # 第二次不能被 dtype 污染卡住

    def test_similarity_for_identical_vectors(self):
        m = SiglipModel(_cfg(), runtime=FakeRuntime(), tokenizer=make_tokenizer(text_len=8))
        self.assertAlmostEqual(m.similarity(self.img, "x"), 1.0, places=5)

    def test_similarities_computes_image_once(self):
        rt = FakeRuntime()
        m = SiglipModel(_cfg(), runtime=rt, tokenizer=make_tokenizer(text_len=8))
        rt.calls = 0
        scores = m.similarities(self.img, ["a", "b", "c"])
        self.assertEqual(len(scores), 3)
        self.assertEqual(rt.calls, 4)                  # 1 次图像 + 3 次文本

    def test_rank_is_descending(self):
        class Rt(FakeRuntime):
            def run(self, image, input_ids):
                idx = int(np.asarray(input_ids).sum())  # 假 backend 用 10+n
                vec = np.zeros(EMBED_DIM, dtype=np.float32)
                vec[idx % EMBED_DIM] = 1.0
                return np.ones((1, EMBED_DIM), dtype=np.float32), vec.reshape(1, -1)

        tok = make_tokenizer(text_len=8, table={"a": [1], "b": [2], "c": [3]})
        m = SiglipModel(_cfg(), runtime=Rt(), tokenizer=tok)
        ranked = m.rank(self.img, ["a", "b", "c"])
        scores = [s for _, s in ranked]
        self.assertEqual(scores, sorted(scores, reverse=True))
        self.assertEqual(len(ranked), 3)
        self.assertEqual(m.best(self.img, ["a", "b", "c"])[0], ranked[0][0])
        self.assertEqual(len(m.rank(self.img, ["a", "b", "c"], top_k=2)), 2)

    def test_empty_texts_rejected(self):
        with self.assertRaises(SiglipInputError):
            self.m.similarities(self.img, [])

    def test_close_closes_owned_runtime_only(self):
        rt = FakeRuntime()
        m = SiglipModel(_cfg(), runtime=rt, tokenizer=make_tokenizer(text_len=8))
        m.close()
        self.assertFalse(rt.closed, "注入的 runtime 不归它管")
        m2 = SiglipModel(_cfg(), runtime=None, tokenizer=make_tokenizer(text_len=8))
        m2._runtime = rt                               # 伪装成自己加载的
        m2._owned_runtime = True
        m2.close()
        self.assertTrue(rt.closed)

    def test_stats(self):
        self.m.encode_image(self.img)
        st = self.m.stats()
        self.assertIn("config", st)
        self.assertEqual(st["calls"], 1)

    def test_constructing_alone_does_not_touch_the_model_file(self):
        # 构造**不加载模型**（不碰 rknnlite）—— 这样"先写好代码、模型后到位"才有可能
        m = SiglipModel(_cfg(model_path="/no/such/model.rknn"),
                        runtime=FakeRuntime(), tokenizer=make_tokenizer(text_len=8))
        self.assertTrue(m.ready)


# ------------------------------------------------------------ 板端集成层 ---
def _vision_section():
    """从仓库里的配置取 `vision:` 段（live 优先，没有就用模板）。

    ⚠ `agent.config.load_config()` 只认白名单里的名字（config / user_profile），
      `config.example` 不在其中（那是刻意的守卫）。所以模板这份直接用 yaml 读 ——
      本函数只是**找一份能代表生产契约的 vision: 段**，不是在生产路径上读配置。
    """
    import agent.config as agent_config
    import yaml

    live = _PROJECT_ROOT / "config" / "config.yaml"
    if live.is_file():
        try:
            data = agent_config.load_config("config")
        except Exception:                              # noqa: BLE001
            data = None
        section = data.get("vision") if isinstance(data, dict) else None
        if isinstance(section, dict):
            return section

    example = _PROJECT_ROOT / "config" / "config.example.yaml"
    if example.is_file():
        try:
            with open(str(example), "r", encoding="utf-8") as fh:
                data = yaml.safe_load(fh)
        except Exception:                              # noqa: BLE001
            data = None
        section = data.get("vision") if isinstance(data, dict) else None
        if isinstance(section, dict):
            return section
    return None


def _board_ready():
    section = _vision_section()
    if section is None:
        return False
    try:
        cfg = SiglipConfig.from_config(section)
    except Exception:                                  # noqa: BLE001
        return False
    if not (os.path.exists(cfg.model_path) and os.path.exists(cfg.tokenizer_path)):
        return False
    try:
        import cv2       # noqa: F401
        import rknnlite  # noqa: F401
        import tokenizers  # noqa: F401
    except ImportError:
        return False
    return True


@unittest.skipUnless(_board_ready(), "需要模型 + tokenizer + NPU 运行时 + cv2")
class TestModelOnBoard(unittest.TestCase):
    """真实双塔集成测试：形状、确定性、判别力回归（板端才跑）。"""

    @classmethod
    def setUpClass(cls):
        import cv2

        cls.cv2 = cv2
        cls.model = SiglipModel.from_config(_vision_section())
        cls.cfg = cls.model.config
        cls.images = [
            # 这三张是 scripts/make-wallpaper-samples.py 造的**纯色**样张——
            # 所以描述必须照实写（第一次写成 "a landscape"，模型诚实地选了
            # "a blue sky with clouds"：一块纯蓝确实更像天空）
            ("/home/kickpi/wallpapers/01_landscape_1280x800.png", "a solid blue rectangle"),
            ("/home/kickpi/wallpapers/06_extreme_600x1200.png",
             "a solid dark red rectangle"),
            ("/home/kickpi/wallpapers/08_circle_1280x1600.png",
             "a dark green rectangle with a white circle"),
        ]

    @classmethod
    def tearDownClass(cls):
        cls.model.close()

    def _load(self, path):
        raw = self.cv2.imread(path)
        if raw is None:
            self.skipTest("读不到图片 %s" % path)
        # 官方口径：直接 resize 到 256×256（不保比例）+ BGR→RGB + 原始 0-255
        return self.cv2.resize(self.cv2.cvtColor(raw, self.cv2.COLOR_BGR2RGB),
                               (IMAGE_SIZE, IMAGE_SIZE))

    def test_embedding_shapes_and_norms(self):
        img = self._load(self.images[0][0])
        ie = self.model.encode_image(img)
        te = self.model.encode_text("a photo of a mountain landscape")
        self.assertEqual(ie.shape, (self.cfg.embed_dim,))
        self.assertEqual(te.shape, (self.cfg.embed_dim,))
        self.assertAlmostEqual(float(np.linalg.norm(ie)), 1.0, places=4)
        self.assertAlmostEqual(float(np.linalg.norm(te)), 1.0, places=4)

    def test_deterministic(self):
        img = self._load(self.images[0][0])
        a = self.model.encode_image(img)
        b = self.model.encode_image(img)
        self.assertEqual(float(np.abs(a - b).max()), 0.0)

    def test_discrimination_diagonal_dominates(self):
        """判别力回归：每张图的最佳文本应当是它自己的描述。"""
        caps = [c for _, c in self.images] + [
            "a cat sleeping on a sofa", "a cup of coffee on a table",
            "a blue sky with clouds"]
        for path, own in self.images:
            if not os.path.exists(path):
                continue
            best, _ = self.model.best(self._load(path), caps)
            self.assertEqual(best, own, "图片 %s 的最佳文本应为 %r，实际 %r"
                             % (path, own, best))


if __name__ == "__main__":
    unittest.main(verbosity=2)
