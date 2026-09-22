# ============================================================================
#  agent/vision/tag_vocab.py — 壁纸标签词表（三轴，Phase 7 T7-2）
#
#  为什么是"固定词表 + 零样本分类"，而不是让模型自由描述
#  ---------------------------------------------------------------------------
#  板端实测（llm/multimodal_report.md）: 让 0.8B 的 VLM 自由描述一张图要 649 s，
#  而 SigLIP 零样本分类只要 1.3–1.7 s/张。但零样本分类的**上限就是这张词表** ——
#  词表里没有的东西它永远选不出来（报告 §7.2 记着这个失败模式: UI 截图全坍缩到
#  "terminal"）。所以:
#    · 词表要覆盖**库里真实会有的**类别（含 abstract / anime 这种兜底类）
#    · 词表变了 → 标签要重打（靠 vocab_sha8 指纹判定，见 wall_data.py）
#
#  为什么标签是**英文裸串**（没有 "a photo of ..." 模板）
#  ---------------------------------------------------------------------------
#  · SigLIP-base-patch16-256 是在**英文**网络图文对上训练的，中文标签没有意义
#    （中文只在界面上给人看，见每个 Label 的 zh）
#  · 裸串是它的分布内用法，板端实测判别力够（sig/ 里那条"对角占优"回归）
#  · 加模板是把简单问题复杂化: 模板也是要标定的东西，而这里没有标定预算
#
#  词表能不能改
#  ---------------------------------------------------------------------------
#  能，而且不用改代码: `config.yaml` 的 `wallpaper.tagging.vocab` 可以**按轴追加**
#  标签（追加式，不是覆盖式 —— 覆盖会让"默认该有哪些标签"只存在于配置里）。
#  想**删**默认标签就改这个文件，并重跑 `assistant tag`（指纹会让旧标签作废）。
# ============================================================================

from __future__ import annotations

import hashlib
from typing import Any, Dict, Iterable, List, Mapping, Sequence, Tuple

__all__ = [
    "Label",
    "Axis",
    "SCENE",
    "TONE",
    "MOOD",
    "AXES",
    "AXIS_BY_NAME",
    "AXIS_NAMES",
    "vocab_sha8",
    "with_overrides",
    "axis_texts",
    "label_zh",
]


class Label(object):
    """一个候选标签: 英文串（喂给模型）+ 中文（给人看）。"""

    __slots__ = ("en", "zh")

    def __init__(self, en: str, zh: str) -> None:
        self.en = en
        self.zh = zh

    def __repr__(self) -> str:
        return "Label(%r, %r)" % (self.en, self.zh)

    def __eq__(self, other: Any) -> bool:
        return isinstance(other, Label) and (self.en, self.zh) == (other.en, other.zh)

    def __hash__(self) -> int:
        return hash((self.en, self.zh))


class Axis(object):
    """一个轴（`scene` / `tone` / `mood`）: 名字 + 中文标题 + 候选标签（顺序即优先级）。"""

    __slots__ = ("name", "title", "note", "labels")

    def __init__(self, name: str, title: str, note: str, labels: Sequence[Label]) -> None:
        self.name = name
        self.title = title
        self.note = note
        self.labels: Tuple[Label, ...] = tuple(labels)

    @property
    def texts(self) -> List[str]:
        return [label.en for label in self.labels]

    def zh_of(self, en: str) -> str:
        for label in self.labels:
            if label.en == en:
                return label.zh
        return en

    def with_extra(self, extra: Iterable[str]) -> "Axis":
        """追加标签（英文串；中文就显示英文）。同名不重复加。"""
        known = {label.en for label in self.labels}
        added = [Label(text, text) for text in extra if text and text not in known]
        return Axis(self.name, self.title, self.note, list(self.labels) + added)

    def __repr__(self) -> str:
        return "<Axis %s (%s) %d 个标签>" % (self.name, self.title, len(self.labels))


# ---------------------------------------------------------------------------
#  词表本体
# ---------------------------------------------------------------------------
SCENE = Axis("scene", "场景", "图里是什么", [
    Label("landscape", "风景"),
    Label("city", "城市"),
    Label("space", "太空"),
    Label("fantasy", "奇幻"),
    Label("anime", "动漫"),
    Label("portrait", "人物"),
    Label("architecture", "建筑"),
    Label("animal", "动物"),
    Label("plant", "植物"),
    Label("abstract", "抽象"),
    Label("minimal", "极简"),
    Label("vehicle", "载具"),
    Label("food", "食物"),
    Label("interior", "室内"),
    Label("technology", "科技"),
    Label("underwater", "水下"),
])

TONE = Axis("tone", "色调", "整体明暗与色相", [
    Label("dark", "暗色"),
    Label("bright", "明亮"),
    Label("colorful", "多彩"),
    Label("monochrome", "黑白灰"),
    Label("warm", "暖色"),
    Label("cool", "冷色"),
    Label("pastel", "柔和"),
    Label("neon", "霓虹"),
])

MOOD = Axis("mood", "氛围", "它给人的感觉", [
    Label("calm", "安静"),
    Label("energetic", "活力"),
    Label("mysterious", "神秘"),
    Label("cozy", "温馨"),
    Label("dramatic", "戏剧性"),
    Label("cute", "可爱"),
    Label("futuristic", "未来感"),
    Label("nostalgic", "怀旧"),
])

#: 轴的全集（顺序 = 展示顺序；也是 vocab_sha8 的一部分，别随手调）
AXES: Tuple[Axis, ...] = (SCENE, TONE, MOOD)
AXIS_BY_NAME: Dict[str, Axis] = {axis.name: axis for axis in AXES}
AXIS_NAMES: Tuple[str, ...] = tuple(axis.name for axis in AXES)


# ---------------------------------------------------------------------------
#  指纹与覆盖
# ---------------------------------------------------------------------------
def vocab_sha8(axes: Sequence[Axis] = AXES) -> str:
    """词表指纹（8 位十六进制）。

    内容 = 每个轴的**名字 + 全部英文标签（按顺序）**。
    用途: `wall_data.jsonl` 里存一份 —— 改了词表（加/删/改名/改顺序）指纹就变，
    `assistant tag` 据此判定"这些图要重打"，不用人肉记。
    """
    payload = ";".join(
        "%s:%s" % (axis.name, ",".join(axis.texts)) for axis in axes
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:8]


def with_overrides(overrides: Mapping[str, Any] = None,
                   axes: Sequence[Axis] = AXES) -> Tuple[Axis, ...]:
    """把配置里的 `wallpaper.tagging.vocab`（按轴追加）套到默认词表上。

    @param overrides `{"scene": ["cyberpunk"], "tone": [...]}`；未知轴名忽略
    @return 新的轴元组（顺序与默认一致）
    """
    if not isinstance(overrides, Mapping):
        return tuple(axes)
    out: List[Axis] = []
    for axis in axes:
        extra = overrides.get(axis.name)
        if isinstance(extra, (list, tuple)):
            texts = [str(item).strip() for item in extra if str(item).strip()]
            out.append(axis.with_extra(texts))
        else:
            out.append(axis)
    return tuple(out)


def axis_texts(axes: Sequence[Axis] = AXES) -> Dict[str, List[str]]:
    """{轴名: [英文标签...]} —— 给模型编码用。"""
    return {axis.name: axis.texts for axis in axes}


def label_zh(axis_name: str, en: str, axes: Sequence[Axis] = AXES) -> str:
    """标签的中文显示名（找不到就把英文原样返回）。"""
    for axis in axes:
        if axis.name == axis_name:
            return axis.zh_of(en)
    return en
