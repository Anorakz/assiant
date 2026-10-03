# ============================================================================
#  agent/vision/wall_data.py — 壁纸标签数据文件（config/wall_data.jsonl，T7-2）
#
#  这是什么
#  ---------------------------------------------------------------------------
#  **第一行**是词表记录（标签向量缓存），后面一行一张图（**派生数据**，不是配置真源）:
#      {"kind":"vocab", "version":1, "model_sha8":…, "vocab_sha8":…, "dim":768,
#       "axes":{"scene":["landscape", …], …},
#       "embeds":{"scene":["<base64 float32>", …], …}}
#      {"version": 1, "path": ..., "sha256": ..., "w": ..., "h": ..., "bytes": ...,
#       "tagged_at": ..., "ms": ..., "model_sha8": ..., "vocab_sha8": ...,
#       "tags": {"scene": [["landscape", 0.21], ...], ...},
#       "embedding": "<base64 float16 × 768>",
#       "used": 3, "last_used": "2026-09-25T10:06:44"}     ← T8-6 运行时字段
#
#  `used` / `last_used`（T8-6）
#  ---------------------------------------------------------------------------
#  这张当壁纸**显示过几次 / 上次是什么时候**。谁加: 运行时推图成功之后
#  （`Runtime._push_wallpaper` → `bump_usage_in_file()`，"换成这一张"才算一次，
#  重推/repeat/重连补推不算）。干什么用: `next_wallpaper(sort="used_asc")` 挑
#  "用得最少的"，以及 `action="tags"` 里回报使用情况。
#  ⚠ 缺这两个字段 = 0 / 没有（老数据文件不用重打)；**重打一张图时要 `with_usage()`
#    把计数带过去**（`assistant tag` 已经这么做），否则计数会被打标签覆盖掉。
#
#  两个向量字段，精度**故意不同**
#  ---------------------------------------------------------------------------
#  · 图像向量（`embedding`）: **float16** —— 语义检索用，省一半体积（一张图 ≈ 2KB）
#  · 词表向量（`embeds`）: **float32** —— 它会被 40 张图各用一次算分，
#    用 float16 存会让"复用缓存"与"现场重编码"的分数有 ~1e-3 的差，
#    那种"看起来一样但数字不同"最难查。32×768×4 ≈ 98KB，不值当省。
#
#  为什么把词表向量也存进来
#  ---------------------------------------------------------------------------
#  打标签时 32 条标签要各过一次文本塔（板端实测 **61 s**，占一次全量 206 s 的 30%）。
#  存下来后，**同模型同词表**的第二次运行直接复用 —— 增量打 1 张新图从 ~70 s 降到 ~5 s。
#  指纹（model_sha8 + vocab_sha8 + 轴上的标签列表）对不上就不用，重编一遍再覆盖。
#
#  谁写、谁读（边界必须清楚）
#  ---------------------------------------------------------------------------
#  写: **只有本模块**（全仓唯一的调用点就在这儿）。两个时机:
#      · `assistant tag --apply` —— 打标签（离线，要 NPU）
#      · 运行时推图成功 —— 只改 `used` / `last_used` 两格（T8-6）
#  读: Agent（标签索引 / 挑图 / 直连问句）与 `assistant tag` 自己（算增量）。
#  ⚠ 它住在 `config/` 里，但**不是**配置真源: `config.yaml` 才是人写的真源，
#    这个文件是机器派生数据 —— 手改它没有意义（下次打标签会覆盖），
#    所以它必须进 .gitignore（它是板端本地数据，不该入库）。
#
#  纯 Python（不 import numpy）
#  ---------------------------------------------------------------------------
#  float16 / float32 都用 `struct` 直接打包，解码用同一套 —— 这样标签索引与挑图
#  在开发机（没有 numpy）上也能跑，测试不必 skip。见 `decode_embedding()`。
# ============================================================================

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import struct
from datetime import datetime
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from agent.core.paths import resolve_config_path

__all__ = [
    "WallDataError",
    "RECORD_VERSION",
    "VOCAB_KIND",
    "VOCAB_RECORD_VERSION",
    "DEFAULT_DATA_FILE",
    "EMBED_DIM",
    "repo_root",
    "default_data_file",
    "resolve_data_file",
    "encode_vector",
    "decode_vector",
    "encode_embedding",
    "decode_embedding",
    "image_sha256",
    "make_record",
    "usage_of",
    "with_usage",
    "bump_usage",
    "bump_usage_in_file",
    "clear_usage_in_file",
    "usage_summary",
    "make_vocab_record",
    "vocab_matches",
    "vocab_vectors",
    "load",
    "read_records",
    "write_records",
    "tag_plan",
    "prune_records",
    "summarise",
]

_log = logging.getLogger(__name__)

#: 图片记录的格式版本（结构变了就 +1；读旧文件时按这个判"要不要重打"）
RECORD_VERSION = 1

#: 词表记录的类型标记与版本
VOCAB_KIND = "vocab"
VOCAB_RECORD_VERSION = 1

#: 默认数据文件（相对仓库根）—— 用户定的位置
DEFAULT_DATA_FILE = "config/wall_data.jsonl"

#: 向量维度（与 SigLIP 双塔一致；存进来是为了纯 CPU 检索）
EMBED_DIM = 768

#: 换行统一用 \n（JSONL 是给人/工具都能看的文本）
_NEWLINE = "\n"

#: struct 的格式字符: e = float16（半精度）, f = float32
_STRUCT_CODES = {"float16": "e", "float32": "f"}
_BYTES_PER = {"float16": 2, "float32": 4}


class WallDataError(ValueError):
    """数据文件读不了 / 写不了 / 格式不对。

    继承 ValueError，与仓库里其他"输入不对"的错误一致。
    """


# ---------------------------------------------------------------------------
#  路径
# ---------------------------------------------------------------------------
def repo_root() -> str:
    """仓库根目录（本文件在 agent/vision/ 下，往上两级）。"""
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def default_data_file() -> str:
    """默认数据文件的**绝对**路径（分隔符按平台规范化）。"""
    return os.path.normpath(os.path.join(repo_root(), DEFAULT_DATA_FILE))


def resolve_data_file(configured: Optional[str] = None) -> str:
    """把配置里的 `wallpaper.tagging.data_file` 解析成绝对路径。

    @note 相对路径按**仓库根**解析（不是当前工作目录）—— 这样 `assistant tag`
          在哪儿敲都一样；绝对路径原样用。
    @note 一律过 `os.path.normpath`：`os.path.join` 遇到带 `/` 的相对路径会混出
          `.../assitant\\config/wall_data.jsonl` 这种两种分隔符都在的串，
          打印/比对/写日志时都不好看。
    """
    return resolve_config_path(configured, default_data_file, repo_root())


# ---------------------------------------------------------------------------
#  向量编解码（float16 / float32 + base64；纯 struct，不依赖 numpy）
# ---------------------------------------------------------------------------
def encode_vector(vector: Sequence[float], dtype: str = "float16",
                  dim: int = EMBED_DIM) -> str:
    """(dim,) 浮点向量 → base64(按 dtype 打包)。

    @param dtype "float16"（图像向量，省体积）或 "float32"（词表向量，要精确）
    @raise WallDataError 维度不对 / dtype 不认识 / 值不是有限数
    """
    code = _STRUCT_CODES.get(str(dtype))
    if code is None:
        raise WallDataError("dtype 只支持 %s，实际 %r"
                            % ("/".join(sorted(_STRUCT_CODES)), dtype))
    values = [float(v) for v in vector]
    if len(values) != int(dim):
        raise WallDataError("向量维度应为 %d，实际 %d" % (dim, len(values)))
    for value in values:
        if value != value or value in (float("inf"), float("-inf")):
            raise WallDataError("向量里有非有限数（nan/inf）—— 不该写进数据文件")
    return base64.b64encode(struct.pack("<%d%s" % (len(values), code), *values)).decode("ascii")


def decode_vector(text: str, dtype: str = "float16", dim: int = EMBED_DIM) -> List[float]:
    """base64(按 dtype 打包) → [float × dim]。

    @note float32 往返是**逐位精确**的（同一个 float32 打包再解开还是它）——
          这正是词表向量用 float32 的原因: 复用缓存与现场重编码得到**完全相同**的分数。
    """
    code = _STRUCT_CODES.get(str(dtype))
    if code is None:
        raise WallDataError("dtype 只支持 %s，实际 %r"
                            % ("/".join(sorted(_STRUCT_CODES)), dtype))
    if not isinstance(text, str) or not text:
        raise WallDataError("向量字段必须是 base64 字符串")
    try:
        raw = base64.b64decode(text.encode("ascii"), validate=True)
    except Exception as exc:                       # noqa: BLE001
        raise WallDataError("向量不是合法的 base64: %s" % exc)
    want = int(dim) * _BYTES_PER[str(dtype)]
    if len(raw) != want:
        raise WallDataError("向量长度应为 %d 字节，实际 %d" % (want, len(raw)))
    return list(struct.unpack("<%d%s" % (int(dim), code), raw))


def encode_embedding(vector: Sequence[float]) -> str:
    """图像向量 → base64(float16)。"""
    return encode_vector(vector, dtype="float16", dim=EMBED_DIM)


def decode_embedding(text: str) -> List[float]:
    """base64(float16) → [float × 768]（图像向量）。"""
    return decode_vector(text, dtype="float16", dim=EMBED_DIM)


# ---------------------------------------------------------------------------
#  图像指纹
# ---------------------------------------------------------------------------
def image_sha256(path: str, chunk: int = 1 << 20) -> str:
    """图片内容的 sha256（分块读，4K 图也只占 1MB 缓冲）。"""
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as handle:
            while True:
                block = handle.read(chunk)
                if not block:
                    break
                digest.update(block)
    except OSError as exc:
        raise WallDataError("读不了图片 %s: %s" % (path, exc))
    return digest.hexdigest()


# ---------------------------------------------------------------------------
#  记录
# ---------------------------------------------------------------------------
def make_record(path: str, width: int, height: int, size: int, sha256: str, ms: int,
                model_sha8: str, vocab_sha8: str,
                tags: Mapping[str, Sequence[Sequence[Any]]],
                embedding: Sequence[float],
                used: int = 0, last_used: Optional[str] = None) -> Dict[str, Any]:
    """造一行记录（**schema 只在这里定义一次**）。

    @param tags {轴名: [[标签, 分数], ...]} —— 每轴 top-k，分数是余弦
    @param embedding L2 归一化后的图像向量（768 维）
    @param used     这张当壁纸显示过几次（T8-6；运行时由 `bump_usage()` 加）
    @param last_used 上次显示的时间（ISO 字符串）
    @note ⚠ `used` / `last_used` 是**运行时字段**，不是打标签字段: 重打一张图时
          调用方要用 `with_usage()` 把它们**带过去**，否则计数会被清零
          （`assistant tag` 已经这么做了）。
    """
    return {
        "version": RECORD_VERSION,
        "path": path,
        "w": int(width),
        "h": int(height),
        "bytes": int(size),
        "sha256": sha256,
        "tagged_at": datetime.now().isoformat(timespec="seconds"),
        "ms": int(ms),
        "model_sha8": model_sha8,
        "vocab_sha8": vocab_sha8,
        "tags": {axis: [[str(label), float(score)] for label, score in pairs]
                 for axis, pairs in (tags or {}).items()},
        "embedding": encode_embedding(embedding),
        "used": max(0, int(used or 0)),
        "last_used": str(last_used) if last_used else None,
    }


# ---------------------------------------------------------------------------
#  使用次数（T8-6: "这张壁纸被显示过几次" —— 挑"用得最少的"就靠它）
# ---------------------------------------------------------------------------
def usage_of(record: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    """读一条记录的使用次数（**缺字段 = 0/None** —— 老数据文件照样能用）。"""
    if not isinstance(record, Mapping):
        return {"used": 0, "last_used": None}
    try:
        used = max(0, int(record.get("used") or 0))
    except (TypeError, ValueError):
        used = 0
    last = record.get("last_used")
    return {"used": used, "last_used": str(last) if last else None}


def with_usage(record: Mapping[str, Any], usage: Mapping[str, Any]) -> Dict[str, Any]:
    """把使用次数写回一条记录（返回**副本**）—— 重打标签时用它保住计数。"""
    out = dict(record)
    out["used"], out["last_used"] = usage_of(usage)["used"], usage_of(usage)["last_used"]
    return out


def bump_usage(record: Mapping[str, Any], when: Optional[str] = None) -> Dict[str, Any]:
    """这张又显示了一次（返回**副本**，不改入参）。

    @param when 时间戳（默认现在）—— 测试要可控就传一个
    """
    out = dict(record)
    usage = usage_of(record)
    out["used"] = usage["used"] + 1
    out["last_used"] = str(when) if when else datetime.now().isoformat(timespec="seconds")
    return out


def bump_usage_in_file(path: str, image_path: str,
                       when: Optional[str] = None) -> Optional[int]:
    """把**某一张**的计数 +1 并整文件写回（原子写 + .bak）。

    @return 加完之后这张的计数；`None` = 文件里没有这张（**不写文件**，不是错误）
    @raise WallDataError 文件读不了 / 写不了
    @note 这是 T8-6 给运行时用的入口 —— 它仍然是"唯一写者"里的那条路
          （`write_records()`），所以数据文件不会出现两个写者各写一半。
    """
    loaded = load(path)
    wanted = str(image_path)
    bumped = False
    records: List[Mapping[str, Any]] = []
    result: Optional[int] = None
    for record in loaded["records"]:
        if str(record.get("path")) == wanted:
            record = bump_usage(record, when=when)
            result = int(record["used"])
            bumped = True
        records.append(record)
    if not bumped:
        return None
    write_records(path, records, vocab=loaded["vocab"])
    return result


def clear_usage_in_file(path: str, image_paths: Sequence[str]) -> int:
    """把这些图的 `used` / `last_used` **清零**并整文件写回（原子写 + .bak）。

    @return 真的清掉几条（0 = 这些图都不在数据文件里, **不写文件**）
    @raise WallDataError 文件读不了 / 写不了
    @note T9-2: 用户说"不想再看这个 IP 了"时用 —— 清 0 之后这些图会重新变成
          "用得最少的"，**下次挑最少的会先把它们挑出来**（这是你定的设计意图：
          清零就是把偏好归零、让它重新进候选），不是 bug。
    @note 仍然是"唯一写者"里的那条路（`write_records()`）。
    """
    wanted = {str(item) for item in (image_paths or ())}
    if not wanted:
        return 0
    loaded = load(path)
    cleared = 0
    records: List[Mapping[str, Any]] = []
    for record in loaded["records"]:
        if str(record.get("path")) in wanted and (usage_of(record)["used"]
                                                  or record.get("last_used")):
            record = with_usage(record, {"used": 0, "last_used": None})
            cleared += 1
        records.append(record)
    if not cleared:
        return 0
    write_records(path, records, vocab=loaded["vocab"])
    return cleared


def usage_summary(records: Sequence[Mapping[str, Any]], top: int = 3) -> Dict[str, Any]:
    """使用次数的摘要（给 `action="tags"` 回话用）。

    @return {"with_usage": n, "never_used": n, "total_used": n,
             "least_used": [{"path","used","last_used"}], "most_used": [...]}
    """
    entries = []
    for record in records:
        usage = usage_of(record)
        entries.append({"path": str(record.get("path")),
                        "used": usage["used"], "last_used": usage["last_used"]})
    ordered = sorted(entries, key=lambda item: (item["used"], item["path"]))
    limit = max(0, int(top))
    return {
        "with_usage": sum(1 for item in entries if item["used"] > 0),
        "never_used": sum(1 for item in entries if item["used"] == 0),
        "total_used": sum(item["used"] for item in entries),
        "least_used": ordered[:limit] if limit else ordered,
        "most_used": list(reversed(ordered[-limit:])) if limit else list(reversed(ordered)),
    }


def read_records(path: str) -> Tuple[List[Dict[str, Any]], List[str]]:
    """读数据文件里的**图片记录**（词表头记录自动跳过）。

    @return (记录列表, 问题列表)。**坏行不会让整个文件读不出来** —— 跳过它、记一句，
            因为"40 张里有一行坏了"不该让 Agent 完全看不到标签。
    @note 文件不存在 = 空表（还没打过标签），不是错误。
    @note 要连词表头一起拿就用 `load()`。
    """
    loaded = load(path)
    return loaded["records"], loaded["problems"]


def load(path: str) -> Dict[str, Any]:
    """读整个数据文件（词表头 + 图片记录 + 问题列表）。

    @return {"records": [...], "vocab": {...}|None, "problems": [...]}
    @note 词表头只认**第一条** `kind:"vocab"` 的行；再出现第二条会记一句问题
          （说明文件被人手改过或被拼接坏了），但用第一条。
    """
    if not os.path.exists(path):
        return {"records": [], "vocab": None, "problems": []}
    records: List[Dict[str, Any]] = []
    problems: List[str] = []
    vocab: Optional[Dict[str, Any]] = None
    try:
        with open(path, "r", encoding="utf-8") as handle:
            for lineno, line in enumerate(handle, 1):
                text = line.strip()
                if not text:
                    continue
                try:
                    item = json.loads(text)
                except ValueError as exc:
                    problems.append("第 %d 行不是合法 JSON: %s" % (lineno, exc))
                    continue
                if not isinstance(item, dict):
                    problems.append("第 %d 行不是对象，已跳过" % lineno)
                    continue
                if item.get("kind") == VOCAB_KIND:
                    if vocab is None:
                        vocab = item
                    else:
                        problems.append("第 %d 行是第二条词表记录（应当只有一条），已忽略"
                                        % lineno)
                    continue
                if not item.get("path"):
                    problems.append("第 %d 行缺 path 字段，已跳过" % lineno)
                    continue
                records.append(item)
    except OSError as exc:
        raise WallDataError("读不了数据文件 %s: %s" % (path, exc))
    return {"records": records, "vocab": vocab, "problems": problems}


def make_vocab_record(axes: Sequence[Any], embeds: Mapping[str, Sequence[Sequence[float]]],
                      model_sha8: str, vocab_sha8: str,
                      dim: int = EMBED_DIM) -> Dict[str, Any]:
    """造词表头记录（**第一行**）。

    @param axes   轴对象序列（只要有 `.name` 与 `.texts`）
    @param embeds {轴名: [向量, ...]}，顺序必须与 `axis.texts` 一一对应
    @raise WallDataError 轴/标签/向量数量对不上（宁可不写，也不写一份错位的缓存）
    """
    axis_labels: Dict[str, List[str]] = {}
    axis_embeds: Dict[str, List[str]] = {}
    for axis in axes:
        name = str(getattr(axis, "name"))
        labels = [str(text) for text in getattr(axis, "texts")]
        vectors = list((embeds or {}).get(name) or [])
        if len(vectors) != len(labels):
            raise WallDataError(
                "轴 %s 的标签 %d 条、向量 %d 条，对不上 —— 不写词表缓存"
                % (name, len(labels), len(vectors)))
        axis_labels[name] = labels
        axis_embeds[name] = [encode_vector(vec, dtype="float32", dim=dim) for vec in vectors]
    return {
        "kind": VOCAB_KIND,
        "version": VOCAB_RECORD_VERSION,
        "model_sha8": str(model_sha8),
        "vocab_sha8": str(vocab_sha8),
        "dim": int(dim),
        "axes": axis_labels,
        "embeds": axis_embeds,
    }


def vocab_matches(record: Optional[Mapping[str, Any]], model_sha8: str, vocab_sha8: str,
                  axes: Sequence[Any], dim: int = EMBED_DIM) -> bool:
    """这份词表缓存还能用吗?

    判据（缺一条都不用）: kind/版本对、模型指纹对、词表指纹对、维度对，
    并且**每个轴的标签列表逐条相等** —— 指纹只保证"词表内容没变"，
    但万一有人手改了头记录的 axes，这里能兜住。
    """
    if not isinstance(record, Mapping):
        return False
    if record.get("kind") != VOCAB_KIND:
        return False
    if int(record.get("version") or 0) != VOCAB_RECORD_VERSION:
        return False
    if int(record.get("dim") or 0) != int(dim):
        return False
    if str(record.get("model_sha8") or "") != str(model_sha8):
        return False
    if str(record.get("vocab_sha8") or "") != str(vocab_sha8):
        return False
    labels = record.get("axes")
    embeds = record.get("embeds")
    if not isinstance(labels, Mapping) or not isinstance(embeds, Mapping):
        return False
    for axis in axes:
        name = str(getattr(axis, "name"))
        want = [str(text) for text in getattr(axis, "texts")]
        if list(labels.get(name) or []) != want:
            return False
        if len(list(embeds.get(name) or [])) != len(want):
            return False
    return True


def vocab_vectors(record: Mapping[str, Any],
                  axes: Sequence[Any]) -> Optional[Dict[str, List[Tuple[str, List[float]]]]]:
    """把词表头解成 {轴名: [(标签, 向量), ...]}（给打标签器复用）。

    @return None = 解不出来（缺字段/base64 坏了）—— 调用方就该重新编码
    """
    embeds = record.get("embeds") if isinstance(record, Mapping) else None
    if not isinstance(embeds, Mapping):
        return None
    out: Dict[str, List[Tuple[str, List[float]]]] = {}
    for axis in axes:
        name = str(getattr(axis, "name"))
        vectors = list(embeds.get(name) or [])
        pairs: List[Tuple[str, List[float]]] = []
        for label, blob in zip([str(t) for t in getattr(axis, "texts")], vectors):
            try:
                pairs.append((label, decode_vector(blob, dtype="float32")))
            except WallDataError as exc:
                _log.warning("wall_data: 词表缓存里 %s/%s 解不开 (%s) —— 当作没有缓存",
                             name, label, exc)
                return None
        out[name] = pairs
    return out


def write_records(path: str, records: Sequence[Mapping[str, Any]],
                  vocab: Optional[Mapping[str, Any]] = None,
                  backup: bool = True) -> str:
    """整体重写数据文件（**原子写 + .bak**）。

    @param vocab 词表头记录（`make_vocab_record()` 造的）；给了就写在**第一行**
    @return 写进去的文本（测试与调用方确认用）
    @note 走 `agent/config.py::write_text_atomic` —— 全仓唯一的原子写实现
          （同目录临时文件 + os.replace）。40 张图的文件很小，整体重写最简单、
          也不需要"增量拼接"这种容易写坏的东西。
    """
    from ..config import write_text_atomic       # 延迟导入：本模块要能被单独测

    lines = []
    if vocab is not None:
        try:
            lines.append(json.dumps(vocab, ensure_ascii=False, sort_keys=False))
        except (TypeError, ValueError) as exc:
            raise WallDataError("词表记录无法序列化: %s" % exc)
    for record in records:
        try:
            lines.append(json.dumps(record, ensure_ascii=False, sort_keys=False))
        except (TypeError, ValueError) as exc:
            raise WallDataError("记录无法序列化: %s (%r)" % (exc, record.get("path")))
    text = _NEWLINE.join(lines) + (_NEWLINE if lines else "")
    if backup and os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as handle:
                old = handle.read()
            with open(path + ".bak", "w", encoding="utf-8") as handle:
                handle.write(old)
        except OSError as exc:                   # 备份失败不该阻止写入
            _log.warning("wall_data: 留备份失败 (已忽略): %r", exc)
    try:
        write_text_atomic(path, text, encoding="utf-8")
    except OSError as exc:
        raise WallDataError("写不了数据文件 %s: %s" % (path, exc))
    return text


# ---------------------------------------------------------------------------
#  增量计划
# ---------------------------------------------------------------------------
def tag_plan(records: Sequence[Mapping[str, Any]], images: Sequence[str],
             model_sha8: str, vocab_sha8: str,
             force: bool = False) -> Dict[str, Any]:
    """算出"哪些图要打、哪些已经最新、哪些行成了孤儿"。

    @param images 当前目录里的图片（绝对路径，顺序稳定）
    @return {"to_tag": [{"path","reason"}], "fresh": n, "orphans": [path],
             "unknown": [path]}
             · reason ∈ {"新图", "图变了", "模型变了", "词表变了", "格式变了", "强制重打"}
             · orphans = 数据文件里有、但目录里已经没有的图（`--prune` 才清）
    """
    by_path = {str(record.get("path")): record for record in records}
    wanted = list(images)
    wanted_set = set(wanted)
    to_tag: List[Dict[str, str]] = []
    fresh = 0
    unknown: List[str] = []

    for path in wanted:
        record = by_path.get(path)
        if record is None:
            to_tag.append({"path": path, "reason": "新图"})
            unknown.append(path)
            continue
        if force:
            to_tag.append({"path": path, "reason": "强制重打"})
            continue
        reason = _stale_reason(record, path, model_sha8, vocab_sha8)
        if reason is None:
            fresh += 1
        else:
            to_tag.append({"path": path, "reason": reason})

    orphans = [str(record.get("path")) for record in records
               if str(record.get("path")) not in wanted_set]
    return {"to_tag": to_tag, "fresh": fresh, "orphans": orphans, "unknown": unknown}


def _stale_reason(record: Mapping[str, Any], path: str,
                  model_sha8: str, vocab_sha8: str) -> Optional[str]:
    """这一行还能用吗? 不能用就返回原因（给人看的）。"""
    if int(record.get("version") or 0) != RECORD_VERSION:
        return "格式变了"
    if str(record.get("model_sha8") or "") != model_sha8:
        return "模型变了"
    if str(record.get("vocab_sha8") or "") != vocab_sha8:
        return "词表变了"
    try:
        if image_sha256(path) != record.get("sha256"):
            return "图变了"
    except WallDataError:
        return "图变了"
    if not record.get("embedding") or not record.get("tags"):
        return "格式变了"
    return None


def prune_records(records: Sequence[Mapping[str, Any]],
                  existing: Iterable[str]) -> Tuple[List[Mapping[str, Any]], List[str]]:
    """把"图已经不在了"的行去掉。

    @return (保留下来的记录, 丢掉的行)
    """
    keep = set(existing)
    kept: List[Mapping[str, Any]] = []
    dropped: List[str] = []
    for record in records:
        path = str(record.get("path"))
        if path in keep:
            kept.append(record)
        else:
            dropped.append(path)
    return kept, dropped


# ---------------------------------------------------------------------------
#  摘要
# ---------------------------------------------------------------------------
def summarise(records: Sequence[Mapping[str, Any]],
              axes: Sequence[str] = ()) -> Dict[str, Any]:
    """数据文件的摘要（`assistant tag` 与标签索引都用它）。

    @param axes 要统计的轴名（空 = 记录里出现的所有轴）
    @return {"count": n, "axes": {轴: {标签: 张数}}, "models": {...}, "vocabs": {...}}
    """
    counts: Dict[str, Dict[str, int]] = {}
    models: Dict[str, int] = {}
    vocabs: Dict[str, int] = {}
    for record in records:
        models[str(record.get("model_sha8"))] = models.get(str(record.get("model_sha8")), 0) + 1
        vocabs[str(record.get("vocab_sha8"))] = vocabs.get(str(record.get("vocab_sha8")), 0) + 1
        tags = record.get("tags") or {}
        if not isinstance(tags, Mapping):
            continue
        for axis, pairs in tags.items():
            if axes and axis not in axes:
                continue
            bucket = counts.setdefault(axis, {})
            for pair in pairs or []:
                if isinstance(pair, (list, tuple)) and pair:
                    label = str(pair[0])
                    bucket[label] = bucket.get(label, 0) + 1
    return {"count": len(records), "axes": counts, "models": models, "vocabs": vocabs}
