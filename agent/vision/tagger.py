# ============================================================================
#  agent/vision/tagger.py — 给壁纸打标签（Phase 7 T7-2）
#
#  它做什么
#  ---------------------------------------------------------------------------
#  一张图 → 三个轴的 top-k 标签（含余弦分数）+ L2 归一化后的图像向量。
#  用 SigLIP 零样本分类（`agent/vision/siglip/`），词表见 `tag_vocab.py`。
#
#  预处理: **照官方口径**（这条差点被我改错）
#  ---------------------------------------------------------------------------
#  `preprocessor_config.json` 就是 `size: 256×256`（**各向异性压扁**、不保比例）
#  + `(x/255 - 0.5)/0.5`，而后者**已经烧进 rknn 计算图**。所以:
#      cv2.imread(BGR) → cvtColor 到 RGB → resize 到 256×256（压扁）→ **原始 0-255**
#  不要"改成等比 letterbox"：那会跑到模型训练分布之外（报告 §7.2 的坍缩是模型对
#  UI 截图的固有行为，不是预处理 bug）。极端比例图是否更适合中心裁切，由
#  `tests/board/preprocess_ab.py` 用真值实测，明显更好才加开关。
#
#  依赖与边界
#  ---------------------------------------------------------------------------
#  · 需要 numpy + cv2 + rknnlite + tokenizers（**板端才有**）。本模块把它们延迟导入，
#    缺了会抛 TaggingUnavailable 并说清缺哪个、怎么装 —— `assistant tag` 直接转述。
#  · 这层**只算标签**，不碰数据文件（读写/指纹在 wall_data.py），不碰 IPC，
#    也不关心"谁调用了它"。所以它能被单独测（PC 上用假模型）。
# ============================================================================

from __future__ import annotations

import hashlib
import logging
import os
import time
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from . import tag_vocab
from .siglip import IMAGE_SIZE, SiglipModel

__all__ = [
    "TaggingUnavailable",
    "DEFAULT_TOP_K",
    "check_dependencies",
    "preprocess",
    "score_axes",
    "Tagging",
]

_log = logging.getLogger(__name__)

#: 每轴存几条候选。**刻意多存几条**（而不是只存 top-1）: 阈值是查询期的事，
#: 存宽一点，以后标定阈值/换筛选口径都不用重打标签。
DEFAULT_TOP_K = 3


class TaggingUnavailable(RuntimeError):
    """这台机器上打不了标签（缺 numpy/cv2/NPU 运行时/模型文件）。"""


def check_dependencies(require_model: bool = True) -> Optional[str]:
    """检查打标签需要的依赖。

    @return None = 齐了；否则一句**给人看**的说明（缺什么、怎么装）
    @note 与 `assistant tag` 的 dry-run 共用: dry-run 只需要 numpy 吗? 不需要 ——
          它只算"哪些图要打"，所以 `require_model=False` 时连 numpy 都不查。
    """
    if not require_model:
        return None
    missing = []
    for module, hint in (("numpy", "pip3 install numpy"),
                         ("cv2", "apt install python3-opencv"),
                         ("rknnlite", "rknn-toolkit-lite2"),
                         ("tokenizers", "pip3 install --no-deps tokenizers==0.20.3")):
        try:
            __import__(module)
        except ImportError:
            missing.append("%s（%s）" % (module, hint))
    if missing:
        return "打标签需要这些依赖: " + "、".join(missing)
    return None


def preprocess(raw: Any, size: int = IMAGE_SIZE) -> Any:
    """cv2 读出来的 BGR 图 → 模型要的 (size, size, 3) RGB uint8（官方口径：压扁）。"""
    import cv2

    if raw is None:
        raise TaggingUnavailable("图片读不出来（cv2.imread 返回 None）")
    rgb = cv2.cvtColor(raw, cv2.COLOR_BGR2RGB)
    if rgb.shape[0] != size or rgb.shape[1] != size:
        rgb = cv2.resize(rgb, (size, size))
    return rgb


def load_image(path: str, size: int = IMAGE_SIZE) -> Any:
    """读图并预处理。**逐张读**（4K 图 ≈ 24MB，40 张一起进内存会把 3.9GB 无 swap 的板子压垮）。"""
    import cv2

    raw = cv2.imread(path)
    if raw is None:
        raise TaggingUnavailable("读不出图片: %s" % path)
    return preprocess(raw, size=size)


def score_axes(model: SiglipModel, image: Any, axes: Sequence[tag_vocab.Axis],
               top_k: int = DEFAULT_TOP_K,
               label_vectors: Optional[Mapping[str, List[Tuple[str, Any]]]] = None,
               ) -> Dict[str, List[Tuple[str, float]]]:
    """算一张图在每个轴上的 top-k。

    @param label_vectors 预编码好的 {轴名: [(标签, 向量)]} —— 省掉重复编码词表
                         （40 张图 × 32 条标签会重复 40 次，实测值得先算一次）
    @return {轴名: [(标签, 余弦), ...]}，每轴按分数降序（空轴不出现在结果里）
    """
    import numpy as np

    image_vec = np.asarray(model.encode_image(image), dtype=np.float32)
    out: Dict[str, List[Tuple[str, float]]] = {}
    for axis in axes:
        if label_vectors is not None and axis.name in label_vectors:
            pairs = [(label, float(np.dot(image_vec, np.asarray(vec, dtype=np.float32))))
                     for label, vec in label_vectors[axis.name]]
        else:
            pairs = [(label, model.similarity(image, label)) for label in axis.texts]
        pairs.sort(key=lambda kv: kv[1], reverse=True)
        out[axis.name] = pairs[:max(1, int(top_k))]
    return out


def encode_labels(model: SiglipModel, axes: Sequence[tag_vocab.Axis],
                  on_progress: Optional[Callable[[str], None]] = None
                  ) -> Dict[str, List[Tuple[str, Any]]]:
    """把词表里所有标签预先编码一遍（每轴一次）。

    @return {轴名: [(标签, 向量), ...]}；向量是 **numpy 数组**（打分用），
            写进数据文件前由 `wall_data.make_vocab_record()` 转成 base64。
    @note 词表是 32 条左右，一次几秒到几十秒（板端实测 61 s）；之后每张图只跑**图像塔**。
          这份结果可以存进数据文件第一行复用（见 `wall_data` 的文件头）。
    """
    out: Dict[str, List[Tuple[str, Any]]] = {}
    for axis in axes:
        pairs: List[Tuple[str, Any]] = []
        for label in axis.texts:
            pairs.append((label, model.encode_text(label)))
        out[axis.name] = pairs
        if on_progress is not None:
            on_progress("%s: %d 条标签已编码" % (axis.title, len(pairs)))
    return out


# ---------------------------------------------------------------------------
#  对外: 一次打一批
# ---------------------------------------------------------------------------
class Tagging(object):
    """给一批图打标签。

    典型用法::

        tag = Tagging(section, axes, top_k=3)
        for record, ms in tag.each(paths):
            ...
    """

    def __init__(self, vision_section: Optional[Mapping[str, Any]] = None,
                 axes: Sequence[tag_vocab.Axis] = tag_vocab.AXES,
                 top_k: int = DEFAULT_TOP_K,
                 model: Any = None) -> None:
        self.axes = tuple(axes)
        self.top_k = max(1, int(top_k))
        self.model = model
        self.load_s = 0.0
        self.label_s = 0.0
        #: 词表向量是从数据文件缓存来的吗（`assistant tag` 会把它打进输出）
        self.labels_from_cache = False
        #: 最近一张失败的原因（`each()` 里更新；批次跑完想知道为什么失败了就看它）
        self.last_error: Optional[str] = None
        self._vision_section = vision_section
        self._labels: Optional[Dict[str, List[Tuple[str, Any]]]] = None

    # ------------------------------------------------------------ 准备 ---
    def model_sha8(self) -> str:
        """模型指纹（前 8 位 sha256）—— 换模型 = 标签作废。

        @note 读的是**模型文件本身**（不是配置里的名字）: 同一个路径换成别的模型
              也要能被发现。
        """
        if self.model is not None:
            path = getattr(getattr(self.model, "config", None), "model_path", "") or ""
        else:
            from .siglip import SiglipConfig

            path = SiglipConfig.from_config(self._vision_section).model_path
        if not path or not os.path.exists(str(path)):
            # 模型文件不在（例如 PC 上）—— 用路径当指纹，至少"换了路径"能发现
            return "missing:" + os.path.basename(str(path))[:24]
        digest = hashlib.sha256()
        with open(str(path), "rb") as handle:
            for block in iter(lambda: handle.read(1 << 20), b""):
                digest.update(block)
        return digest.hexdigest()[:8]

    def prepare(self, label_vectors: Optional[Mapping[str, List[Tuple[str, Any]]]] = None,
                ) -> None:
        """加载模型 + 编码词表（幂等）。

        @param label_vectors 已有的词表向量（来自数据文件第一行的缓存）—— 给了就用它，
                              **跳过 61 s 的文本塔编码**。它必须是完整的
                              {轴名: [(标签, 向量), ...]}，缺哪个轴就重新编码那个轴。
        @note 模型本身**总是要加载**（图像塔要跑），缓存省的是文本塔那部分。
        """
        if self.model is None:
            reason = check_dependencies()
            if reason:
                raise TaggingUnavailable(reason)
            self.model = SiglipModel.from_config(self._vision_section)
        started = time.time()
        self.model.load()
        self.load_s = time.time() - started

        if label_vectors and all(axis.name in label_vectors for axis in self.axes):
            self._labels = {axis.name: list(label_vectors[axis.name]) for axis in self.axes}
            self.label_s = 0.0
            self.labels_from_cache = True
            return

        if self._labels is None:
            started = time.time()
            self._labels = encode_labels(self.model, self.axes)
            self.label_s = time.time() - started
            self.labels_from_cache = False

    def label_vectors(self) -> Optional[Dict[str, List[Tuple[str, Any]]]]:
        """已编码的词表向量（没编码过就是 None）—— 拿来写数据文件第一行。"""
        return self._labels

    # ------------------------------------------------------------ 打分 ---
    def score(self, path: str) -> Tuple[Dict[str, List[Tuple[str, float]]], Any, int, int, int]:
        """一张图 → (三轴 top-k, 图像向量, 耗时 ms, 原图宽, 原图高)。

        @note 原图**只读一次**（cv2.imread 4K 图要 0.3–1s，读两次是白花时间）。
        """
        self.prepare()
        started = time.time()
        import cv2
        import numpy as np

        raw = cv2.imread(path)
        if raw is None:
            raise TaggingUnavailable("读不出图片: %s" % path)
        height, width = int(raw.shape[0]), int(raw.shape[1])
        image = preprocess(raw)
        del raw                                   # 4K 图 ≈ 24MB，早点放手
        tags = score_axes(self.model, image, self.axes, top_k=self.top_k,
                          label_vectors=self._labels)
        vector = np.asarray(self.model.encode_image(image), dtype=np.float32)
        return tags, vector, int((time.time() - started) * 1000), width, height

    def each(self, paths: Iterable[str],
             on_done: Optional[Callable[[str, Dict[str, Any]], None]] = None,
             on_error: Optional[Callable[[str, str], None]] = None
             ) -> Iterable[Tuple[str, Dict[str, Any]]]:
        """逐张打分（**顺序执行**，一张一释放）。

        @param on_done  每张成功的回调（`assistant tag` 拿它边打边写盘）
        @param on_error 失败的回调（缺图/坏图不该让整批停下）
        @yield (路径, {"tags", "vector", "ms", "w", "h", "bytes"})
        """
        self.prepare()
        for path in paths:
            try:
                tags, vector, ms, width, height = self.score(path)
            except Exception as exc:                     # noqa: BLE001 - 单张失败不拖垮整批
                self.last_error = "%s: %s" % (type(exc).__name__, exc)
                if on_error is not None:
                    on_error(path, self.last_error)
                continue
            try:
                size = int(os.path.getsize(path))
            except OSError:
                size = 0
            meta: Dict[str, Any] = {"tags": tags, "vector": vector, "ms": ms,
                                    "w": width, "h": height, "bytes": size}
            if on_done is not None:
                on_done(path, meta)
            yield path, meta

    def __repr__(self) -> str:
        return "<Tagging axes=%s top_k=%d model=%s>" % (
            ",".join(axis.name for axis in self.axes), self.top_k,
            "ready" if self.model is not None else "not loaded")
