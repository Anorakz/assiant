# ============================================================================
#  agent/llm/service.py — 让 Agent 管本机 llama-server 的启停（Phase 7 T7-4）
#
#  它做什么
#  ---------------------------------------------------------------------------
#  edge 模式要的是"板端本机的 llama-server 正跑着"。以前这件事**完全靠人**:
#  开机后忘了起 / 板子重启过 / 手动关了 —— Agent 只会一直降级到规则兜底
#  （日志里一行 `LLM 降级为规则兜底: APIConnectionError`, 界面上看起来像"变笨了"）。
#  这个模块把启停接进 Agent 的生命周期:
#
#      mode=edge 且 llm.manage_service=true 时
#        · Agent 启动        -> 起服务
#        · 离开 SLEEP        -> 起服务
#        · 进入 SLEEP        -> 停服务（睡觉时不该占着 CPU/内存）
#
#  为什么不自己 fork llama-server
#  ---------------------------------------------------------------------------
#  起停的**唯一实现**在 `llm/scripts/{start,stop}.sh` 里（PID 文件、日志、端口、
#  模型校验都在那儿）。GUI 的「启动服务」按钮也是跑它。这里再写一份 fork/杀进程，
#  就会出现"GUI 说在跑、Agent 说没跑"这种最难查的分叉。所以本模块只做两件事:
#  调那两个脚本 + 探一次"到底能用了没"。
#
#  ⚠ 默认**不管**（`llm.manage_service` 缺省 false）
#  ---------------------------------------------------------------------------
#  管启停意味着"Agent 可能把你手动起的服务停掉"。这是明确的行为改变, 所以默认关闭,
#  想用就在 config.yaml 里打开 —— 与"别偷偷动别人的进程"同一条口径。
#
#  ⚠ 只动**本机**、只在 edge 模式
#  ---------------------------------------------------------------------------
#  cloud / disabled 模式下行里根本没有本机服务这回事; mode 不是 edge 时
#  `from_config()` 直接返回 None, 调用方什么都不做。
# ============================================================================

from __future__ import annotations

import json
import logging
import os
import subprocess
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Mapping, Optional, Tuple

__all__ = [
    "LlamaService",
    "LlamaServiceError",
    "default_scripts_dir",
    "REPO_ROOT",
]

_log_default = logging.getLogger(__name__)

#: `agent/llm/service.py` 往上三级 = 仓库根（与 `agent/vision/wall_data.py` 同款算法）
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

#: 脚本自己的超时（起服务是 nohup + 立刻返回, 停服务最多等它退出）
SCRIPT_TIMEOUT_S = 30.0

#: 等"真的能用了"的上限（模型加载在板端实测 20–40 s）
DEFAULT_READY_TIMEOUT_S = 90.0

#: 探活间隔
PROBE_INTERVAL_S = 1.5


class LlamaServiceError(RuntimeError):
    """起停脚本用不了（不存在 / 跑不起来）。

    继承 RuntimeError：与 `agent/llm/provider.py` 的错误家族一致。
    """


def default_scripts_dir() -> str:
    """`llm/scripts/`（起停脚本所在目录）。"""
    return os.path.join(REPO_ROOT, "llm", "scripts")


def _truthy(value: Any) -> bool:
    """配置里的布尔值（YAML 给的是真 bool；字符串 'true'/'yes' 也认，手写配置常见）。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on", "启用")
    if isinstance(value, (int, float)):
        return bool(value)
    return False


def _run_bash(script_path: str) -> subprocess.CompletedProcess:
    """默认的"怎么跑脚本": `bash <脚本>`（与 GUI 的「启动服务」按钮同一条路）。

    @note 用 bash 而不是 `sh`: 脚本里用了 `BASH_SOURCE` / `source`
          （板端 Ubuntu 的 /bin/sh 是 dash, 跑不了 —— `status.sh` 实测报
          "Bad substitution"）。
    @note 路径**统一转成正斜杠**: 开发机上装的 bash 是 Git Bash（msys）, 它把
          `C:\\Users\\…` 里的反斜杠当**转义字符**吃掉, 于是报"找不到
          C:Users…"这种莫名其妙的路径。正斜杠两边都认。
    """
    return subprocess.run(
        ["bash", script_path.replace(os.sep, "/")],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=SCRIPT_TIMEOUT_S,
        cwd=os.path.dirname(os.path.dirname(script_path)),
    )


class LlamaService:
    """本机 llama-server 的启停 + 探活（薄薄一层，真正干活的在 llm/scripts/）。"""

    def __init__(
        self,
        scripts_dir: Optional[str] = None,
        port: int = 9000,
        api_key: str = "",
        ready_timeout_s: float = DEFAULT_READY_TIMEOUT_S,
        log: Optional[logging.Logger] = None,
        runner: Optional[Callable[[str], Any]] = None,
    ) -> None:
        """
        @param runner 可选: "怎么跑脚本"的替身（默认 `bash <脚本>`）。
                      **只给测试用** —— 起停脚本是 shell 脚本, 开发机（Windows）上
                      没有可靠的 bash, 所以那些用例注入一个假 runner 来验"退出码/输出
                      怎么解释", 而真跑脚本的那两条只在有 bash 的机器上跑。
        """
        self.scripts_dir = scripts_dir or default_scripts_dir()
        self.port = int(port or 9000)
        self.api_key = str(api_key or "")
        self.ready_timeout_s = float(ready_timeout_s)
        self.log = log or _log_default
        self._runner = runner or _run_bash

    # ------------------------------------------------------------ 装配 ---
    @classmethod
    def from_config(cls, config: Optional[Mapping[str, Any]] = None,
                    scripts_dir: Optional[str] = None,
                    log: Optional[logging.Logger] = None) -> Optional["LlamaService"]:
        """按配置决定"要不要管这个服务"。

        @return None = 不管（`mode` 不是 edge，或 `manage_service` 没开）
        @note 只在**边 mode=edge 且显式打开开关**时才返回对象 —— "该不该动别人的
              进程"必须是一条配置说清楚的规则，不是猜出来的
        """
        node: Any = config
        llm = node.get("llm") if isinstance(node, Mapping) else None
        llm = llm if isinstance(llm, Mapping) else {}
        mode = str(llm.get("mode") or "").strip().lower()
        if mode != "edge":
            return None
        if not _truthy(llm.get("manage_service")):
            return None
        port = llm.get("port") or 9000
        try:
            port = int(port)
        except (TypeError, ValueError):
            port = 9000
        return cls(scripts_dir=scripts_dir, port=port,
                   api_key=str(llm.get("local_api_key") or ""), log=log)

    # ------------------------------------------------------------ 起停 ---
    def start(self) -> Tuple[bool, str]:
        """跑 `llm/scripts/start.sh`。**不抛** —— 返回 (ok, 一句给人看的说明)。"""
        return self._run_script("start.sh")

    def stop(self) -> Tuple[bool, str]:
        """跑 `llm/scripts/stop.sh`。**不抛**。"""
        return self._run_script("stop.sh")

    def _run_script(self, name: str) -> Tuple[bool, str]:
        path = os.path.join(self.scripts_dir, name)
        if not os.path.isfile(path):
            return False, "脚本不存在: %s" % path
        try:
            proc = self._runner(path)
        except (OSError, subprocess.SubprocessError) as exc:
            return False, "%s 跑不起来: %s: %s" % (name, type(exc).__name__, exc)
        output = (getattr(proc, "stdout", b"") or b"")
        if isinstance(output, str):                   # 替身 runner 可能直接给字符串
            output = output.encode("utf-8", "replace")
        lines = output.decode("utf-8", "replace").strip().splitlines()
        tail = lines[-1].strip() if lines else ""
        code = int(getattr(proc, "returncode", 0) or 0)
        if code != 0:
            return False, "%s 退出码 %d%s" % (name, code,
                                             "（%s）" % tail if tail else "")
        return True, tail or "%s 成功（没有输出）" % name

    # ------------------------------------------------------------ 探活 ---
    def endpoint(self) -> str:
        return "http://127.0.0.1:%d/v1/models" % self.port

    def is_ready(self) -> bool:
        """服务真的能应答了吗（带 key 请求 `/v1/models`）。

        @note 只有 HTTP 200 才算就绪：端口活着但**拒绝 key** 时也不算 —— 那正是
              "起来了一半"的状态，报"就绪"会把人骗过去。
        """
        request = urllib.request.Request(self.endpoint())
        if self.api_key:
            request.add_header("Authorization", "Bearer %s" % self.api_key)
        try:
            with urllib.request.urlopen(request, timeout=3.0) as response:
                body = response.read()
        except urllib.error.HTTPError as exc:
            self.log.debug("llm_service: 探活 HTTP %s", exc.code)
            return False
        except Exception as exc:                      # noqa: BLE001 - 没起来就是连不上
            self.log.debug("llm_service: 探活失败 %r", exc)
            return False
        try:
            payload = json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return False
        return isinstance(payload, dict) and bool(payload.get("data"))

    def wait_ready(self, timeout_s: Optional[float] = None) -> Tuple[bool, float]:
        """轮询到就绪（**阻塞**；调用方自己丢线程池）。

        @return (是否就绪, 等了多久秒)
        """
        limit = float(self.ready_timeout_s if timeout_s is None else timeout_s)
        started = time.monotonic()
        while True:
            if self.is_ready():
                return True, time.monotonic() - started
            if time.monotonic() - started >= limit:
                return False, time.monotonic() - started
            time.sleep(PROBE_INTERVAL_S)

    def __repr__(self) -> str:
        return "<LlamaService port=%d scripts=%s>" % (self.port, self.scripts_dir)
