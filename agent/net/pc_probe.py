# ============================================================================
#  agent/net/pc_probe.py — 从板端**问 PC 正在跑什么**（Phase 7 T11-4）
#
#  为什么需要它: 认游戏有两路 —— **画面锚点**（SigLIP, 实测 top-1 只有 69%, galgame 之间会混）
#  和 **PC 进程名**（硬证据）。T11-0 实测:
#      · **窗口标题拿不到** —— Windows 的 sshd 跑在 **session 0**, 那里没有交互桌面,
#        `MainWindowTitle` 恒为空（试过加不加 UTF-8 强制都一样）。所以只能读**进程名**。
#      · `powershell -NoProfile -Command "Get-Process | Select-Object -ExpandProperty ProcessName"`
#        能正常拿到（0.5~1.5 s, 中文/空格进程名也没问题, UTF-8 解码干净）。
#
#  谁用它: `agent/core/game_watch.py`（画面与进程对不上时以进程为准, 并把它学成锚点）。
#
#  ⚠ 复用 `NeteaseCli._ssh_argv`（同一套 ssh 选项: `BatchMode=yes` / 连接超时）——
#    那条注释本来就写着"跑任意远程命令也用同一套选项"。**不自己再抄一份 ssh 参数**。
#  ⚠ 本模块不 import native、import 时不联网; `runner` 可注入（单测全离线）。
# ============================================================================

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Mapping, Optional

__all__ = ["PcProbe", "PcProbeError", "normalize_process", "DEFAULT_PS_COMMAND"]

_log = logging.getLogger(__name__)

#: 问进程名的那条命令（实测可用; 只列名字, 不列窗口标题 —— 那玩意在 session 0 恒空）。
DEFAULT_PS_COMMAND = ("powershell -NoProfile -Command "
                      "\"Get-Process | Select-Object -ExpandProperty ProcessName\"")


class PcProbeError(RuntimeError):
    """问不到 PC 的情况（连不上/命令失败）。消息给人看。"""


def normalize_process(name: Any) -> str:
    """进程名归一化: 去 `.exe` 后缀、去首尾空白、**小写**（比较用）。"""
    text = str(name or "").strip().strip('"')
    if text.lower().endswith(".exe"):
        text = text[:-4]
    return text.strip().lower()


class PcProbe(object):
    """问 PC 上正在跑的进程（用来认游戏）。

    典型用法::

        probe = PcProbe.from_config(config)
        probe.games()        # -> ["stellaris"]（按映射表把进程名翻成游戏名）
    """

    def __init__(self, *, mapping: Optional[Mapping[str, str]] = None,
                 ssh: str = "ssh", host: str = "", user: str = "", port: int = 22,
                 timeout_s: float = 15.0, runner: Optional[Callable[..., Any]] = None,
                 command: str = DEFAULT_PS_COMMAND,
                 log: Optional[logging.Logger] = None) -> None:
        """
        @param mapping {进程名（可带 .exe）: 游戏名} —— 老板给的那张表
        @param runner  可选替身 `(argv, timeout) -> (rc, stdout, stderr)`（单测注入）
        """
        self.log = log or _log
        self.mapping = {normalize_process(key): str(value)
                        for key, value in dict(mapping or {}).items() if str(key).strip()}
        self.ssh = str(ssh or "ssh")
        self.host = str(host or "")
        self.user = str(user or "")
        self.port = int(port or 22)
        self.timeout_s = float(timeout_s or 15.0)
        self.command = str(command or DEFAULT_PS_COMMAND)
        self._runner = runner

    @classmethod
    def from_config(cls, section: Optional[Mapping[str, Any]] = None, *,
                    music: Optional[Mapping[str, Any]] = None,
                    runner: Optional[Callable[..., Any]] = None,
                    log: Optional[logging.Logger] = None) -> "PcProbe":
        """从 `config.yaml` 构造: `bilibili.game_watch.process_names` + `music.pc_*`（同一台 PC）。

        @param music `music:` 段（host/user/port/ssh 都在那儿 —— 音乐那套已经在用同一个 ssh）
        """
        data = dict(section or {})
        music = dict(music or {})
        return cls(mapping=data.get("process_names") or {},
                   ssh=str(music.get("ssh") or "ssh"),
                   host=str(music.get("pc_host") or ""),
                   user=str(music.get("pc_user") or ""),
                   port=int(music.get("pc_port") or 22),
                   timeout_s=float(music.get("timeout_s") or 15.0),
                   runner=runner, log=log)

    # ------------------------------------------------------------ 问 ---
    def _ssh_argv(self, remote: str) -> List[str]:
        from agent.net.netease_cli import NeteaseCli

        helper = NeteaseCli(host=self.host, user=self.user, port=self.port, ssh=self.ssh,
                            timeout_s=self.timeout_s)
        return helper._ssh_argv(remote)

    def names(self) -> List[str]:
        """PC 上正在跑的进程名（归一化后去重）。

        @raise PcProbeError 没配 PC 地址 / ssh 失败 / 命令失败（都带"能照做"的话）
        """
        if not self.host:
            raise PcProbeError("没配 PC 地址（config.yaml 的 music.pc_host）—— 问不了进程")
        argv = self._ssh_argv(self.command)
        runner = self._runner
        if runner is None:
            import subprocess

            def runner(command, timeout):                 # noqa: E306 - 默认实现
                proc = subprocess.run(list(command), stdout=subprocess.PIPE,
                                      stderr=subprocess.PIPE, timeout=timeout)
                return (proc.returncode, proc.stdout.decode("utf-8", "replace"),
                        proc.stderr.decode("utf-8", "replace"))
        try:
            result = runner(argv, self.timeout_s)
        except Exception as exc:                          # noqa: BLE001 - 超时/没有 ssh 都归这类
            raise PcProbeError("问不了 PC（%s）—— 检查 PC 是否开机/同一网段" % exc) from exc
        code, out, err = (list(result) + ["", "", ""])[:3]
        if int(code or 0) != 0:
            raise PcProbeError("在 PC 上跑那条命令失败了（退出码 %s）：%s"
                               % (code, str(err or out).strip()[:120]))
        names: List[str] = []
        for line in str(out or "").splitlines():
            name = normalize_process(line)
            if name and name not in names:
                names.append(name)
        if not names:
            raise PcProbeError("PC 一个进程名都没回（ssh 通了但命令没输出）")
        return names

    def games(self) -> Dict[str, List[str]]:
        """{游戏名: [命中的进程名…]} —— 只包含**映射表里认识**的那些。

        @note 认不出就返回空的（调用方据此走"画面说了算"或"什么都不做"），
              **不猜**成别的游戏。
        """
        found: Dict[str, List[str]] = {}
        for name in self.names():
            game = self.mapping.get(name)
            if game:
                found.setdefault(game, []).append(name)
        return found

    def snapshot(self) -> Dict[str, Any]:
        """给日志/气泡用的一句话快照（出错也如实带着原因）。"""
        try:
            games = self.games()
        except PcProbeError as exc:
            return {"ok": False, "games": {}, "why": str(exc)}
        out: Dict[str, Any] = {"ok": True, "games": games, "why": ""}
        if len(games) > 1:
            out["why"] = "PC 上同时开着多个认识的游戏：%s" % "、".join(sorted(games))
        return out
