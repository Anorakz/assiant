# ============================================================================
#  agent/core/llm_env.py — **派生** llm/config/llm.env（T14-2）
#
#  干什么: 按**唯一真源** `config/config.yaml` 的 `llm:` 段，重写 `llm/config/llm.env`
#          里那 8 行 `LLM_*=…`（只动目标行，注释/空行/顺序/行尾换行状态逐字节保留，
#          写之前在原文件旁留一份 `.bak`）。
#
#  为什么从 C++ 搬到这里（T14-2 的决定，见 docs/adr/0005）
#  ---------------------------------------------------------------------------
#    · `llm.env` 是**派生文件**（喂 llama-server），而 T14 起 **GUI 不再自己写任何东西**：
#      GUI 把"要改哪些键"交给 Agent，Agent 一次把「真源 + 派生」都落到位。
#    · 原来这份映射表的唯一实现是 `gui/src/core/config_sync.cpp`；`gui_config_sync`
#      那个 CLI 与 `assistant doctor` 都靠它。既然写入者只剩 Agent 一个，实现也只剩
#      这一份（Python）—— 两个语言各写一遍同一个映射表，迟早会漂。
#    · ⚠ 本模块**只写不读**：它绝不把 llm.env 当配置来源（那是归一化 D 系列清掉的
#      老毛病）。这一点由 `tests/test_config_source_guard.py` 机械守着。
#
#  映射表（8 个键，与 `gui/src/core/config_sync.cpp::buildEnv` 逐条对齐）
#  ---------------------------------------------------------------------------
#      LLM_MODEL_PATH      <- llm.model_path
#      LLM_MODEL_NAME      <- llm.model_name
#      LLM_PORT            <- llm.port
#      LLM_CTX_SIZE        <- llm.ctx_size
#      LLM_BATCH_SIZE      <- llm.batch_size
#      LLM_THREADS         <- llm.threads
#      LLM_THREADS_BATCH   <- llm.threads_batch
#      LLM_API_KEY         <- llm.local_api_key      ⚠ 是 local_api_key，不是 api_key
#
#  其余键（`LLM_HOST` / `LLM_LOG_DIR` / `LLM_RUN_DIR` / `LLM_PID_FILE` / `LLM_LOG_FILE`）
#  由板端自己维护，派生**不碰**。
#
#  与 C++ 那版的两处**刻意不同**（都是"更少破坏"的方向）
#  ---------------------------------------------------------------------------
#    1. 行尾注释保留：`LLM_PORT=9000   # 注释` 改完还是 `LLM_PORT=9000   # 注释`。
#       C++ 那版的 Env 分支会把 `=` 之后整段重写（注释没了）。板端真实那份 llm.env
#       没有行内注释，所以这是"顺手修好"，不是行为回归。
#    2. 缺键**追加**在文件末尾（与 C++ 一致），但**不动**已有行的顺序、也不重排注释。
#
#  谁用它: `agent/ipc/` 的 `set_config` 处理器（GUI 保存设置那条路）与
#          `assistant doctor` 的"派生一致性"检查。
# ============================================================================

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple, Union

from agent.config import config_dir, read_config_file, write_text_atomic

__all__ = [
    "LlmEnvError",
    "LLM_ENV_KEYS",
    "DEFAULT_ENV_RELATIVE",
    "default_env_path",
    "read_env_value",
    "plan",
    "apply",
    "sync_from_config",
]

#: 备份后缀（与 ConfigStore / settings_config 同一约定；`*.bak` 已被 .gitignore 覆盖）
BACKUP_SUFFIX = ".bak"

#: 派生文件相对**仓库根**的默认位置（与 `gui/src/core/config_sync.cpp` 的
#: `llm/config/llm.env`、`gui_config_sync --env` 的默认值一致）。
DEFAULT_ENV_RELATIVE = os.path.join("llm", "config", "llm.env")

#: (llm.env 里的键, config.yaml 里的点号路径) —— **顺序就是 C++ 那版的顺序**。
LLM_ENV_KEYS: Tuple[Tuple[str, str], ...] = (
    ("LLM_MODEL_PATH", "llm.model_path"),
    ("LLM_MODEL_NAME", "llm.model_name"),
    ("LLM_PORT", "llm.port"),
    ("LLM_CTX_SIZE", "llm.ctx_size"),
    ("LLM_BATCH_SIZE", "llm.batch_size"),
    ("LLM_THREADS", "llm.threads"),
    ("LLM_THREADS_BATCH", "llm.threads_batch"),
    ("LLM_API_KEY", "llm.local_api_key"),
)


class LlmEnvError(ValueError):
    """派生 llm.env 失败（消息给人看；继承 ValueError 与其它模块一致）。"""


# ---------------------------------------------------------------------------
#  文本工具（与 settings_config / schedule_config 同一套口径：逐字节读、保留行尾）
# ---------------------------------------------------------------------------
def _read_text(path: Path) -> str:
    """**逐字节**读（不做换行翻译）—— 否则"读出来再写回去"会把 CRLF 悄悄改成 LF。"""
    with open(str(path), "r", encoding="utf-8", newline="") as handle:
        return handle.read()


def _strip_body(line: str) -> str:
    """去掉行尾（`\\n` / `\\r\\n`），留着内容。"""
    return line[:-2] if line.endswith("\r\n") else (line[:-1] if line.endswith("\n") else line)


def _split_inline_comment(raw: str) -> Tuple[str, str]:
    """`9000   # 注释` -> (`9000   `, `# 注释`)；没有注释时第二段是空串。

    ⚠ 只在"空白 + #"处切 —— 与 `settings_config._split_value` 同一个口径
      （值里带 `#` 不算注释，例如 URL 的 fragment）。
    """
    for index in range(1, len(raw)):
        if raw[index] == "#" and raw[index - 1] in (" ", "\t"):
            start = index - 1
            while start > 0 and raw[start - 1] in (" ", "\t"):
                start -= 1
            return raw[:start], raw[start:]
    return raw, ""


def _scalar_to_text(value: Any) -> str:
    """配置里的值 -> llm.env 里那一行的文本（与 C++ 读原始 YAML 文本等价）。"""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        text = ("%g" % value)
        return text
    return str(value)


def _key_of(line: str) -> Optional[str]:
    """`LLM_PORT=9000` -> `LLM_PORT`；不是 `KEY=…` 形状返回 None。"""
    body = _strip_body(line)
    stripped = body.strip()
    if not stripped or stripped.startswith("#"):
        return None
    eq = stripped.find("=")
    if eq <= 0:
        return None
    return stripped[:eq].strip()


# ---------------------------------------------------------------------------
#  路径
# ---------------------------------------------------------------------------
def default_env_path(config_path: Optional[Union[str, Path]] = None) -> Path:
    """派生文件放哪。

    @param config_path 传了就按它推：`<config.yaml 的父目录>/../llm/config/llm.env`
                       —— 与 GUI 的 `repoRoot_`（config 目录的上一级）同一算法；
                       不传就按 `agent.config.config_dir()` 的上一级（仓库根）。
    """
    if config_path:
        return Path(config_path).resolve().parents[1] / DEFAULT_ENV_RELATIVE
    return Path(config_dir()).resolve().parents[0] / DEFAULT_ENV_RELATIVE


# ---------------------------------------------------------------------------
#  读 / 计划 / 写
# ---------------------------------------------------------------------------
def read_env_value(env_key: str, env_path: Optional[Union[str, Path]] = None,
                   config_path: Optional[Union[str, Path]] = None) -> Optional[str]:
    """读 llm.env 里某个键现在的值（读不到返回 None）。只给诊断/测试用。"""
    path = Path(env_path) if env_path else default_env_path(config_path)
    if not path.is_file():
        return None
    for line in _read_text(path).splitlines():
        if _key_of(line) == env_key:
            body = _strip_body(line)
            return body[body.find("=") + 1:].strip()
    return None


def _values_from_config(config_path: Optional[Union[str, Path]] = None) -> Dict[str, str]:
    """从真源里取那 8 个键的值（**没有的键不出现** —— 派生就不动它）。"""
    path = Path(config_path) if config_path else Path(config_dir()) / "config.yaml"
    if not path.is_file():
        raise LlmEnvError("读不到真源 %s（派生 llm.env 要以它为准）" % path)
    try:
        data = read_config_file(path)
    except Exception as exc:                         # noqa: BLE001 - ConfigError 及 YAML 错
        raise LlmEnvError("真源读不出来（%s）：%s" % (path, exc)) from exc
    section = data.get("llm") if isinstance(data, Mapping) else None
    if not isinstance(section, Mapping):
        raise LlmEnvError("%s 里没有 llm: 段（派生 llm.env 要有它）" % path)
    out: Dict[str, str] = {}
    for env_key, dotted in LLM_ENV_KEYS:
        name = dotted.split(".", 1)[1]
        if name not in section:
            continue                                  # 真源里没写 -> 不动 llm.env 那一行
        value = section[name]
        if value is None:
            continue                                  # `key:`（空值）在 C++ 那版是"容器"，跳过
        out[env_key] = _scalar_to_text(value)
    return out


def plan(config_path: Optional[Union[str, Path]] = None,
         env_path: Optional[Union[str, Path]] = None) -> List[Dict[str, Any]]:
    """算出这次会改哪些行（**不写文件**）。

    @return 每条改动 `{"key", "line", "old", "new"}`；没有差异就是空列表
    @raise LlmEnvError 真源读不到 / llm.env 不存在
    @note 缺的键**追加**到文件末尾（与 C++ 那版的 Env 分支一致）。
    """
    values = _values_from_config(config_path)
    path = Path(env_path) if env_path else default_env_path(config_path)
    if not path.is_file():
        raise LlmEnvError("llm.env 不在: %s（派生文件要先把模板/上一次那份放到位）" % path)

    lines = _read_text(path).splitlines(keepends=True)
    found: Dict[str, int] = {}
    for index, line in enumerate(lines):
        key = _key_of(line)
        if key is not None and key in values and key not in found:
            found[key] = index

    plans: List[Dict[str, Any]] = []
    for env_key, _dotted in LLM_ENV_KEYS:
        if env_key not in values:
            continue
        want = values[env_key]
        if env_key in found:
            index = found[env_key]
            body = _strip_body(lines[index])
            value_part, comment = _split_inline_comment(body[body.find("=") + 1:])
            current = value_part.strip()
            if current == want:
                continue
            # ⚠ 分隔符是 `=` 本身：**不加空格**（llm.env 的写法是 `LLM_PORT=9000`）。
            #   原来 `=` 后面有多少空白就留多少（通常一个都没有）——
            #   早先这里默认补一个空格，写出来变成 `LLM_PORT= 9100`（测试抓住的）。
            lead = value_part[:len(value_part) - len(current)]
            head = body[:body.find("=") + 1]
            plans.append({"key": env_key, "line": index + 1, "old": current, "new": want,
                          "text": head + lead + want + comment})
        else:
            plans.append({"key": env_key, "line": len(lines) + 1, "old": None, "new": want,
                          "text": "%s=%s" % (env_key, want)})
    return plans


def apply(config_path: Optional[Union[str, Path]] = None,
          env_path: Optional[Union[str, Path]] = None) -> Dict[str, Any]:
    """按计划真写（原文件旁 `.bak` + **原子写**）。

    @return `{"plans", "changed", "backup", "path"}`
    @note 一条都没改 -> **不写文件、不留 `.bak`**（与 settings_config 同一取舍）。
    @note 逐字节保留：注释 / 空行 / 顺序 / 行尾（含"原来没有尾换行"这件事）。
    """
    path = Path(env_path) if env_path else default_env_path(config_path)
    plans = plan(config_path=config_path, env_path=path)
    if not plans:
        return {"plans": [], "changed": 0, "backup": "", "path": str(path)}

    text = _read_text(path)
    lines = text.splitlines(keepends=True)
    appended: List[str] = []
    for item in plans:
        if item["line"] <= len(lines):
            index = item["line"] - 1
            body = _strip_body(lines[index])
            ending = lines[index][len(body):] or ""
            lines[index] = item["text"] + ending
        else:
            appended.append(item["text"] + "\n")
    new_text = "".join(lines) + "".join(appended)

    backup = str(path) + BACKUP_SUFFIX
    try:
        shutil.copy2(str(path), backup)
        write_text_atomic(path, new_text)
    except OSError as exc:
        raise LlmEnvError("写不进去（%s）: %s" % (path, exc)) from exc
    return {"plans": plans, "changed": len(plans), "backup": backup, "path": str(path)}


def sync_from_config(config_path: Optional[Union[str, Path]] = None,
                     env_path: Optional[Union[str, Path]] = None) -> Dict[str, Any]:
    """`apply()` 的别名（语义：从真源同步到派生文件）。"""
    return apply(config_path=config_path, env_path=env_path)
