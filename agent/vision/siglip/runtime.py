# ============================================================================
#  agent/vision/siglip/runtime.py — RKNN 运行时适配（薄封装，可注入）
#
#  为什么单独一层
#  ---------------------------------------------------------------------------
#  · 把 rknnlite 的怪癖集中在一个文件里，上层（model.py）只管语义
#  · 单测可以注入假 runtime，不需要 NPU、不需要模型文件就能测 model.py
#
#  ⚠ 两个实测踩过的坑（都在这里处理，别在别处重复踩）
#  ---------------------------------------------------------------------------
#  1) `RKNNLite.inference(..., data_type=[...])` 会**就地改写**传入的 data_type
#     列表（把 "uint8" 换成内部码 3 之类）。复用一个 list 第二次必然报
#     "inputs[0].dtype: uint8 is not same as data_type[0]"。
#     → 本模块每次调用都新建 list。
#  2) `data_format` 只接受 'nhwc'（传 'nchw' 直接 KeyError: 'nchw'）。
#     → 固定用 nhwc，语义由 config.py 的 IMAGE_LAYOUT 校验保证。
#
#  来历：从板端实验树 `sig/siglip/runtime.py` 原样搬入（已实测验证）。
# ============================================================================

from __future__ import annotations

import time
from typing import Any, Tuple

from .errors import SiglipRuntimeError

__all__ = ["RknnRuntime"]

#: 模型缺省输入名（config.py 的 *_INPUT_NAME 与之对应，仅用于报错信息）
_IMAGE_INPUT = "pixel_values"
_TEXT_INPUT = "input_ids"


class RknnRuntime(object):
    """把 (image, input_ids) 喂进双塔 rknn，取回 (image_embeds, text_embeds)。"""

    def __init__(self, model_path: str, image_dtype: str = "uint8",
                 text_dtype: str = "int64", npu_core: str = "auto",
                 verbose: bool = False, warmup_runs: int = 0) -> None:
        self.model_path = model_path
        self.image_dtype = image_dtype
        self.text_dtype = text_dtype
        self.npu_core = (npu_core or "auto").lower()
        self.warmup_runs = int(warmup_runs or 0)
        self._warmed = 0
        self.load_s = 0.0
        self.init_s = 0.0
        self.calls = 0
        self._rknn = None
        self._load(verbose)

    # ------------------------------------------------------------ 加载 ---
    def _load(self, verbose: bool) -> None:
        try:
            from rknnlite.api import RKNNLite
        except ImportError as exc:
            raise SiglipRuntimeError("缺少 rknnlite（rknn-toolkit-lite2）：%s" % exc)

        rknn = RKNNLite(verbose=bool(verbose))
        t0 = time.time()
        ret = rknn.load_rknn(self.model_path)
        self.load_s = time.time() - t0
        if ret != 0:
            raise SiglipRuntimeError(
                "load_rknn 失败(%s): %s —— 多半是模型版本与 librknnrt 不匹配"
                "（model version 6 需要 >= 1.6.0）" % (ret, self.model_path))

        t0 = time.time()
        # RK3568 只支持"不传 core_mask"；core_mask 仅 RK3588 有效，传了 init 直接 -1
        if self.npu_core in ("", "auto"):
            ret = rknn.init_runtime()
        else:
            mask = getattr(RKNNLite, "NPU_CORE_" + self.npu_core, None)
            if mask is None:
                raise SiglipRuntimeError("npu_core 非法: %r" % self.npu_core)
            ret = rknn.init_runtime(core_mask=mask)
        self.init_s = time.time() - t0
        if ret != 0:
            raise SiglipRuntimeError("init_runtime 失败(%s)，model=%s" % (ret, self.model_path))
        self._rknn = rknn

    # ------------------------------------------------------------ 推理 ---
    def run(self, image, input_ids) -> Tuple[Any, Any]:
        """一次前向。返回 (image_embeds, text_embeds)，形状均为 (1, embed_dim)。"""
        if self._rknn is None:
            raise SiglipRuntimeError("运行时未初始化")
        # 每次新建 list —— set_inputs 会就地改写（见文件头坑 1）
        data_type = [self.image_dtype, self.text_dtype]
        out = self._rknn.inference([image, input_ids], data_type=data_type, data_format="nhwc")
        if out is None:
            raise SiglipRuntimeError("inference 返回 None（输入 dtype/形状不匹配？）")
        if len(out) < 2:
            raise SiglipRuntimeError(
                "期望 2 个输出(image_embeds, text_embeds)，实际 %d 个" % len(out))
        self.calls += 1

        if self.warmup_runs and self._warmed < self.warmup_runs:
            # ⚠ 这里传的是 **ndarray 本身**；只有 data_type 需要每次新建 list。
            #    （把 input 也 list() 会报 AttributeError: 'list' object has no attribute 'size'）
            for _ in range(self.warmup_runs - self._warmed):
                self._rknn.inference([image, input_ids],
                                     data_type=[self.image_dtype, self.text_dtype],
                                     data_format="nhwc")
                self.calls += 1
            self._warmed = self.warmup_runs
        return out[0], out[1]

    # ------------------------------------------------------------ 释放 ---
    def close(self) -> None:
        if self._rknn is not None:
            try:
                self._rknn.release()
            except Exception:                         # noqa: BLE001
                pass
            self._rknn = None

    def __repr__(self) -> str:
        return "<RknnRuntime %s core=%s load=%.2fs init=%.2fs calls=%d>" % (
            self.model_path, self.npu_core, self.load_s, self.init_s, self.calls)
