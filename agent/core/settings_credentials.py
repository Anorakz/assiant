# ============================================================================
#  agent/core/settings_credentials.py — **凭据文件**的读写（Phase 13 T13-8）
#
#  干什么: 把 B 站 cookie 写进 `config/bilibili_cookie.json`（或读出来看一眼）。
#
#  为什么单独一个模块（不塞进 settings_config）
#  ---------------------------------------------------------------------------
#    · 它写的**不是配置真源**，是**凭据**：格式是 JSON、内容只有几个键、
#      而且**绝不能回显**（`SESSDATA` 等于账号）。
#    · `settings_config` 的承诺是"只动 config.yaml 里那一行"; 凭据是另一件事
#      （整份文件、原子写、`.bak`）。混在一起会让两边的承诺都变模糊。
#
#  约定
#  ---------------------------------------------------------------------------
#    · 文件路径取 `config.yaml` 的 `bilibili.cookie_file`（默认 `config/bilibili_cookie.json`），
#      与 Agent 读的是**同一个键**（`bilibili_api` 那边也是它）。
#    · 已进 `.gitignore`（`.bak` 也在），**永远不要提交**。
#    · 键名照 B 站原样（`SESSDATA` / `bili_jct` / `DedeUserID`）—— 大小写敏感。
#      ⚠ 实测踩过：写成 `SEESSDATA`（多一个 E）B 站不认（`nav` 回 -101），
#        所以这里**只认这三个键**，别的键一律拒绝（不让人把笔误写进去）。
#    · 写盘: 先 `.bak` 再**原子写**（`write_text_atomic`）—— 凭据文件写坏了很烦。
# ============================================================================

from __future__ import annotations

import json
import os
import shutil
from typing import Any, Dict, Mapping, Optional

from agent.config import write_text_atomic

__all__ = ["CredentialsError", "ALLOWED_KEYS", "DEFAULT_COOKIE_FILE",
           "cookie_path", "read_cookie", "clean_values", "write_cookie"]

#: 只认这三个（B 站的键名, 大小写敏感）。
ALLOWED_KEYS = ("SESSDATA", "bili_jct", "DedeUserID")

#: 配置里没写 `bilibili.cookie_file` 时的默认位置（与 `agent/net/bilibili_api.py` 一致）。
DEFAULT_COOKIE_FILE = "config/bilibili_cookie.json"

BACKUP_SUFFIX = ".bak"


class CredentialsError(ValueError):
    """凭据文件的形状不对（消息给人看）。"""


def cookie_path(config: Optional[Mapping[str, Any]] = None) -> str:
    """配置里 `bilibili.cookie_file` 指的那个文件（相对路径按**仓库根**）。"""
    section = dict((config or {}).get("bilibili") or {})
    text = str(section.get("cookie_file") or "").strip() or DEFAULT_COOKIE_FILE
    if os.path.isabs(text):
        return os.path.normpath(text)
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    return os.path.normpath(os.path.join(root, text))


def read_cookie(config: Optional[Mapping[str, Any]] = None) -> Dict[str, str]:
    """读出凭据文件（不在 / 空的 / 坏的都是**空字典**，不是错误 —— 匿名是合法状态）。

    @note 读不出内容时**不抛**：Agent 那条路也把"没有 cookie"当成匿名。
    """
    path = cookie_path(config)
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, Mapping):
        return {}
    return {key: str(value) for key, value in data.items() if key in ALLOWED_KEYS and value}


def clean_values(values: Mapping[str, Any]) -> Dict[str, str]:
    """**只校验、不写盘**：把一次 credentials 改动归一成"要写的那几个键"。

    给 T14-2 的 IPC 处理器用：它要先确认凭据合法，再决定要不要动配置真源 ——
    一次性写两处（真源 + 凭据）时，"先校验后写"比"写一半发现键写错了"好。

    @return 要写的键（空值被丢掉 = 不改动那个键）；输入里没有可写的键时返回 {}
    @raise CredentialsError 键不认识 / 值不是字符串 / 值里有换行
    """
    cleaned: Dict[str, str] = {}
    for key, value in (values or {}).items():
        name = str(key)
        if name not in ALLOWED_KEYS:
            raise CredentialsError("不认识的键 %r（只认 %s —— 笔误会被 B 站当成没登录）"
                                   % (key, " / ".join(ALLOWED_KEYS)))
        if value is not None and not isinstance(value, str):
            raise CredentialsError("值必须是字符串（%s 收到 %s）"
                                   % (name, type(value).__name__))
        text = str(value or "").strip()
        if not text:
            continue
        if "\n" in text or "\r" in text:
            raise CredentialsError("值不能换行（凭据是一行的）")
        cleaned[name] = text
    return cleaned


def write_cookie(config: Optional[Mapping[str, Any]], values: Mapping[str, Any]) -> str:
    """把凭据写进文件（**合并**已有内容, 不认识的键拒绝）。

    @return 写进去的文件路径
    @raise CredentialsError 键不认识 / 值不是字符串 / 一个可写的值都没给 / 写不进去
    """
    cleaned = clean_values(values)
    if not cleaned:
        raise CredentialsError("没给任何值（要写至少一个: %s）" % " / ".join(ALLOWED_KEYS))
    path = cookie_path(config)
    merged = dict(read_cookie(config))                   # 只给 SESSDATA 时别把别的键抹掉
    merged.update(cleaned)
    text = json.dumps({key: merged[key] for key in ALLOWED_KEYS if key in merged},
                      ensure_ascii=False, indent=2) + "\n"
    try:
        if os.path.exists(path):
            shutil.copy2(path, path + BACKUP_SUFFIX)
        write_text_atomic(path, text)
    except OSError as exc:
        raise CredentialsError("写不进去（%s）: %s" % (path, exc)) from exc
    return path
