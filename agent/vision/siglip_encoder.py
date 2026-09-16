# ============================================================================
#  agent/vision/siglip_encoder.py — SigLIP 图像编码 (当前 mock)
#
#  它在链路里的位置
#  ---------------------------------------------------------------------------
#      image_rb (256×256 RGB888) → **SigLIPEncoder.encode()** → embedding
#        → 交给 LLM 做"图里是什么"的判断
#
#  ⚠ 当前是 mock, 不做真实推理
#  ---------------------------------------------------------------------------
#  encode() 返回**由图像内容决定的确定性伪随机向量**, 不是真的 SigLIP 输出:
#    · 确定性: 同一张图 -> 同一向量。这让上层"同一帧只编码一次""向量可用于
#      比较"之类的逻辑现在就能写并测, 换成真模型时行为不变。
#    · 不是真推理: 向量没有语义, 相似图片不会得到相似向量。不要拿它做识别。
#  `ready` 恒为 False, 明确表示"真实现还没有"。
#
#  真实实现要做什么 (后续任务)
#  ---------------------------------------------------------------------------
#  接 agent_native / RKNN runtime: 把 256×256×3 按模型要求量化/归一化后喂进
#  RKNN, 取输出层向量。本模块的对外接口 (构造 + encode) 不用改。
#
#  为什么不在这里做预处理 / 缓存 (按约定)
#  ---------------------------------------------------------------------------
#  · 预处理: native 层已经把 YUV 变成 256×256 RGB888 了, 这里再动一次只会
#    让"到底谁负责什么"变模糊
#  · 缓存: embedding 缓存的失效策略依赖调用场景 (按帧? 按 ROI? 按时间?),
#    放这里会变成猜; 留给真正知道策略的那一层
# ============================================================================

from __future__ import annotations

import hashlib
from typing import Any, Optional, Sequence, Tuple

__all__ = [
    "SigLIPEncoder",
    "SigLIPError",
    "DEFAULT_EMBEDDING_DIM",
    "EXPECTED_SHAPE",
]

#: 期望的输入形状 (native preprocess 的输出)
EXPECTED_SHAPE: Tuple[int, int, int] = (256, 256, 3)

#: mock 的向量维度。取 768 是为了贴近 SigLIP-B/16 的常用维度, 这样把 mock
#: 换成真模型时下游如果按维度做了假设, 不会因为维度突变而炸。
DEFAULT_EMBEDDING_DIM = 768

#: 向量 dtype 用 float32 (RKNN 输出一般是 float32)
_DTYPE = "float32"


class SigLIPError(RuntimeError):
    """编码器错误 (输入形状不对、模型没接上……)。"""


class SigLIPEncoder:
    """SigLIP 图像编码器 (当前 mock)。

    典型用法::

        encoder = SigLIPEncoder("models/siglip.rknn")
        emb = encoder.encode(frame)          # numpy (768,) float32
        encoder.ready                        # False —— 还是 mock

    测试可以注入:
        encoder = SigLIPEncoder("x", dim=16, rng=random.Random(0))
    """

    def __init__(
        self,
        model_path: str,
        dim: int = DEFAULT_EMBEDDING_DIM,
        expected_shape: Sequence[int] = EXPECTED_SHAPE,
        rng: Any = None,
    ) -> None:
        """
        @param model_path    模型文件路径。**mock 不加载它**, 只记录下来;
                             构造时不检查文件是否存在 —— 检查了就会让"先写好
                             代码、模型后到位"变得不可能, 而且真实现才需要文件。
        @param dim           输出向量维度
        @param expected_shape 期望输入形状 (校验用)
        @param rng           注入的随机源 (需要 .randbytes 或 .getrandbits 或
                             .random); None 时按图像内容自行播种
        """
        if not isinstance(model_path, str) or not model_path.strip():
            raise SigLIPError("model_path must be a non-empty string")
        if not isinstance(dim, int) or dim <= 0:
            raise SigLIPError("dim must be a positive int, got %r" % (dim,))

        shape = tuple(int(v) for v in expected_shape)
        if len(shape) != 3 or any(v <= 0 for v in shape):
            raise SigLIPError("expected_shape must be (h, w, c) with positive values")

        self.model_path = model_path
        self.dim = dim
        self.expected_shape = shape
        self._rng = rng
        #: 累计编码次数 —— 诊断用, 也方便测试断言"没被重复调用"
        self.encoded_count = 0

    # ------------------------------------------------------------ 状态 ---
    @property
    def ready(self) -> bool:
        """真实模型是否已就绪。

        mock 恒为 False —— 上层可以据此在界面上标"视觉能力不可用",
        而不是等调用之后才发现拿到的是假向量。
        """
        return False

    # ------------------------------------------------------------ 编码 ---
    def encode(self, image: Any) -> Any:
        """把一张 256×256×3 的 RGB 图编码成 embedding。

        @param image 形如 (256,256,3) 的数组 (numpy 或任意支持 .shape/len 的序列)
        @return (dim,) float32 向量; 没有 numpy 时退化成 list[float]
        @raise SigLIPError 形状不符合 expected_shape
        """
        shape = _shape_of(image)
        if shape != self.expected_shape:
            raise SigLIPError(
                "image shape must be %s, got %s" % (self.expected_shape, shape)
            )

        self.encoded_count += 1

        np = _try_numpy()
        if np is None:
            # 没 numpy (宿主机就是这种情况): 退化成 list, 接口不变
            return self._encode_as_list(image)

        # 由内容决定种子 -> 同一张图永远得到同一向量
        seed = _content_seed(image)
        rng = np.random.default_rng(seed)
        vector = rng.standard_normal(self.dim)
        # 归一化到单位长度: 真 SigLIP 输出通常也是归一化的, 下游可以直接点积
        norm = float(np.linalg.norm(vector))
        if norm > 0:
            vector = vector / norm
        return vector.astype(_DTYPE)

    def _encode_as_list(self, image: Any) -> list:
        """无 numpy 时的退化路径 (仅供宿主机测试/兜底)。"""
        import random

        rng = self._rng if self._rng is not None else random.Random(_content_seed(image))
        vector = [rng.gauss(0.0, 1.0) for _ in range(self.dim)]
        norm = sum(v * v for v in vector) ** 0.5
        if norm > 0:
            vector = [v / norm for v in vector]
        return vector

    # ------------------------------------------------------------ 诊断 ---
    def __repr__(self) -> str:
        return "<SigLIPEncoder %s dim=%d ready=%s encoded=%d>" % (
            self.model_path,
            self.dim,
            self.ready,
            self.encoded_count,
        )


# ---------------------------------------------------------------------------
#  辅助
# ---------------------------------------------------------------------------
def _try_numpy() -> Optional[Any]:
    try:
        import numpy  # noqa: F401
    except ImportError:
        return None
    return numpy


def _shape_of(image: Any) -> Optional[Tuple[int, ...]]:
    """取数组形状。

    只认标准的 `.shape` 协议 (numpy / torch / 任何数组类库都提供它)。
    刻意不去猜嵌套 list 的形状 —— 猜错了会把"传错东西"变成"形状碰巧不匹配"的
    模糊报错, 不如直接说形状取不到。
    """
    shape = getattr(image, "shape", None)
    if shape is None:
        return None
    try:
        return tuple(int(v) for v in shape)
    except (TypeError, ValueError):
        return None


def _content_seed(image: Any) -> int:
    """由图像内容算出稳定的种子。

    用 BLAKE2b 而不是内置 hash(): 内置 hash 对 bytes 有随机化 (PYTHONHASHSEED),
    跨进程不稳定 —— 那样"同一张图得到同一向量"这条性质在重启后就没了。
    只取摘要前 8 字节。
    """
    digest = hashlib.blake2b(_content_bytes(image), digest_size=8).digest()
    return int.from_bytes(digest, "big")


def _content_bytes(image: Any) -> bytes:
    """把图像内容转成 bytes 用于摘要。

    优先 tobytes() (numpy/array 的标准接口); 拿不到就退回形状 + 类型 + repr,
    至少保证"不同的东西得到不同的种子", 而不是全都撞到同一个常量。
    """
    tobytes = getattr(image, "tobytes", None)
    if callable(tobytes):
        try:
            return bytes(tobytes())
        except Exception:  # noqa: BLE001
            pass
    if isinstance(image, (bytes, bytearray, memoryview)):
        return bytes(image)
    try:
        return ("%s|%s" % (_shape_of(image), type(image).__name__)).encode("utf-8")
    except Exception:  # noqa: BLE001
        return b""
