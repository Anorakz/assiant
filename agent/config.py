# ============================================================================
#  agent/config.py — YAML 配置加载 / 保存 / 点号路径读取
#
#  设计目标（刻意做小）
#  ---------------------------------------------------------------------------
#  · 只做四件事: 读盘、写盘、缓存、按 "a.b.c" 取值
#  · 配置名走 **白名单** (ALLOWED_CONFIGS): 只认本文件里列出的几个名字。
#    黑名单只能挡住想得到的坏输入, 白名单是"只认这几个", 漏掉的自动被拒,
#    失败方向是安全的, 也顺带免疫目录穿越。
#  · **不做** schema 校验 —— 各模块自己校验自己的字段 (配置项是各模块的私有约定,
#    集中校验会把所有模块的字段名耦合到这里, 改一个字段要动两个地方)
#  · **不做** 热重载 —— 要生效就重新 load_config(); 显式优于隐式
#  · **不做** 环境变量替换 —— 配置里写什么就是什么 (唯一例外见下面 CONFIG_DIR_ENV,
#    那是"配置目录在哪", 不是"配置内容替换")
#
#  文件与目录
#  ---------------------------------------------------------------------------
#      config/config.yaml              # 全局: llm.mode, sunshine.*, ipc.*, scheduler.*
#      config/user_profile.yaml        # 用户画像
#
#  · 日程**不是**独立配置: 它住在 config.yaml 的 `scheduler:` 段。原先那份
#    `config/schedule.example.yaml` 全仓没人读, L1 已删除 —— 所以白名单里
#    不再有 "schedule" 这个名字。
#  · 真实 *.yaml 不入 git; 仓库里只放 *.example.yaml 模板
#  · load_config(name) 先找 <name>.yaml, 没有就退回 <name>.example.yaml,
#    所以"刚 clone 下来没建配置"也能跑起来(用模板默认值)
#  · save_config(name) 永远写 <name>.yaml (绝不覆盖 .example.yaml —— 那是模板)
#
#  缓存语义
#  ---------------------------------------------------------------------------
#  · 缓存 key = 白名单里的 base name (已剥掉 .yaml / .example.yaml);
#    命中就完全不碰磁盘
#  · load_config 返回的是**深拷贝**: 调用方改返回值不会污染缓存,
#    也就不会出现"内存里改了但没落盘"的静默不一致
#  · save_config 写盘成功后**同步更新缓存**, 保证下一次 get() 立刻看到新值
#  · clear_cache() 清空缓存 (主要给测试用; 生产代码一般不需要)
#
#  与板端的关系
#  ---------------------------------------------------------------------------
#  纯标准库 + PyYAML, 没有第三方依赖, 板端 Python 3.8 可直接跑。
# ============================================================================

from __future__ import annotations

import copy
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional, Tuple, Union

try:
    import yaml
except ImportError as _exc:  # pragma: no cover - 环境问题, 不是逻辑分支
    # 明确说是缺依赖, 而不是抛一个裸 ModuleNotFoundError: yaml
    raise ImportError(
        "agent.config 需要 PyYAML。安装: pip3 install pyyaml "
        "(板端 Ubuntu 20.04 也可以: apt install python3-yaml)"
    ) from _exc

__all__ = [
    "ConfigError",
    "ConfigNotFoundError",
    "load_config",
    "save_config",
    "write_text_atomic",
    "get",
    "config_path",
    "config_dir",
    "clear_cache",
    "DEFAULT_CONFIG_NAME",
    "ALLOWED_CONFIGS",
    "CONFIG_DIR_ENV",
]

# ---------------------------------------------------------------- 位置解析 ---

#: get() 默认读取的配置名
DEFAULT_CONFIG_NAME = "config"

#: 可选的配置目录覆盖。注意这**不是**配置内容的环境变量替换,
#: 只是"去哪儿找文件" —— 板端把配置放在 /etc/agent 之类的位置时很有用。
CONFIG_DIR_ENV = "AGENT_CONFIG_DIR"

#: 允许的配置名**白名单** (base name, 不含 .yaml)。
#:
#: 为什么用白名单而不是黑名单:
#:   · 黑名单(挡 ".."、"a/b"、绝对路径)只能挡住**想得到的**坏输入, 漏一个就是
#:     目录穿越; 白名单是"只认这几个", 漏掉的自动被拒, 失败方向是安全的。
#:   · 配置名是有限的、由本仓库自己定义的集合, 本来就不该是任意字符串。
#: 新增配置时在这里加一个名字, 并同步加 config/<name>.example.yaml 模板。
#: (曾经的 "schedule" 已随 L1 删除: 日程住在 config.yaml 的 scheduler 段。)
ALLOWED_CONFIGS = ("config", "user_profile")

#: 默认配置目录 = 仓库根下的 config/
#:   本文件在 <root>/agent/config.py, 所以 parents[1] 就是 <root>
_DEFAULT_CONFIG_DIR = Path(__file__).resolve().parents[1] / "config"

_REAL_SUFFIX = ".yaml"
_EXAMPLE_SUFFIX = ".example.yaml"
_EXAMPLE_MARK = ".example"

#: 已加载配置: 配置名 -> dict
#: key 用配置名而不是绝对路径, 是因为**存在的那个文件**可能从 .yaml 变成
#: .example.yaml (或反过来), 用路径做 key 会让缓存出现两份同内容的条目。
_CACHE: Dict[str, Dict[str, Any]] = {}


class ConfigError(ValueError):
    """配置层错误基类。

    继承 ValueError 是刻意的: 参数不对(类型错、路径穿越)本质上就是 ValueError,
    调用方既可以用 `except ConfigError` 精确捕获, 也可以用 `except ValueError`
    统一处理。如果是裸 Exception, 这两种写法就只剩一种能用。
    """


class ConfigNotFoundError(ConfigError):
    """既没有 <name>.yaml 也没有 <name>.example.yaml。"""


# ------------------------------------------------------------------ 内部 ---


def config_dir() -> Path:
    """返回当前生效的配置目录 (不创建、不校验存在性)。

    每次调用都重新解析 $AGENT_CONFIG_DIR, 这样测试改了环境变量立刻生效,
    不需要额外的 setter。
    """
    override = os.environ.get(CONFIG_DIR_ENV)
    if override:
        return Path(override).expanduser()
    return _DEFAULT_CONFIG_DIR


def _normalize_name(name: str) -> str:
    """把配置名规范化成白名单里的 base name。

    接受这几种写法, 都归一到同一个 base name:
        "config"                -> "config"
        "config.yaml"           -> "config"
        "config.example.yaml"   -> "config"   (显式要模板也是同一个配置)
    然后必须落在 ALLOWED_CONFIGS 里。

    @note 白名单顺带解决了目录穿越: "../secret"、"a/b"、"/etc/passwd" 都不可能
          在 ALLOWED_CONFIGS 里, 所以不需要再单独写一堆字符检查。
    """
    if not isinstance(name, str):
        raise ConfigError("config name must be a str, got %s" % type(name).__name__)

    stem = name.strip()

    # 依次剥掉 .yaml 与 .example —— 顺序固定, 所以 "x.example.yaml" 两步都能剥到
    if stem.endswith(_REAL_SUFFIX):
        stem = stem[: -len(_REAL_SUFFIX)]
    if stem.endswith(_EXAMPLE_MARK):
        stem = stem[: -len(_EXAMPLE_MARK)]

    if stem not in ALLOWED_CONFIGS:
        raise ConfigError(
            "unknown config name %r; allowed: %s"
            % (name, ", ".join(ALLOWED_CONFIGS))
        )

    return stem


def _resolve(name: str) -> Tuple[str, Path, bool]:
    """定位配置名对应的文件。

    @return (stem, path, is_example)
    @raise ConfigNotFoundError 两个候选都不存在
    """
    stem = _normalize_name(name)
    base = config_dir()

    real = base / (stem + _REAL_SUFFIX)
    if real.is_file():
        return stem, real, False

    example = base / (stem + _EXAMPLE_SUFFIX)
    if example.is_file():
        return stem, example, True

    raise ConfigNotFoundError(
        "config %r not found: neither %s nor %s exists"
        % (stem, real, example)
    )


def _read_yaml(path: Path) -> Dict[str, Any]:
    """读 YAML 并要求顶层是 mapping。

    · 空文件 (或只有注释/空白的文件) 直接报错: 那几乎总是"模板没复制好"或
      "写盘被中断", 静默当成 {} 会让缺配置的故障延后暴露在很远的地方。
    · 顶层是 list/scalar 也直接报错 —— 后面的点号路径 get("a.b") 隐含假设了
      顶层是 dict, 放任它到 get() 才炸会让报错位置离现场很远。
    """
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = fh.read()
    except OSError as exc:
        raise ConfigError("cannot read config %s: %s" % (path, exc)) from exc

    if not raw.strip():
        raise ConfigError("config %s is empty" % path)

    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise ConfigError("invalid YAML in %s: %s" % (path, exc)) from exc

    # 有内容但解析出 None: 例如整份文件都是注释
    if data is None:
        raise ConfigError("config %s has no content (only comments/blank lines)" % path)

    if not isinstance(data, dict):
        raise ConfigError(
            "config %s must contain a mapping at the top level, got %s"
            % (path, type(data).__name__)
        )
    return data


# ------------------------------------------------------------------ 公开 ---


def load_config(name: str = DEFAULT_CONFIG_NAME) -> Dict[str, Any]:
    """加载配置并缓存。

    先找 <name>.yaml, 再退回 <name>.example.yaml。命中缓存时**不读盘**。

    @param name 白名单 (ALLOWED_CONFIGS) 里的配置名; 也接受 "x.yaml" /
                "x.example.yaml" 写法, 会归一到同一个配置
    @return 配置的**深拷贝** (改它不会污染缓存)
    @raise ConfigError         配置名不在白名单 / 文件为空 / YAML 非法 /
                               顶层不是 mapping
    @raise ConfigNotFoundError 白名单内, 但两个候选文件都不存在 (ConfigError 子类)
    """
    stem = _normalize_name(name)

    cached = _CACHE.get(stem)
    if cached is not None:
        return copy.deepcopy(cached)

    _, path, _ = _resolve(stem)
    data = _read_yaml(path)
    _CACHE[stem] = data
    return copy.deepcopy(data)


def write_text_atomic(path: Union[str, Path], text: str, encoding: str = "utf-8") -> None:
    """把文本**原子地**写进 path（同目录临时文件 + `os.replace`）。

    @note 全仓只有这一处实现"原子写文本"：`save_config()` 与核心侧的"删掉已触发的
          一次性日程"（agent/core/schedule_config.py）都走它 —— "临时文件必须落在目标
          同目录（同一文件系统才能原子换入）"这条细节只写一遍。
    @note ⚠ `newline=""`：**逐字节**写进去，不做换行翻译。文本模式默认会把 `\n` 翻成
          `os.linesep`，于是同一个 CRLF 文本在 Linux 上被写成 LF、在 Windows 上被写成
          `\r\r\n` —— 而"文本级改配置"要求的是"除了那几行，其余字节不变"（T12-5 板端实测）。
    @raise OSError 写盘失败（权限、磁盘满……）—— 原样抛出, 不吞
    """
    target = Path(path)
    base = target.parent
    base.mkdir(parents=True, exist_ok=True)

    fd, tmp_name = tempfile.mkstemp(prefix=".%s." % target.name, suffix=".tmp", dir=str(base))
    try:
        with os.fdopen(fd, "w", encoding=encoding, newline="") as fh:
            fh.write(text)
        os.replace(tmp_name, target)
    except BaseException:
        # 失败时别把临时文件留在目录里
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def save_config(name: str, data: Dict[str, Any]) -> None:
    """把配置写到 <name>.yaml, 并同步刷新缓存。

    · 只写真实文件名, **不会**覆盖 .example.yaml (模板必须保持干净)
    · 先写**目标同目录**下的临时文件, 再 os.replace 原子换入:
        - mkstemp(dir=<配置目录>) 保证临时文件与目标是同一文件系统,
          否则 os.replace 会抛 OSError(EXDEV) 而退化成非原子操作
        - 同目录还保证"换入"这一步只改目录项, 不会跨盘复制
      (写到一半掉电时, 目标文件要么是旧的完整内容, 要么是新的完整内容)
    · 目录不存在会自动创建

    @param name 白名单 (ALLOWED_CONFIGS) 里的配置名
    @raise ConfigError data 不是 mapping, 或配置名不在白名单
    @raise OSError     写盘失败 (权限、磁盘满……) —— 原样抛出, 不吞
    """
    stem = _normalize_name(name)

    if not isinstance(data, dict):
        raise ConfigError(
            "save_config(%r) expects a mapping, got %s" % (stem, type(data).__name__)
        )

    base = config_dir()
    base.mkdir(parents=True, exist_ok=True)
    target = base / (stem + _REAL_SUFFIX)

    # sort_keys=False: 保持调用方给的顺序, 落盘后的可读性和 example 模板一致
    text = yaml.safe_dump(data, allow_unicode=True, sort_keys=False, default_flow_style=False)
    write_text_atomic(target, text)

    # 写盘成功才更新缓存 —— 失败时缓存保持旧值, 与磁盘一致
    _CACHE[stem] = copy.deepcopy(data)


def get(key: str, default: Any = None) -> Any:
    """按 "sunshine.host" 这样的点号路径从**默认配置**里取值。

    · 中间层不是 mapping (例如 llm 是字符串却取 llm.mode) -> 返回 default
    · 任何一层缺 key -> 返回 default
    · 配置文件不存在 -> 返回 default (而不是抛异常: 读一个"可选的"配置项
      不该让调用方到处写 try/except)

    @note 只能读 DEFAULT_CONFIG_NAME 这个配置。要读别的配置请先 load_config("x"),
          再自己按 key 取值 —— get() 的签名里没有配置名, 这是有意的简化。
    """
    try:
        data: Any = load_config(DEFAULT_CONFIG_NAME)
    except ConfigNotFoundError:
        return default

    if not isinstance(key, str) or not key:
        return default

    for part in key.split("."):
        if not part:
            return default
        if not isinstance(data, dict):
            return default
        if part not in data:
            return default
        data = data[part]

    return data


def config_path(name: str = DEFAULT_CONFIG_NAME) -> Path:
    """返回配置名实际会用到的文件路径 (已存在的那种)。

    先看 <name>.yaml, 再看 <name>.example.yaml。
    @raise ConfigNotFoundError 两个都不存在
    """
    _, path, _ = _resolve(name)
    return path


def clear_cache() -> None:
    """清空缓存。下次 load_config 会重新读盘。

    主要给测试与"我知道文件被外部改了"的场景用; 正常运行不需要。
    """
    _CACHE.clear()
