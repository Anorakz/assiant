# ============================================================================
#  agent/net/sunshine_client.py — Sunshine (GameStream) HTTPS 握手客户端
#
#  为什么握手放在 Python 而不是 C++
#  ---------------------------------------------------------------------------
#  Sunshine 把 /serverinfo /applist /launch 放在 **HTTPS 47984 + 客户端证书** 上;
#  不带证书的明文 HTTP 47989 只会得到 PairStatus=0, /launch 必然失败 —— 这正是
#  Phase 6 之前连不上的根因。native 侧的 moonlight_connection.cpp 是手写的裸
#  socket HTTP 客户端, 要支持 TLS 就得再给交叉编译引入 OpenSSL; 而 Python 的
#  ssl 模块本来就在。所以分工是:
#
#      Python (本模块)   HTTPS 47984        → app_version + sessionUrl0
#      native            start_with_session() → 直接进 LiStartConnection
#
#  moonlight_connection.cpp 那一层留给"无 TLS 的 GFE 主机", 不归本模块管。
#
#  与别的 Moonlight 客户端共存 (同一台主机上)
#  ---------------------------------------------------------------------------
#  主机上已经有应用在跑时, Sunshine 对 /launch 回:
#
#      <root status_code="400" status_message="An app is already running on this host">
#
#  这不是失败 —— 正确做法是 /resume **加入**那个会话 (Moonlight 自己遇到"主机忙"
#  也是这么做的)。start_session() 封了这一层: 先 /launch, 命中"已在运行"就退到
#  /resume。实测 (主机上有另一个 Moonlight 客户端在串流时):
#
#      /launch -> status_code=400  An app is already running on this host
#      /resume -> status_code=200  <sessionUrl0>rtsp://127.0.0.1:48010</sessionUrl0>
#                                  <resume>1</resume>
#
#  走 /resume 时我们加入的是**同一个**会话, 所以拿到的画面与对方一致, 这是共存的
#  正常表现。反过来, 我们 stop() 只是断开自己的 RTSP 客户端, Sunshine 要等最后一个
#  客户端断开才收应用, 因此不会把对方踢下线。
#
#  真源对照
#  ---------------------------------------------------------------------------
#  scripts/verify-authorized.sh 是同一套请求的 curl 版本, 已在本项目验证过通路
#  (真的拿到过 sessionUrl0)。本模块的 query 串与它逐项一致, 另外追加 native 提供的
#  LiGetLaunchUrlQueryParameters() —— "Sunshine 扩展参数"该由 moonlight-common-c
#  说了算, 不抄进 Python。(本版本该函数就返回 "&corever=1")
#
#  分工
#  ---------------------------------------------------------------------------
#  纯函数 (解析 / query 拼装) 与网络 I/O 分开, 前者无网可测, 见 tests/test_sunshine_client.py:
#
#      parse_server_info / parse_app_list / parse_launch
#      build_launch_query / stream_mode / surround_audio_info / is_already_running
# ============================================================================

from __future__ import annotations

import dataclasses
import re
import socket
import ssl
from pathlib import Path
from typing import List, NamedTuple, Optional, Sequence, Tuple
from urllib import error as urlerror
from urllib import request as urlrequest

# ---------------------------------------------------------------------------
#  常量
# ---------------------------------------------------------------------------

#: Sunshine 的 HTTPS 端口 (握手 + 会话), 需要客户端证书
DEFAULT_HTTPS_PORT = 47984
#: Sunshine 的 HTTP 端口 (配对用), 明文; 这里只作为提示保留
DEFAULT_HTTP_PORT = 47989
#: 连接与读写总超时 (秒)
DEFAULT_TIMEOUT = 12.0
#: 配对时用的 uniqueid。必须与 scripts/pair_sunshine.py 用的是**同一个**:
#: Sunshine 把客户端证书和 uniqueid 绑在一起, 换一个就会 401。
#: (scripts/verify-authorized.sh 里的 PAIR_UID 也是这个值)
DEFAULT_UNIQUE_ID = "0123456789ABCDEF0123456789ABCDEF"

USER_AGENT = "agent-rk3568/0.1"

#: 立体声的 surroundAudioInfo。
#: 由 Limelight.h 的两个宏推导, 不是抄来的魔数:
#:     AUDIO_CONFIGURATION_STEREO = MAKE_AUDIO_CONFIGURATION(2, 0x3)
#:     MAKE_AUDIO_CONFIGURATION(c, m)          = (m << 16) | (c << 8) | 0xCA
#:     SURROUNDAUDIOINFO_FROM_AUDIO_CONFIGURATION(x) = (mask << 16) | count
#: 所以 = (0x3 << 16) | 2 = 196610, 与 verify-authorized.sh 里那个字面量一致。
STEREO_CHANNEL_COUNT = 2
STEREO_CHANNEL_MASK = 0x3

#: 流加密密钥。本版本的 LiGetLaunchUrlQueryParameters() 只返回 "&corever=1"
#: (= 不启用加密), 所以这里给全 0, Sunshine 接受 (verify-authorized.sh 同样)。
ZERO_RIKEY = "0" * 32

#: "主机上已经有应用在跑"的判定依据。
#:
#: 文案取自真机实测 (Sunshine 对 /launch 的原话); 用文本而不是只认状态码, 是因为
#: 状态码会与"请求本身有问题"共用。400 是实测到的码, 作为兜底: 即使文案变了,
#: 也不会漏判 (万一是别的原因, /resume 会再失败一次并把两边的原话都带出来)。
ALREADY_RUNNING_HINTS = (
    "already running",
    "already in use",
    "app is already",
)
ALREADY_RUNNING_CODES = (400,)


def surround_audio_info(channel_count: int = STEREO_CHANNEL_COUNT,
                        channel_mask: int = STEREO_CHANNEL_MASK) -> int:
    """把 (声道数, 声道掩码) 打包成 /launch 的 surroundAudioInfo 参数。

    对应 Limelight.h 的 SURROUNDAUDIOINFO_FROM_AUDIO_CONFIGURATION()。
    """
    if channel_count <= 0 or channel_mask <= 0:
        raise SunshineConfigError(
            "surroundAudioInfo 参数非法: channel_count=%d channel_mask=%d"
            % (channel_count, channel_mask)
        )
    return (channel_mask << 16) | channel_count


def stream_mode(width: int, height: int, fps: int) -> str:
    """拼 /launch 的 mode 参数: "1280x720x60" 形态。"""
    for name, value in (("width", width), ("height", height), ("fps", fps)):
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise SunshineConfigError("mode 参数 %s 非法: %r" % (name, value))
    return "%dx%dx%d" % (width, height, fps)


# ---------------------------------------------------------------------------
#  异常
#
#  遵循仓库约定: 运行期故障 -> RuntimeError; 参数/证书写错 -> ValueError。
# ---------------------------------------------------------------------------


class SunshineError(RuntimeError):
    """握手失败: 网络不通 / HTTP 报错 / 响应解析不出东西。"""


class SunshineConfigError(ValueError):
    """参数或证书文件有问题 (当场能改对再跑)。"""


# ---------------------------------------------------------------------------
#  数据
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class ServerInfo:
    """/serverinfo 里我们关心的字段 (与 native 的 ServerInfo 结构对应)。"""

    hostname: str = ""
    app_version: str = ""
    gfe_version: str = ""
    unique_id: str = ""
    codec_mode_support: int = 0
    pair_status: int = -1
    status_code: int = -1
    raw: str = ""

    @property
    def paired(self) -> bool:
        """Sunshine 授权名单里有这个客户端时为 True (PairStatus == 1)。"""
        return self.pair_status == 1

    def __repr__(self) -> str:  # 不打印 raw, 免得日志里塞整段 XML
        return (
            "ServerInfo(hostname=%r, app_version=%r, pair_status=%d, "
            "codec_mode_support=%d)"
            % (self.hostname, self.app_version, self.pair_status, self.codec_mode_support)
        )


@dataclasses.dataclass(frozen=True)
class AppEntry:
    """/applist 里的一个应用。"""

    app_id: str
    title: str

    def __repr__(self) -> str:
        return "AppEntry(app_id=%r, title=%r)" % (self.app_id, self.title)


@dataclasses.dataclass(frozen=True)
class LaunchResult:
    """/launch 的结果。session_url 就是 moonlight 的 rtspSessionUrl。"""

    session_url: str = ""
    status_code: int = -1
    status_message: str = ""
    raw: str = ""

    @property
    def ok(self) -> bool:
        return self.status_code == 200 and bool(self.session_url)

    def __repr__(self) -> str:
        return (
            "LaunchResult(status_code=%d, session_url=%s)"
            % (self.status_code, "有" if self.session_url else "空")
        )


# ---------------------------------------------------------------------------
#  纯解析函数
#
#  Sunshine 的响应是固定几个字段的 XML, 不需要通用解析器; 越通用越难在小样本上
#  验证 (native/moonlight_connection.cpp 是同样的取舍)。
# ---------------------------------------------------------------------------


def _tag(xml: str, tag: str) -> str:
    """取 <tag>value</tag> 的 value; 找不到返回空串。

    开标签允许带属性 (<tag a="b">value</tag>), 并且不要求标签之间有换行/空白 ——
    Sunshine 会把整个 <App> 打在一行里。
    """
    pattern = r"<%s(?:\s[^>]*)?>(.*?)</%s>" % (re.escape(tag), re.escape(tag))
    match = re.search(pattern, xml, re.S)
    return match.group(1).strip() if match else ""


def _root_attrs(xml: str) -> str:
    """取 <root ...> 里那段属性文本 (只认根元素, 免得匹配到内层同名属性)。"""
    match = re.search(r"<root\b([^>]*)>", xml, re.S)
    return match.group(1) if match else ""


def _attribute(xml: str, name: str) -> str:
    """取 <root name="value"> 的属性值; 找不到返回空串。

    ⚠ Sunshine 的 status_message 是**属性**不是元素 (moonlight_connection.h 里也
      专门记了这条), 用 _tag 是取不到的。
    """
    match = re.search(r'\b%s\s*=\s*"([^"]*)"' % re.escape(name), _root_attrs(xml))
    return match.group(1) if match else ""


def _int(text: str, default: int) -> int:
    try:
        return int(text)
    except (TypeError, ValueError):
        return default


def status_code_of(xml: str) -> int:
    """取 <root status_code="200"> 里的 status_code; 解析失败返回 -1。"""
    return _int(_attribute(xml, "status_code"), -1)


def parse_server_info(xml: str) -> ServerInfo:
    """解析 /serverinfo 的响应体。"""
    return ServerInfo(
        hostname=_tag(xml, "hostname"),
        app_version=_tag(xml, "appversion"),
        gfe_version=_tag(xml, "GfeVersion"),
        unique_id=_tag(xml, "uniqueid"),
        codec_mode_support=_int(_tag(xml, "ServerCodecModeSupport"), 0),
        pair_status=_int(_tag(xml, "PairStatus"), -1),
        status_code=status_code_of(xml),
        raw=xml,
    )


def parse_app_list(xml: str) -> List[AppEntry]:
    """解析 /applist 的响应体, 按出现顺序返回。没有 <ID> 的条目直接跳过。"""
    apps: List[AppEntry] = []
    for block in re.findall(r"<App>(.*?)</App>", xml, re.S):
        app_id = _tag(block, "ID")
        if not app_id:
            continue
        apps.append(AppEntry(app_id=app_id, title=_tag(block, "AppTitle")))
    return apps


def parse_launch(xml: str) -> LaunchResult:
    """解析 /launch 的响应体。"""
    return LaunchResult(
        session_url=_tag(xml, "sessionUrl0"),
        status_code=status_code_of(xml),
        status_message=_attribute(xml, "status_message"),
        raw=xml,
    )


def build_launch_query(unique_id: str,
                       app_id: str,
                       mode: str,
                       extra: str = "") -> str:
    """拼 /launch 的 query 串 (不含开头的 '?')。

    逐项与 scripts/verify-authorized.sh 一致; extra 放 native 给的
    LiGetLaunchUrlQueryParameters() (形如 "&corever=1")。
    """
    if not unique_id:
        raise SunshineConfigError("unique_id 不能为空 (必须与配对时一致)")
    if not app_id:
        raise SunshineConfigError("app_id 不能为空")
    if not mode:
        raise SunshineConfigError("mode 不能为空 (形如 1280x720x60)")

    pairs: Sequence[Tuple[str, str]] = (
        ("uniqueid", unique_id),
        ("appid", app_id),
        ("mode", mode),
        ("rikey", ZERO_RIKEY),
        ("rikeyid", "0"),
        ("localAudioPlayMode", "0"),
        ("surroundAudioInfo", str(surround_audio_info())),
        ("gcmap", "0"),
        ("hdrMode", "0"),
        ("sops", "0"),
    )
    query = "&".join("%s=%s" % (key, value) for key, value in pairs)
    if extra:
        query += extra if extra.startswith("&") else "&" + extra
    return query


def is_already_running(status_code: int, status_message: str = "", body: str = "") -> bool:
    """/launch 的这次失败是不是"主机上已经有应用在跑"。

    是的话就该退到 /resume 加入已有会话 (与别的 Moonlight 客户端共存), 而不是报错。
    """
    text = ("%s %s" % (status_message or "", body or "")).lower()
    if any(hint in text for hint in ALREADY_RUNNING_HINTS):
        return True
    return status_code in ALREADY_RUNNING_CODES


class SessionStart(NamedTuple):
    """start_session() 的结果: 会话 URL + 实际用的是哪个 endpoint。"""

    result: LaunchResult
    endpoint: str  # "launch" 或 "resume"

    @property
    def session_url(self) -> str:
        return self.result.session_url

    def __repr__(self) -> str:
        return "SessionStart(endpoint=%r, session_url=%s)" % (
            self.endpoint,
            "有" if self.session_url else "空",
        )


# ---------------------------------------------------------------------------
#  客户端
# ---------------------------------------------------------------------------


class SunshineClient:
    """Sunshine 的 HTTPS 握手客户端 (同步阻塞; 调用方用线程跑)。

    只做三件事: 问 /serverinfo 要 app_version, 问 /applist 要 appid,
    问 /launch 要 sessionUrl0。拿到之后交给 native 的
    moonlight.start_with_session()。
    """

    def __init__(self,
                 host: str,
                 https_port: int = DEFAULT_HTTPS_PORT,
                 cert: Optional[str] = None,
                 key: Optional[str] = None,
                 unique_id: str = DEFAULT_UNIQUE_ID,
                 timeout: float = DEFAULT_TIMEOUT,
                 launch_extra_query: str = "",
                 logger=None) -> None:
        if not host or not str(host).strip():
            raise SunshineConfigError("sunshine.host 不能为空")
        if not isinstance(https_port, int) or isinstance(https_port, bool):
            raise SunshineConfigError("https_port 必须是整数: %r" % (https_port,))
        if not 0 < https_port < 65536:
            raise SunshineConfigError("https_port 越界: %d" % https_port)
        if timeout is not None and timeout <= 0:
            raise SunshineConfigError("timeout 必须为正数: %r" % (timeout,))
        if (cert is None) != (key is None):
            raise SunshineConfigError(
                "客户端证书和私钥必须成对提供 (cert=%r, key=%r)" % (cert, key)
            )

        self.host = str(host).strip()
        self.https_port = https_port
        self.unique_id = unique_id or DEFAULT_UNIQUE_ID
        self.timeout = float(timeout) if timeout else DEFAULT_TIMEOUT
        self.launch_extra_query = launch_extra_query or ""
        self.cert = self._existing_file(cert, "cert") if cert else None
        self.key = self._existing_file(key, "key") if key else None
        self._log = logger
        # 建上下文时就把证书读进来: 证书坏了应该在启动时就报, 而不是握手时才报
        self._context = self._build_context()

    # ------------------------------------------------------------ 内部 ----

    @staticmethod
    def _existing_file(path, what: str) -> str:
        resolved = Path(path)
        if not resolved.is_file():
            raise SunshineConfigError(
                "客户端%s文件不存在: %s —— 配对时生成, 路径见 config/config.yaml 的 "
                "sunshine.%s" % ("证书" if what == "cert" else "私钥", resolved, what)
            )
        return str(resolved)

    def _build_context(self) -> ssl.SSLContext:
        # Sunshine 用自签证书, 客户端认证靠客户端证书 —— 与 verify-authorized.sh 的
        # `curl -sk` 等价: 不校验证书链, 但要把客户端证书递上去。
        # PROTOCOL_TLS_CLIENT 默认 check_hostname=True / CERT_REQUIRED, 两个都要关。
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        if self.cert:
            try:
                context.load_cert_chain(certfile=self.cert, keyfile=self.key)
            except (ssl.SSLError, OSError) as exc:
                raise SunshineConfigError(
                    "客户端证书加载失败 (%s / %s): %s" % (self.cert, self.key, exc)
                ) from exc
        return context

    def _get(self, path_and_query: str) -> str:
        url = "https://%s:%d%s" % (self.host, self.https_port, path_and_query)
        request = urlrequest.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urlrequest.urlopen(
                request, timeout=self.timeout, context=self._context
            ) as response:
                return response.read().decode("utf-8", "replace")
        except urlerror.HTTPError as exc:
            # HTTPError 本身是个 response: 身体里往往有 Sunshine 的说明, 要读出来
            body = ""
            try:
                body = exc.read().decode("utf-8", "replace")
            except Exception:  # noqa: BLE001 - 读不到就算了, 不能因此盖掉原错误
                body = ""
            finally:
                # 它持有 fp (urllib 会落到临时文件), 不关掉会漏, 且在 GC 时报
                # ResourceWarning
                exc.close()
            raise SunshineError(self._http_error_message(exc.code, body)) from exc
        except urlerror.URLError as exc:
            raise SunshineError(self._url_error_message(exc.reason)) from exc
        except (socket.timeout, TimeoutError) as exc:
            raise SunshineError(
                "握手超时 (%.1fs): %s —— 主机在但不响应? 或防火墙拦了 47984?"
                % (self.timeout, url)
            ) from exc
        except ssl.SSLError as exc:
            raise SunshineError("TLS 握手失败: %s (%s)" % (exc, url)) from exc

    def _http_error_message(self, code: int, body: str) -> str:
        hint = {
            401: "客户端证书未被主机授权 —— 需要配对/加入 Sunshine 名单 "
                 "(见 scripts/pair_sunshine.py), 且 uniqueid 必须与配对时一致",
            403: "主机拒绝了这次请求 (证书可能在名单里但被禁用)",
            404: "接口不存在 —— 端口是不是填成了 HTTP 的 47989?",
        }.get(code, "主机返回了错误状态")
        message = "HTTPS %d: %s" % (code, hint)
        if body:
            detail = _tag(body, "status_message") or _attribute(body, "status_message")
            message += " | 主机原话: %s" % (detail or body.strip()[:200])
        return message

    def _url_error_message(self, reason) -> str:
        text = str(reason)
        if isinstance(reason, ConnectionRefusedError) or "refused" in text.lower():
            return (
                "连不上 %s:%d (连接被拒) —— 主机上 Sunshine 在跑吗? 47984 是它的 "
                "HTTPS 端口" % (self.host, self.https_port)
            )
        if isinstance(reason, socket.gaierror):
            return "主机名解析失败: %s (%s)" % (self.host, text)
        if "timed out" in text.lower():
            return "连接 %s:%d 超时 (%.1fs)" % (self.host, self.https_port, self.timeout)
        return "连接 %s:%d 失败: %s" % (self.host, self.https_port, text)

    def _note(self, message: str) -> None:
        """只在传了 logger 时记一笔 —— 本模块不强制依赖日志框架。"""
        if self._log is not None:
            self._log.debug("sunshine: %s", message)

    # ------------------------------------------------------------ 接口 ----

    def server_info(self) -> ServerInfo:
        """GET /serverinfo —— 拿 app_version / GfeVersion / codec 支持 / PairStatus。"""
        body = self._get("/serverinfo?uniqueid=%s" % self.unique_id)
        info = parse_server_info(body)
        self._note("serverinfo: %r" % (info,))
        if not info.app_version:
            raise SunshineError(
                "/serverinfo 里没有 appversion (HTTP status_code=%d) —— 没有它无法 "
                "启动会话; 响应开头: %s" % (info.status_code, body.strip()[:160])
            )
        return info

    def app_list(self) -> List[AppEntry]:
        """GET /applist —— 需要已授权的客户端证书。"""
        body = self._get("/applist?uniqueid=%s" % self.unique_id)
        apps = parse_app_list(body)
        self._note("applist: %d 个应用" % len(apps))
        if not apps:
            raise SunshineError(
                "/applist 没解析出任何应用 (HTTP status_code=%d) —— 证书被授权了吗? "
                "响应开头: %s" % (status_code_of(body), body.strip()[:160])
            )
        return apps

    def resolve_app_id(self, app: str) -> str:
        """把 sunshine.app 解析成 appid。

        先按 appid 精确匹配, 再按标题忽略大小写匹配; 都对不上就报错并列出主机上
        有哪些应用 —— 静默退回"第一个应用"只会让人以为启动对了。
        """
        wanted = str(app).strip()
        if not wanted:
            raise SunshineConfigError("sunshine.app 不能为空")

        apps = self.app_list()
        for entry in apps:
            if entry.app_id == wanted:
                return entry.app_id
        for entry in apps:
            if entry.title.lower() == wanted.lower():
                return entry.app_id

        available = ", ".join("%s(appid=%s)" % (e.title, e.app_id) for e in apps)
        raise SunshineError(
            "主机上没有名为 %r 的应用; 可用: %s" % (wanted, available)
        )

    def _session_request(self, endpoint: str, app_id: str, mode: str) -> LaunchResult:
        """请求 /launch 或 /resume, 只解析不定性 (成败判断留给调用方)。"""
        query = build_launch_query(
            self.unique_id, app_id, mode, self.launch_extra_query
        )
        body = self._get("/%s?%s" % (endpoint, query))
        result = parse_launch(body)
        self._note("%s: %r" % (endpoint, result))
        return result

    @staticmethod
    def _session_failure(endpoint: str, result: LaunchResult) -> str:
        # Sunshine 会把"已有会话 / 应用启动失败"这类原因放在 status_message 里,
        # 原样带出去 —— 比只说"没拿到 sessionUrl0"有用得多。
        return "/%s 没拿到 sessionUrl0 (status_code=%d): %s" % (
            endpoint,
            result.status_code,
            result.status_message or result.raw.strip()[:200],
        )

    def launch(self, app_id: str, mode: str) -> LaunchResult:
        """GET /launch —— 启动应用, 拿 sessionUrl0 (就是 moonlight 的 rtspSessionUrl)。

        ⚠ 主机上已有应用在跑时这里必然失败 (实测 status_code=400)。要共存请用
          start_session(), 它会自动退到 /resume。
        """
        result = self._session_request("launch", app_id, mode)
        if not result.ok:
            raise SunshineError(self._session_failure("launch", result))
        return result

    def resume(self, app_id: str, mode: str) -> LaunchResult:
        """GET /resume —— 主机上已有应用在跑时**加入**那个会话 (与别的客户端共存)。

        实测 Sunshine 在 appid 对不上时也返回同一个会话 (响应里带 <resume>1</resume>),
        所以 app_id 给 0 也能用; 这里仍然把真实 appid 带上, 语义更清楚。
        """
        result = self._session_request("resume", app_id, mode)
        if not result.ok:
            raise SunshineError(self._session_failure("resume", result))
        return result

    def start_session(self, app_id: str, mode: str) -> SessionStart:
        """启动**或**加入会话: 先 /launch, 主机已在运行就退到 /resume。

        这才是 Agent 该用的入口 —— 主机上同时挂着别的 Moonlight 客户端是正常情况,
        这时 /launch 必然 400, 而 /resume 能拿到同一个会话的 sessionUrl0。
        """
        launched = self._session_request("launch", app_id, mode)
        if launched.ok:
            return SessionStart(launched, "launch")

        if not is_already_running(launched.status_code, launched.status_message, launched.raw):
            # 不是"主机忙"就原样报错, 别拿 /resume 去掩盖真问题
            raise SunshineError(self._session_failure("launch", launched))

        self._note("主机已在运行应用, 退到 /resume 加入已有会话")
        resumed = self._session_request("resume", app_id, mode)
        if resumed.ok:
            return SessionStart(resumed, "resume")

        raise SunshineError(
            "%s | /resume 也没拿到 sessionUrl0 (status_code=%d): %s"
            % (
                self._session_failure("launch", launched),
                resumed.status_code,
                resumed.status_message or resumed.raw.strip()[:200],
            )
        )

    def __repr__(self) -> str:
        return "SunshineClient(host=%r, https_port=%d, cert=%s)" % (
            self.host,
            self.https_port,
            "有" if self.cert else "无",
        )
