# ============================================================================
#  agent/net/netease_cli.py — 在 **PC 上**跑 neteasecli（Phase 7 T8-2）
#
#  这是什么
#  ---------------------------------------------------------------------------
#  网易云音乐没有公开 API，我们也不想在 PC 上放**自己写的**程序。所以:
#
#      PC 侧（我们一个字都不写）         板端（我们只写这一层）
#      ─────────────────────────         ──────────────────────────────
#      neteasecli（第三方 CLI, 已登录） ◀── ssh ── NeteaseCli.run(...)
#      mpv（第三方播放器, 出声）              ↑ 只解析它的 --json 输出
#
#  neteasecli 的 `--json` 输出**形状固定**: `{"success":bool,"data":…,"error":{…}|null}`
#  （见它的 README: 管道/`--json` 时就是 JSON）—— 本模块把它解成 (ok, data, error)。
#
#  为什么用 ssh 而不是别的
#  ---------------------------------------------------------------------------
#  · PC 侧允许跑的只有"系统组件 + 第三方工具": Windows 自带 OpenSSH Server 属于前者
#    （一次性开一次, 不是常驻程序）, 所以 ssh 是"零自制程序"里唯一能**读回输出**的通道
#  · 输入注入（Sunshine 那条路, 见 agent/io/input_sender.py）也能让 PC 跑命令, 但
#    **读不回 JSON** —— 那样进度只能瞎猜。所以主通道是 ssh, 注入留给"回到桌面"那类。
#  · `BatchMode=yes` 是**必须的**: 万一公钥没配好, 我们要的是"立刻失败并说清楚",
#    而不是卡在密码提示上把 Agent 挂住（那会连带把工具循环拖死）。
#
#  错误都翻译成"能照做的话"
#  ---------------------------------------------------------------------------
#  区分四类（板端排查时最想知道的）:
#      · 连不上 PC        -> PC 没开机 / sshd 没开 / 不在同一网段 / 防火墙
#      · 认证失败         -> 板端公钥没放进 PC 的 administrators_authorized_keys
#      · 登录态失效(code 2)-> 在 PC 上重跑 `neteasecli auth login`
#      · mpv 起不来       -> PC 上 mpv 没装 / 不在 PATH（`spawn mpv ENOENT`）
#  其余原样带上（不吞信息）。
# ============================================================================

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import time
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

__all__ = [
    "NeteaseCli",
    "NeteaseCliError",
    "DEFAULT_BINARY",
    "DEFAULT_TIMEOUT_S",
    "AuthFailure",
    "PlayerFailure",
    "ConnectionFailure",
]

_log_default = logging.getLogger(__name__)

#: PC 上那个 CLI 的名字（npm 全局装好后就在 PATH 上）
DEFAULT_BINARY = "neteasecli"

#: 单次远程调用超时（秒）—— neteasecli 自己也有 --timeout（默认 30），这里包一层
DEFAULT_TIMEOUT_S = 45.0

#: 连接超时（秒）：连接阶段短一点，别让"PC 没开机"拖满整个超时
CONNECT_TIMEOUT_S = 6.0

#: 播放类命令要落到**交互会话**用的计划任务名（每次覆盖重建，用完删掉）
INTERACTIVE_TASK = "agent_netease_play"

#: 播放之后等多久再确认"真起来了"（mpv 要加载流；实测 1–2 s 内 duration 就有值）
PLAY_CONFIRM_WAIT_S = 2.0


class NeteaseCliError(RuntimeError):
    """一次远程调用失败（连不上 / 认证 / 登录态 / 业务错误）。

    继承 RuntimeError，与 `agent/net/sunshine_client.py` 的错误家族一致。
    `message` 一律是**给人看的中文**（会经工具结果回到对话里）。
    """


class ConnectionFailure(NeteaseCliError):
    """连不上 PC（没开机 / sshd 没开 / 网络不通）。"""


class AuthFailure(NeteaseCliError):
    """要么板端公钥没配好，要么 neteasecli 的登录态过期。"""


class PlayerFailure(NeteaseCliError):
    """PC 上的播放器出问题（最常见：mpv 没装 / 不在 PATH）。"""


class NeteaseCli:
    """把 `ssh <pc> neteasecli --json …` 包成几个方法。

    典型用法::

        cli = NeteaseCli(host="192.168.137.1", user="Anorak")
        cli.play("2747166493")          # PC 上开始放
        cli.status()                    # {"playing":True,"position":9.0,...}
    """

    def __init__(
        self,
        host: str,
        user: str,
        port: int = 22,
        binary: str = DEFAULT_BINARY,
        ssh: str = "ssh",
        timeout_s: float = DEFAULT_TIMEOUT_S,
        log: Optional[logging.Logger] = None,
        runner: Optional[Callable[[Sequence[str], float], Tuple[int, str, str]]] = None,
    ) -> None:
        """
        @param runner 可选: "怎么跑这条 ssh"的替身 `(argv, timeout) -> (rc, stdout, stderr)`
                      —— 只给测试用（开发机上没有 PC 可连）
        """
        self.host = str(host or "").strip()
        self.user = str(user or "").strip()
        self.port = int(port or 22)
        self.binary = str(binary or DEFAULT_BINARY).strip() or DEFAULT_BINARY
        self.ssh = str(ssh or "ssh").strip() or "ssh"
        self.timeout_s = float(timeout_s or DEFAULT_TIMEOUT_S)
        self.log = log or _log_default
        self._runner = runner

    # ------------------------------------------------------------ 装配 ---
    @classmethod
    def from_config(cls, config: Optional[Mapping[str, Any]] = None,
                    log: Optional[logging.Logger] = None,
                    runner: Optional[Callable[..., Any]] = None) -> Optional["NeteaseCli"]:
        """按配置造（`music:` 段）。

        @return None = 没配/没开（`music.enabled` 为假或没有 `pc_host`）
        @note 只读**我们自己的**配置（`config.yaml` 的 `music:` 段）——
              PC 上那个 CLI 的账号信息不在我们这儿（它自己在 PC 上存 cookie）
        """
        node = (config or {}).get("music") if isinstance(config, Mapping) else None
        music = node if isinstance(node, Mapping) else {}
        if not _truthy(music.get("enabled")):
            return None
        host = str(music.get("pc_host") or "").strip()
        if not host:
            return None
        return cls(
            host=host,
            user=str(music.get("pc_user") or "").strip() or _default_user(),
            port=int(music.get("pc_port") or 22),
            binary=str(music.get("binary") or DEFAULT_BINARY),
            ssh=str(music.get("ssh") or "ssh"),
            timeout_s=float(music.get("timeout_s") or DEFAULT_TIMEOUT_S),
            log=log,
            runner=runner,
        )

    # ------------------------------------------------------------ 调用 ---
    def argv(self, args: Sequence[str]) -> List[str]:
        """拼出"跑 neteasecli"的 ssh 命令行（**纯函数**, 测试直接断言它）。"""
        remote = " ".join([self.binary] + [_quote(a) for a in args])
        return self._ssh_argv(remote)

    def _ssh_argv(self, remote: str) -> List[str]:
        """拼出"跑任意远程命令"的 ssh 命令行（schtasks 这类也用同一套选项）。"""
        target = "%s@%s" % (self.user, self.host) if self.user else self.host
        return [
            self.ssh,
            "-p", str(self.port),
            "-o", "BatchMode=yes",                  # 不许弹密码：失败就立刻失败
            "-o", "StrictHostKeyChecking=no",       # 局域网内自己人；不想因为主机键变化挂住
            "-o", "ConnectTimeout=%d" % int(CONNECT_TIMEOUT_S),
            target,
            remote,
        ]

    def run(self, *args: str, timeout_s: Optional[float] = None) -> Dict[str, Any]:
        """跑一次 `neteasecli --json <args>`，返回它的 `data`。

        @raise ConnectionFailure / AuthFailure / PlayerFailure / NeteaseCliError
        """
        if not self.host:
            raise ConnectionFailure("没有配 PC 地址（config.yaml 的 music.pc_host）")
        argv = self.argv(list(args))
        limit = float(timeout_s or self.timeout_s)
        runner = self._runner or self._run_subprocess
        try:
            code, out, err = runner(argv, limit)
        except FileNotFoundError as exc:
            raise ConnectionFailure("板端没有 ssh 命令（装 openssh-client）: %s" % exc)
        except subprocess.TimeoutExpired:
            raise ConnectionFailure("ssh 超过 %.0f 秒没回来（PC 卡住 / 网络不通）" % limit)

        payload = self._parse(code, out, err)
        if not payload.get("success"):
            raise _classify(payload, code, err)
        data = payload.get("data")
        return data if isinstance(data, dict) else {"data": data}

    def _run_subprocess(self, argv: Sequence[str], timeout_s: float) -> Tuple[int, str, str]:
        proc = subprocess.run(list(argv), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              timeout=timeout_s)
        return (proc.returncode,
                (proc.stdout or b"").decode("utf-8", "replace"),
                (proc.stderr or b"").decode("utf-8", "replace"))

    @staticmethod
    def _parse(code: int, out: str, err: str) -> Dict[str, Any]:
        """把 (退出码, stdout, stderr) 解成 neteasecli 的信封。

        @note 退出码约定（README）: 0 成功 / 1 一般错误 / 2 认证 / 3 网络。
              但 ssh 自己的失败是 **255**，跟上面不是一套 —— 先看输出里有没有 JSON，
              没有 JSON 才按 ssh 的错误解释。
        @note stdout 里可能混着别的行（mpv 的日志等），所以先整段试一次 JSON，
              失败再从第一个 `{` 开始试一次。
        """
        text = (out or "").strip()
        payload = _first_json(text)
        if payload is not None:
            return payload
        if code == 255 or "Permission denied" in err or "Connection refused" in err \
                or "Connection timed out" in err or "No route to host" in err \
                or "Could not resolve hostname" in err:
            return {"success": False, "data": None,
                    "error": {"code": "SSH", "message": (err or out or "").strip()}}
        # 有 JSON 但形状不对 / 没 JSON 又没明显 ssh 错误：如实说
        return {"success": False, "data": None,
                "error": {"code": "UNEXPECTED",
                          "message": "neteasecli 的输出看不懂（rc=%d）: %s"
                                     % (code, (text or err or "")[:200])}}

    # ------------------------------------------------------------ 动作 ---
    #: 下面这些都是**薄封装**：参数拼错在测试里一眼能看出来
    def status(self) -> Dict[str, Any]:
        """当前播放状态（position/duration/playing —— mpv IPC 的**真实值**）。

        ⚠ `playing=True, position=0, duration=0` 是 neteasecli 的**乐观默认值**：
          它连不上 mpv 时就长这样（实测 mpv 没装/会话被杀时都是这个形状）。
          所以判断"到底在不在放"要看 **duration > 0**，不能只看 playing。
          本模块把它顺手翻成 `is_playing()`。
        """
        return self.run("player", "status")

    def is_playing(self) -> bool:
        """真的有个 mpv 在放吗（`duration > 0` 才算 —— 见 `status()` 的说明）。"""
        try:
            data = self.status()
        except NeteaseCliError:
            return False
        return float(data.get("duration") or 0) > 0

    def play(self, track_id: Any, quality: Optional[str] = None,
             confirm: bool = True) -> Dict[str, Any]:
        """在 PC 上开始播放（**走交互会话**，所以真的出声）。

        ⚠ 为什么不能直接 ssh 跑 `track play`（实测踩到）: Windows 的 sshd 是**服务**,
          它起的进程在 **session 0** —— 那里**没有音频设备**（放不出声）, 而且 ssh 会话
          一断, 子进程就被杀（实测 3 s 后 `mpv.exe` 已经没了、`duration` 恒为 0）。
          所以播放这一条走 `schtasks /it`（"只在用户登录时运行"）—— 落到**用户桌面会话**;
          **读取与控制仍然走 ssh**（mpv 的 IPC socket 在 session 0 也够得着, 实测能暂停）。

        @param confirm True = 起完等 1–2 s 确认 `duration > 0`（做不到就如实抛错）
        @raise PlayerFailure PC 上没人登录桌面 / mpv 起不来
        """
        command = "%s track play %s" % (self.binary, track_id)
        if quality:
            command += " -q %s" % quality
        self._run_interactive(command)
        if not confirm:
            return {"message": "已在 PC 上请求播放 %s" % track_id}
        time.sleep(PLAY_CONFIRM_WAIT_S)
        if not self.is_playing():
            raise PlayerFailure(
                "PC 上没有开始播放 —— 常见原因: ①PC 上**没人登录桌面**（计划任务只在"
                "用户登录时运行）②PC 上 mpv 不在 PATH ③这首要 VIP/版权拿不到地址。"
                "可以先在 PC 上手动跑一次 `neteasecli track play %s` 看它报什么" % track_id)
        return {"message": "正在 PC 上播放 %s" % track_id}

    def _run_interactive(self, command: str) -> None:
        """把一条命令丢进**交互会话**里跑（schtasks /it 那套）。

        @note 步骤: 建/覆盖计划任务 → 运行 → 删掉。任务名固定（同时只有一条播放命令）。
        @note 失败**不抛**: 调用方（`play()`）会用"到底有没有声音"来判定, 那比 schtasks
              的退出码可靠（它经常返回 0 却什么都没跑）。
        """
        quoted = command.replace('"', '\\"')
        create = ('schtasks /create /tn %s /tr "cmd /c %s" /sc once /st 00:00 /it /f'
                  % (INTERACTIVE_TASK, quoted))
        for remote in (create, "schtasks /run /tn %s" % INTERACTIVE_TASK,
                       "schtasks /delete /tn %s /f" % INTERACTIVE_TASK):
            try:
                self._ssh(remote)
            except NeteaseCliError as exc:
                self.log.warning("netease_cli: 交互会话启动失败 (%s): %s", remote[:60], exc)

    def _ssh(self, remote: str, timeout_s: Optional[float] = None) -> Tuple[int, str, str]:
        """跑一条**任意**远程命令（不解析 JSON）—— 给 schtasks 这类用。"""
        if not self.host:
            raise ConnectionFailure("没有配 PC 地址（config.yaml 的 music.pc_host）")
        argv = self._ssh_argv(remote)
        runner = self._runner or self._run_subprocess
        try:
            return runner(argv, float(timeout_s or self.timeout_s))
        except FileNotFoundError as exc:
            raise ConnectionFailure("板端没有 ssh 命令（装 openssh-client）: %s" % exc)
        except subprocess.TimeoutExpired:
            raise ConnectionFailure("ssh 超过 %.0f 秒没回来（PC 卡住 / 网络不通）"
                                    % float(timeout_s or self.timeout_s))

    def pause(self) -> Dict[str, Any]:
        return self.run("player", "pause")

    def stop(self) -> Dict[str, Any]:
        return self.run("player", "stop")

    def seek(self, seconds: float, absolute: bool = False) -> Dict[str, Any]:
        args = ["player", "seek", str(seconds)]
        if absolute:
            args.append("--absolute")
        return self.run(*args)

    def set_volume(self, level: int) -> Dict[str, Any]:
        return self.run("player", "volume", str(int(level)))

    def volume(self) -> Dict[str, Any]:
        return self.run("player", "volume")

    def track_detail(self, track_id: Any) -> Dict[str, Any]:
        return self.run("track", "detail", str(track_id))

    def track_url(self, track_id: Any, quality: str = "exhigh") -> Dict[str, Any]:
        return self.run("track", "url", str(track_id), "-q", quality)

    def lyric(self, track_id: Any) -> Dict[str, Any]:
        return self.run("track", "lyric", str(track_id))

    def search(self, kind: str, keyword: str, limit: int = 20,
               offset: int = 0) -> Dict[str, Any]:
        return self.run("search", str(kind), str(keyword),
                        "-l", str(int(limit)), "-o", str(int(offset)))

    def playlist_detail(self, playlist_id: Any) -> Dict[str, Any]:
        return self.run("playlist", "detail", str(playlist_id))

    def playlist_list(self) -> Dict[str, Any]:
        return self.run("playlist", "list")

    def auth_check(self) -> Dict[str, Any]:
        return self.run("auth", "check")

    def __repr__(self) -> str:
        return "<NeteaseCli %s@%s:%d %s>" % (self.user or "-", self.host, self.port, self.binary)


# ---------------------------------------------------------------------------
#  辅助
# ---------------------------------------------------------------------------
def _quote(text: str) -> str:
    """远程命令里的参数：含空格/引号就单引号包起来（POSIX shell）。

    @note 这里的"远程"是 **Windows 的 OpenSSH**：它的默认 shell 是 cmd.exe,
          但也接受单引号? —— 不, cmd 不认单引号。所以**只用双引号**并把内部的
          双引号转义成 `\\"`，这是 cmd 与 POSIX 都能接受的最小公共写法。
          我们的参数只有歌单/歌曲 id、关键词、模式名, 正常都不带空格。
    """
    text = str(text)
    if not text or all(ch.isalnum() or ch in "-_./:=@[]" for ch in text):
        return text
    return '"%s"' % text.replace('"', '\\"')


def _truthy(value: Any) -> bool:
    """配置里的布尔值（YAML 给真 bool；手写的 "true"/"yes" 也认）。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on", "启用")
    return bool(value)


def _default_user() -> str:
    """没配 `pc_user` 时的兜底：当前用户名（板端是 root，通常不对 —— 会如实报认证失败）。"""
    import getpass

    try:
        return getpass.getuser()
    except Exception:                                 # noqa: BLE001
        return ""


def _first_json(text: str) -> Optional[Dict[str, Any]]:
    """从一段输出里找**第一个**像 neteasecli 信封的 JSON 对象。

    @return None = 没找到（调用方就按 rc/stderr 解释）
    @note 为什么要"找"而不是直接 json.loads: ssh 回来的 stdout 里可能混着别的行
          （mpv 的日志、PowerShell 的横幅…），整段解析会失败。
    """
    if not text:
        return None
    candidates: List[str] = [text]
    start = text.find("{")
    if start > 0:
        candidates.append(text[start:])
    for candidate in candidates:
        try:
            payload = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(payload, dict) and "success" in payload:
            return payload
    return None


#: neteasecli 自己给的错误码 -> 我们的异常类型
_ERRORS = {
    "AUTH_ERROR": AuthFailure,
    "AUTH": AuthFailure,
    "PLAYER_ERROR": PlayerFailure,
    "PLAYER": PlayerFailure,
}


def _classify(payload: Mapping[str, Any], code: int, err: str) -> NeteaseCliError:
    """把 neteasecli 的失败信封翻成"能照做的话"。"""
    error = payload.get("error") or {}
    if not isinstance(error, Mapping):
        error = {"message": str(error)}
    error_code = str(error.get("code") or "").upper()
    message = str(error.get("message") or "").strip() or "neteasecli 没给出原因"
    joined = "%s %s" % (message, err or "")

    if error_code == "SSH":
        if "Permission denied" in joined:
            return AuthFailure(
                "板端连 PC 的 ssh 被拒（认证失败）—— 把板端公钥 %s 放进 PC 的 "
                "C:\\ProgramData\\ssh\\administrators_authorized_keys（见 docs/music.md）"
                % _public_key_hint())
        if "Connection refused" in joined or "timed out" in joined \
                or "No route to host" in joined or "resolve" in joined:
            return ConnectionFailure(
                "连不上 PC（%s）—— 检查 PC 是否开机、Windows OpenSSH Server 是否启动、"
                "板端与 PC 是否在同一网段" % joined.strip()[:160])
        return ConnectionFailure("ssh 调用失败: %s" % joined.strip()[:200])

    if error_code in _ERRORS:
        cls = _ERRORS[error_code]
        if "ENOENT" in joined or "mpv" in joined.lower():
            return PlayerFailure(
                "PC 上起不了 mpv（neteasecli 报 %s）—— 确认 PC 装了 mpv 且在 PATH 里"
                % message)
        if cls is AuthFailure:
            return AuthFailure("网易云登录态失效（%s）—— 在 **PC 上**重跑 `neteasecli auth login`"
                               % message)
        return PlayerFailure("PC 播放器出错: %s" % message)

    if code == 2:
        return AuthFailure("网易云登录态失效 —— 在 PC 上重跑 `neteasecli auth login`")
    if code == 3:
        return ConnectionFailure("PC 那边网络请求失败: %s" % message)
    return NeteaseCliError("neteasecli 失败（%s）: %s" % (error_code or "未知", message))


def _public_key_hint() -> str:
    """给错误信息里带上"该放哪把公钥"，省得人去翻文档。"""
    path = None
    for candidate in ("/root/.ssh/id_ed25519.pub", "/home/kickpi/.ssh/id_ed25519.pub"):
        import os

        if os.path.exists(candidate):
            path = candidate
            break
    if path is None:
        return "(~/.ssh/id_ed25519.pub)"
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read().strip()
    except OSError:
        return path


def ssh_available() -> bool:
    """板端有没有 ssh 客户端（没有的话错误信息里就说清装什么）。"""
    return shutil.which("ssh") is not None
