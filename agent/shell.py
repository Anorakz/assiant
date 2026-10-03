# -*- coding: utf-8 -*-
"""agent/shell.py —— `assistant shell`：一个很小的交互式会话（T15-4 任务 2）

它只做三件事:

  · 读一行 -> 用**同一套** argparse 解析（`agent.cli.build_parser()`）-> 执行
    （`args.func(args)`）—— 不另造一套命令语法, 一行命令与今天一次性调用的执行路径**逐字相同**;
  · 会话内的**权限层**: `mode root` 进 root 级、`mode user` 降回、`exit` 退出会话;
  · 把当前层塞进 `args.tier`（`cmd_set` 靠它决定"这一项能不能改"）。

⚠ 为什么要有会话: 用户定的"root 体系用显式的 `mode root` 进、`exit` 退"需要一个**跨命令的
   状态**。CLI 其它命令都是一次性进程（一个进程一条命令），所以这个状态只能活在会话里 ——
   而且**不落任何文件**（进程一退就没了, 不存在"忘了退出"的残留, 也没有可被伪造的状态文件）。

⚠ 与 Agent 模式的关系: `assistant mode <SLEEP|IDLE|STUDY|GAME>` 照旧是**切 Agent 模式**;
   会话里另外认 `mode root` / `mode user` 当**权限层**切换（`root`/`user` 不是合法的 Agent
   模式, 所以机械上不会歧义; 文档里也写清了这一点）。

⚠ 为什么 `mode root` 还要看 euid: 板上人人都是 root（`BR2_TARGET_GENERIC_ROOT_PASSWD`，
   agent/gui 服务也 `User=root`），所以这道門**不是安全边界**, 只是"意图确认 + 别让界面/
   脚本乱改"。留着 euid 判断是为了将来真出现非 root 登录时行为正确（那时它才是有用的門）。

⚠ 为什么这里自己跑 `asyncio.run` 而不是让 `cli.main` 统一 await: 会话要一条一条地跑命令,
   而 `asyncio.run` **不能嵌套**。所以 `cli.main` 对 `shell` 走**同步早分支**（见那里的注释）,
   每条命令在这里各自 `asyncio.run(...)` —— 与一次性调用完全同一条路。
"""
from __future__ import annotations

import asyncio
import os
import shlex
import sys
from typing import Callable, Iterable, List, Optional, Sequence, TextIO, Tuple

from agent.core import config_tiers

__all__ = ["Session", "PROMPT", "ROOT_PROMPT_NOTE", "run_line", "run_lines", "main"]

#: 提示符（层级一眼可见）
PROMPT = "assistant(%s)> "
#: 进 root 层时那句提示
ROOT_PROMPT_NOTE = ("⚠ 已进入 root 体系（**只在本会话有效**）：root 级设置项现在可以改。"
                    "改完记得 `mode user` 或 `exit`。")
#: 退出会话的词（用户定的那个）
EXIT_WORDS = ("exit", "quit")
#: 权限层的两个取值（会话内 `mode <值>`）
TIER_WORDS = ("root", "user")


class Session(object):
    """会话状态（目前只有权限层 + 用来判 euid 的口子）。

    @param tier 当前权限层（默认 user）
    @param euid 判"是不是 root"用；`None` = 问操作系统（测试里注入 0/1000，不真降权）
    """

    def __init__(self, tier: str = config_tiers.USER_TIER, *,
                 euid: Optional[int] = None) -> None:
        self.tier = str(tier)
        self._euid = euid

    @property
    def is_root(self) -> bool:
        return self.tier == config_tiers.ROOT_TIER

    @property
    def euid(self) -> int:
        if self._euid is not None:
            return int(self._euid)
        getter = getattr(os, "geteuid", None)
        return int(getter()) if callable(getter) else 0     # Windows 没有 geteuid

    def prompt(self) -> str:
        return PROMPT % self.tier


def _split(text: str) -> List[str]:
    """把一行拆成 argv（引号不闭合就按"原样当做一个词"处理, 不炸会话）。"""
    try:
        return shlex.split(text)
    except ValueError:
        return text.split()


def _emit(out: Callable[..., None], text: str = "", **kwargs: object) -> None:
    out(text, **kwargs)


def run_line(text: str, session: Session, *,
             out: Callable[..., None] = print,
             err: Optional[Callable[..., None]] = None) -> Tuple[int, bool]:
    """跑一行。

    @return `(退出码, 是否结束会话)` —— `exit` 之后那一行不会再跑
    """
    err = err or (lambda message="", **kwargs: print(message, file=sys.stderr, **kwargs))
    tokens = _split(str(text).strip())
    if not tokens or tokens[0].startswith("#"):
        return 0, False

    head = tokens[0]
    if head in EXIT_WORDS:
        if session.is_root:
            _emit(out, "已退出 root 体系。")
        return 0, True

    # 会话内的权限层切换（`mode root` / `mode user`）—— 不发给 Agent, 也不进 cmd_mode
    if head == "mode" and len(tokens) >= 2 and tokens[1].lower() in TIER_WORDS:
        want = tokens[1].lower()
        if want == config_tiers.ROOT_TIER:
            if session.euid != 0:
                _emit(err, "进不了 root 体系：当前不是 root（euid=%d）。"
                           "root 级设置项只在板端 root 控制台里改。" % session.euid)
                return 2, False
            session.tier = config_tiers.ROOT_TIER
            _emit(out, ROOT_PROMPT_NOTE)
            return 0, False
        session.tier = config_tiers.USER_TIER
        _emit(out, "已回到 user 体系（root 级设置项改不了了）。")
        return 0, False

    if head == "shell":
        _emit(err, "已经在 shell 里了（不许嵌套）。要退出就 `exit`。")
        return 2, False

    from agent.cli import build_parser                      # 惰性: 免得 import 成环

    try:
        args = build_parser().parse_args(tokens)
    except SystemExit as exc:                               # argparse 的错误路径会 sys.exit
        return int(exc.code or 0), False

    args.tier = session.tier                                # ← cmd_set 靠这个判权限
    try:
        return int(asyncio.run(args.func(args))), False
    except KeyboardInterrupt:                               # Ctrl-C: 只中断这一条
        _emit(out, "")
        return 130, False
    except Exception as exc:                                # noqa: BLE001 - 一条命令炸了不该带走会话
        _emit(err, "这条命令出错了：%r" % (exc,))
        return 1, False


def run_lines(lines: Iterable[str], session: Optional[Session] = None, *,
              out: Callable[..., None] = print,
              err: Optional[Callable[..., None]] = None) -> int:
    """按顺序跑多行（`-c "a; b"` 与非 tty 的 stdin 都走这里）。

    @return **最后一条**的退出码（与一次性 CLI 的语义一致）
    """
    session = session or Session()
    last = 0
    for line in lines:
        code, stop = run_line(line, session, out=out, err=err)
        last = code
        if stop:
            break
    return last


def main(command: Optional[Sequence[str]] = None, *,
         stdin: Optional[TextIO] = None) -> int:
    """`assistant shell` 的入口。

    @param command `-c` 给的那些（每条里可以用 `;` 分成多条）; None = 交互式/读 stdin
    """
    if command:
        lines: List[str] = []
        for chunk in command:
            lines.extend(str(chunk).split(";"))
        return run_lines(lines)

    stream = stdin if stdin is not None else sys.stdin
    session = Session()
    interactive = bool(getattr(stream, "isatty", lambda: False)())
    while True:
        if interactive:
            # 提示符先打出来再读（串口上就是这种感觉）
            sys.stdout.write(session.prompt())
            sys.stdout.flush()
        try:
            line = stream.readline()
        except KeyboardInterrupt:                           # Ctrl-C: 不打走会话
            print()
            continue
        if not line:                                        # EOF / Ctrl-D
            if interactive:
                print()
            return 0
        try:
            code, stop = run_line(line, session)
        except KeyboardInterrupt:
            print()
            continue
        if stop:
            return code


if __name__ == "__main__":                                  # pragma: no cover - 手工入口
    # ⚠ 这个 `if __name__` 不只是"能单独跑"：审计器的口径是"有它 = 这是个入口, 输出就是它的
    #   产物, 不是库代码里的 print"（`agent/cli.py` 同理）。所以别删。
    sys.exit(main())
