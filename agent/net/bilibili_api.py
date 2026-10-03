# ============================================================================
#  agent/net/bilibili_api.py — B 站的**唯一网络层**（Phase 7 T11-1）
#
#  它是什么: 板端直连 B 站那几个公开 JSON 接口（**标准库 urllib，不加依赖**）:
#              search      关键词 -> 视频列表（含 bvid/标题/作者/播放量/时长/封面）
#              view        bvid -> cid / 分P数 / 时长
#              playurl     bvid+cid -> **可播的直链**（单文件 mp4 优先, 拿不到才 DASH）
#              nav         登录态自检（cookie 有没有失效）
#
#  为什么只有这一层碰 B 站: 接口是**非官方**的，随时会改/加风控。把字段解析、错误翻译、
#  清晰度阶梯全关在这一个文件里，坏了只修这一处（上层 core/tools 只认这里给的形状）。
#
#  T11-0 板端实测（写代码前先量的，注释里引用的数字都出自它）:
#   · **匿名就能用**，但必须先访问一次首页拿 `buvid3`/`b_nut`（不带 -> 风控 **412**）。
#   · **`order=play` 不生效**（回落 B 站综合排序）；真正按播放量降序的是 **`order=click`**。
#     我们要的"综合排序"就是 **`order=totalrank`**（跨页一致、分页稳定）。
#   · 同一页连取 3 次**完全一致**；但 **`page > numPages` 不报错、会回重复内容** ——
#     所以边界只能按 `numPages` 判（调用方别指望"返回空"）。
#   · `platform=html5&fnval=1` 给**单文件 mp4**（360P/720P，有的视频给 1080P）；
#     `platform=pc&fnval=16` 给 **DASH（音视频分离）**，**带 cookie 才从 720P 抬到 1080P**
#     （匿名 DASH 只给到 480P）→ 所以高清必须"带 cookie + DASH + ffmpeg 合流"。
#   · **ffmpeg/下载器拉 CDN 直链必须带 UA + Referer**，否则两个 CDN 都回 **403**。
#
#  谁用它: `agent/core/bilibili.py`（队列）/ `agent/core/bilibili_buffer.py`（合流+缓冲）
#          / `agent/core/game_watch.py`（画面认游戏后搜队列）。**工具层不直接用它**。
#
#  ⚠ 本模块 import 时不联网: 只有真正调用方法才发请求；`transport` 可注入（单测全离线）。
# ============================================================================

from __future__ import annotations

import html
import json
import logging
import os
import re
from typing import Any, Dict, List, Mapping, Optional, Tuple

__all__ = [
    "BilibiliApi",
    "BilibiliError",
    "BilibiliNetworkError",
    "BilibiliRiskError",
    "BilibiliAuthError",
    "BilibiliNotFound",
    "BilibiliPayloadError",
    "UrllibTransport",
    "load_cookie_file",
    "clean_title",
    "parse_duration_text",
    "quality_label",
    "DEFAULT_UA",
    "DEFAULT_COOKIE_FILE",
    "DEFAULT_TIMEOUT_S",
    "SEARCH_ORDERS",
    "HOME_URL",
]

_log = logging.getLogger(__name__)

#: 桌面 Chrome 的 UA。**不要改成移动端 UA**（实测同一视频三种 UA 阶梯一样，但移动 UA 更容易被
#: 当成爬虫；保持不变省得以后踩坑）。
DEFAULT_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

HOME_URL = "https://www.bilibili.com/"
SEARCH_URL = "https://api.bilibili.com/x/web-interface/search/type"
VIEW_URL = "https://api.bilibili.com/x/web-interface/view"
PLAYURL_URL = "https://api.bilibili.com/x/player/playurl"
NAV_URL = "https://api.bilibili.com/x/web-interface/nav"

#: cookie 文件默认位置（**凭据**，进 .gitignore；不存在/为空 = 匿名）。
DEFAULT_COOKIE_FILE = "config/bilibili_cookie.json"
DEFAULT_TIMEOUT_S = 15.0

#: `search` 的 order 取值。**默认 totalrank = B 站综合排序**（分页一致，队列要的就是它）;
#: `click` 是实测**真的按播放量降序**的那个（`play` 不生效）。
SEARCH_ORDERS = ("totalrank", "click", "pubdate", "dm", "stow")

#: 清晰度码 -> 给人看的名字（16=360P、64=720P、80=1080P、112=1080P+、120=4K）。
QUALITY_LABELS = {6: "240P", 16: "360P", 32: "480P", 64: "720P", 74: "720P60",
                  80: "1080P", 112: "1080P+", 116: "1080P60", 120: "4K", 125: "HDR", 127: "8K"}

#: 风控（要"等一会再试"而不是"改配置"）。
RISK_CODES = (412, -352, -412, -509)
#: 视频没了/被删（各接口给不同的码）。
NOT_FOUND_CODES = (-404, 62002, 62004)
#: 登录态失效。
AUTH_CODES = (-101, -400)


# ---------------------------------------------------------------------------
#  异常：消息都是**给人看的**（会经工具/气泡显示到界面上），要求"能照做"
# ---------------------------------------------------------------------------
class BilibiliError(RuntimeError):
    """B 站这条路出错（基类）。消息是给人看的、能照做。"""


class BilibiliNetworkError(BilibiliError):
    """连不上 / 超时 / HTTP 层就失败了。"""


class BilibiliRiskError(BilibiliError):
    """被 B 站风控（412 / -352）。**等几分钟再试**，改配置没用。"""


class BilibiliAuthError(BilibiliError):
    """cookie 失效/没登录：去更新 `config/bilibili_cookie.json`。"""


class BilibiliNotFound(BilibiliError):
    """视频不存在 / 已删除 / 地区限制。"""


class BilibiliPayloadError(BilibiliError):
    """响应形状不对（接口改了 / 被中间设备改了）—— 如实报，不猜。"""


# ---------------------------------------------------------------------------
#  纯函数（可脱离网络单测）
# ---------------------------------------------------------------------------
_TAG_RE = re.compile(r"<[^>]+>")


def clean_title(text: Any) -> str:
    """搜索结果里的标题 -> 干净标题。

    @note 接口把关键词包在 `<em class="keyword">…</em>` 里（**转义成 `\\u003cem`**），
          还有 `&amp;` 这类实体 —— 实测原文长这样，直接显示会带一堆尖括号。
    """
    raw = html.unescape(str(text or ""))
    return _TAG_RE.sub("", raw).strip()


def parse_duration_text(text: Any) -> int:
    """搜索结果里的 `duration` 文本 -> 秒。

    @note 实测格式: `"3:4"`（分:秒）、`"233:45"`（**分可以超过 60**）、`"1:02:03"`。
          `view.duration` 是权威秒数，能用就用它，这个只是兜底。
    @return 秒；解析不出来返回 0（调用方当"不知道"）。
    """
    parts = [p for p in str(text or "").strip().split(":") if p != ""]
    if not parts or len(parts) > 3:
        return 0
    try:
        numbers = [int(p) for p in parts]
    except ValueError:
        return 0
    if len(numbers) == 3:
        return numbers[0] * 3600 + numbers[1] * 60 + numbers[2]
    if len(numbers) == 2:
        return numbers[0] * 60 + numbers[1]
    return numbers[0]


def quality_label(quality: Any) -> str:
    """清晰度码 -> "1080P" 这种给人看的名字（不认识就回 `"未知清晰度(码 N)"`）。"""
    try:
        code = int(quality)
    except (TypeError, ValueError):
        return "未知清晰度"
    return QUALITY_LABELS.get(code, "未知清晰度(码 %d)" % code)


def _abs_url(url: Any) -> str:
    """`//i0.hdslb.com/...` -> `https://i0.hdslb.com/...`（封面/头像都是协议相对写法）。"""
    text = str(url or "").strip()
    if text.startswith("//"):
        return "https:" + text
    return text


def load_cookie_file(path: Optional[str]) -> Tuple[Dict[str, str], List[str]]:
    """读 cookie 文件 -> (名字->值, 提醒列表)。**任何异常都不抛**，当匿名处理。

    @note 认的是 `SESSDATA`。实测踩到过一种**手写笔误**：键名写成 `SEESSDATA`（多一个 E）——
          B 站只认 `SESSDATA`（用错键名时 `nav` 回 `-101`）。这里按语义接受并**如实提醒**，
          不静默当匿名（那样用户会以为"配了却没用"）。
    @return 第二个元素是给日志/回复看的提醒（可能为空）。
    """
    notes: List[str] = []
    text = str(path or "").strip()
    if not text:
        return {}, notes
    if not os.path.exists(text):
        return {}, notes                     # 没有文件 = 匿名，不是错误
    try:
        with open(text, encoding="utf-8") as handle:
            raw = json.load(handle)
    except (OSError, ValueError) as exc:
        notes.append("cookie 文件读不了（%s）—— 按匿名处理" % exc)
        return {}, notes
    if not isinstance(raw, Mapping):
        notes.append("cookie 文件里不是个 JSON 对象 —— 按匿名处理")
        return {}, notes
    out: Dict[str, str] = {}
    for key, value in raw.items():
        name = str(key).strip()
        val = str(value or "").strip()
        if not name or not val:
            continue
        if name == "SEESSDATA":
            notes.append("cookie 文件里的键名是 SEESSDATA（多了一个 E），B 站只认 SESSDATA —— "
                         "已按 SESSDATA 使用，建议把键名改对")
            name = "SESSDATA"
        out[name] = val
    if out and "SESSDATA" not in out:
        notes.append("cookie 文件里没有 SESSDATA（只有 %s）—— 只能拿匿名清晰度"
                     % "、".join(sorted(out)))
    return out, notes


# ---------------------------------------------------------------------------
#  传输层（真实现用 urllib + cookiejar；单测注入假的）
# ---------------------------------------------------------------------------
class UrllibTransport(object):
    """默认传输：urllib + CookieJar（匿名会话的 `buvid3` 就存在这个 jar 里）。"""

    def __init__(self, timeout_s: float = DEFAULT_TIMEOUT_S) -> None:
        self.timeout_s = float(timeout_s or DEFAULT_TIMEOUT_S)
        import http.cookiejar
        import urllib.request

        self._cookiejar = http.cookiejar.CookieJar()
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self._cookiejar))

    def get(self, url: str, headers: Optional[Mapping[str, str]] = None,
            timeout_s: Optional[float] = None) -> Tuple[int, str]:
        """发一次 GET。@return (状态码, 文本)。**非 2xx 不抛**（交给上层按 B 站习惯解读）。"""
        import urllib.error
        import urllib.request

        request = urllib.request.Request(url, headers=dict(headers or {}))
        limit = float(timeout_s or self.timeout_s)
        try:
            with self._opener.open(request, timeout=limit) as response:
                body = response.read().decode("utf-8", "replace")
                return int(getattr(response, "status", 200) or 200), body
        except urllib.error.HTTPError as exc:
            # 412 是 B 站的风控**页面**（不是 JSON），所以要带上状态码让上层翻译
            body = ""
            try:
                body = exc.read().decode("utf-8", "replace")
            except Exception:                       # noqa: BLE001
                pass
            return int(getattr(exc, "code", 0) or 0), body
        except Exception as exc:                    # noqa: BLE001 - 超时/连不上都归这类
            raise BilibiliNetworkError(
                "连不上 B 站（%s）—— 检查板端网络；网络恢复后再试一次" % exc) from exc

    def cookie_header(self) -> str:
        """匿名 jar 里的 cookie（`buvid3`/`b_nut`…）拼成请求头用的一行。"""
        return "; ".join("%s=%s" % (cookie.name, cookie.value)
                         for cookie in self._cookiejar)


# ---------------------------------------------------------------------------
#  唯一的 B 站客户端
# ---------------------------------------------------------------------------
class BilibiliApi(object):
    """B 站公开接口客户端。**上层只用这里给的形状**（不自己解析 B 站字段）。

    典型用法::

        api = BilibiliApi.from_config(config.get("bilibili"))
        found = api.search("luna say maybe")          # 队列就是从 items 来的
        info = api.view(found["items"][0]["bvid"])
        stream = api.playurl(info["bvid"], info["cid"])   # 单文件 或 DASH
        args = api.ffmpeg_input_args(info["bvid"], stream["url"])   # 交给 ffmpeg -c copy
    """

    def __init__(self, *, cookie_file: Optional[str] = None,
                 transport: Optional[Any] = None,
                 timeout_s: float = DEFAULT_TIMEOUT_S,
                 log: Optional[logging.Logger] = None) -> None:
        """
        @param cookie_file 凭据文件（None/缺省 -> 默认位置；文件不在 = 匿名）
        @param transport   `get(url, headers) -> (status, text)` + `cookie_header()`；
                           不传就用 `UrllibTransport`（单测一律注入假的）
        """
        self.log = log or _log
        self.timeout_s = float(timeout_s or DEFAULT_TIMEOUT_S)
        self.cookie_file = str(cookie_file or DEFAULT_COOKIE_FILE)
        self.cookie, self.cookie_notes = load_cookie_file(self.cookie_file)
        #: ⚑ T11-9 板端验收抓出来的真问题：cookie **失效**时 DASH 那条路被
        #:   `playurl` 的兜底吞掉（它会退回单文件，视频照样能放），于是**没人告诉用户**
        #:   "cookie 失效了" —— 用户看到的会是"这条 B 站只给到 720P，和登不登录无关"，
        #:   而真相是"配上有效的 SESSDATA 就是 1080P"。所以把那条话**记在这里**，
        #:   由 `Runtime._bilibili_quality_note()` 优先说出去。
        self.auth_note = ""
        self._transport = transport if transport is not None else UrllibTransport(self.timeout_s)
        self._booted = False
        self._login: Optional[Dict[str, Any]] = None
        for note in self.cookie_notes:
            self.log.warning("bilibili: %s", note)

    # ------------------------------------------------------------ 构造 ---
    @classmethod
    def from_config(cls, section: Optional[Mapping[str, Any]] = None,
                    transport: Optional[Any] = None,
                    log: Optional[logging.Logger] = None) -> Optional["BilibiliApi"]:
        """从 `config.yaml` 的 `bilibili:` 段构造。`enabled: false` -> None（工具整个不装）。"""
        data = dict(section or {})
        if not bool(data.get("enabled", True)):
            _log.info("bilibili: 没开（config 的 bilibili.enabled）")
            return None
        path = data.get("cookie_file") or DEFAULT_COOKIE_FILE
        if isinstance(path, str) and path and not os.path.isabs(path):
            # 相对路径按**仓库根**解析（与 wall_data / music_library / user_profile 同款）
            root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
            path = os.path.normpath(os.path.join(root, path))
        timeout_s = data.get("timeout_s") or DEFAULT_TIMEOUT_S
        return cls(cookie_file=str(path), transport=transport,
                   timeout_s=float(timeout_s), log=log)

    # ------------------------------------------------------------ 会话 ---
    @property
    def cookie_present(self) -> bool:
        """有没有配 SESSDATA（**不代表还有效**——有效性只有 `login()` 知道）。"""
        return bool(self.cookie.get("SESSDATA"))

    def session(self) -> None:
        """懒初始化匿名会话：访问一次首页，把 `buvid3`/`b_nut` 收进 jar（幂等）。

        @note 实测：不带这个 cookie 直接打接口 -> **412**；带上 -> 200。
        """
        if self._booted:
            return
        self._booted = True
        self._transport.get(HOME_URL, {"User-Agent": DEFAULT_UA, "Accept": "text/html"})

    def request_headers(self, referer: str = HOME_URL) -> Dict[str, str]:
        """接口请求的公共头：UA + Referer + cookie（匿名 jar + 文件里的 SESSDATA）。"""
        headers = {"User-Agent": DEFAULT_UA, "Referer": referer,
                   "Accept": "application/json, text/plain, */*"}
        parts = []
        jar = ""
        getter = getattr(self._transport, "cookie_header", None)
        if callable(getter):
            jar = str(getter() or "")
        if jar:
            parts.append(jar)
        if self.cookie_present:
            parts.append("SESSDATA=%s" % self.cookie["SESSDATA"])
        if parts:
            headers["Cookie"] = "; ".join(parts)
        return headers

    def media_headers(self, bvid: str) -> Dict[str, str]:
        """**CDN 直链**要的头（实测不带就 403）：UA + 视频页 Referer + cookie。"""
        headers = {"User-Agent": DEFAULT_UA,
                   "Referer": "https://www.bilibili.com/video/%s" % bvid}
        if self.cookie_present:
            headers["Cookie"] = "SESSDATA=%s" % self.cookie["SESSDATA"]
        return headers

    def ffmpeg_input_args(self, bvid: str, url: str) -> List[str]:
        """拼出「让 ffmpeg 读这条直链」的参数（每个输入都要来一份）。

        @note 实测：ffmpeg 默认 UA（Lavf）+ 没 Referer -> **403**；带上就正常。
        @note `-headers` 的值必须以 **CRLF** 结尾，这是 ffmpeg 的约定。
        """
        return ["-user_agent", DEFAULT_UA,
                "-headers", "Referer: https://www.bilibili.com/video/%s\r\n" % bvid,
                "-i", url]

    # ------------------------------------------------------------ 内部 ---
    def _json(self, url: str, *, what: str, referer: str = HOME_URL) -> Any:
        """发请求 + 解析 + **把 B 站的错误码翻译成能照做的话**。"""
        self.session()
        status, text = self._transport.get(url, self.request_headers(referer))
        if status == 412 or status in RISK_CODES:
            raise BilibiliRiskError(
                "被 B 站风控了（HTTP %s）—— 等几分钟再试，别连着刷" % status)
        if status and (status < 200 or status >= 300):
            raise BilibiliNetworkError("B 站回了 HTTP %s（%s）—— 过一会再试" % (status, what))
        try:
            payload = json.loads(text)
        except ValueError as exc:
            raise BilibiliPayloadError(
                "%s 的响应不是 JSON（可能被风控或接口变了）: %s" % (what, text[:120])) from exc
        if not isinstance(payload, Mapping):
            raise BilibiliPayloadError("%s 的响应形状不对" % what)
        code = payload.get("code")
        if code in (0, None):
            data = payload.get("data")
            return data if data is not None else {}
        return self._raise_for_code(int(code) if isinstance(code, int) else -1,
                                    str(payload.get("message") or ""), what)

    def _raise_for_code(self, code: int, message: str, what: str) -> Any:
        if code in RISK_CODES:
            raise BilibiliRiskError("被 B 站风控了（code=%s %s）—— 等几分钟再试" % (code, message))
        if code in AUTH_CODES:
            raise BilibiliAuthError(
                "B 站的登录态失效了（code=%s %s）—— 去更新 config/bilibili_cookie.json 里的 "
                "SESSDATA（不改也能用，只是清晰度只有匿名档）" % (code, message))
        if code in NOT_FOUND_CODES:
            raise BilibiliNotFound("这个视频看不了了（code=%s %s）—— 换一个" % (code, message))
        raise BilibiliError("%s 出错（code=%s %s）" % (what, code, message))

    # ------------------------------------------------------------ 搜索 ---
    def search(self, keyword: str, page: int = 1, order: str = "totalrank",
               limit: int = 0) -> Dict[str, Any]:
        """搜视频。@return {"keyword","page","num_pages","num_results","items":[…]}

        @param order `totalrank`（默认，B 站综合排序）/ `click`（**实测真按播放量降序**）…
        @param limit >0 时截断 items（队列窗口用）
        @note **边界按 `num_pages` 判**：实测 `page > numPages` 不报错、会回重复内容 ——
              调用方别用"结果为空"当到底了。
        """
        text = str(keyword or "").strip()
        if not text:
            raise BilibiliPayloadError("要搜什么？给我一个关键词")
        order = str(order or "totalrank").strip().lower()
        if order not in SEARCH_ORDERS:
            order = "totalrank"
        try:
            page = max(1, int(page or 1))
        except (TypeError, ValueError):
            page = 1
        url = ("%s?search_type=video&keyword=%s&order=%s&page=%d"
               % (SEARCH_URL, _quote(text), order, page))
        data = self._json(url, what="搜索")
        if not isinstance(data, Mapping):
            data = {}
        rows = data.get("result") or []
        items: List[Dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            bvid = str(row.get("bvid") or "").strip()
            if not bvid:
                continue
            items.append({
                "bvid": bvid,
                "title": clean_title(row.get("title")),
                "author": str(row.get("author") or ""),
                "play": _int(row.get("play")),
                "like": _int(row.get("like")),
                "duration_s": parse_duration_text(row.get("duration")),
                "duration_text": str(row.get("duration") or ""),
                "cover": _abs_url(row.get("pic")),
                "url": "https://www.bilibili.com/video/%s" % bvid,
                "pubdate": _int(row.get("pubdate")),
            })
        if limit and limit > 0:
            items = items[:int(limit)]
        return {"keyword": text, "page": page, "order": order,
                "num_pages": _int(data.get("numPages")), "num_results": _int(data.get("numResults")),
                "items": items}

    # ------------------------------------------------------------ 详情 ---
    def view(self, bvid: str) -> Dict[str, Any]:
        """视频详情（拿 `cid` 才能要播放地址）。"""
        text = str(bvid or "").strip()
        if not text:
            raise BilibiliPayloadError("没有给 bvid")
        data = self._json("%s?bvid=%s" % (VIEW_URL, _quote(text)), what="视频详情")
        data = data if isinstance(data, Mapping) else {}
        pages = data.get("pages") or []
        return {
            "bvid": str(data.get("bvid") or text),
            "aid": _int(data.get("aid")),
            "cid": _int(data.get("cid")),
            "title": clean_title(data.get("title")),
            "cover": _abs_url(data.get("pic")),
            "duration_s": _int(data.get("duration")),
            "page_count": len(pages) or 1,
            "owner": str((data.get("owner") or {}).get("name") or ""),
        }

    # ------------------------------------------------------- 播放地址 ---
    def playurl(self, bvid: str, cid: Optional[int] = None,
                prefer: str = "plain") -> Dict[str, Any]:
        """要可播的直链。

        @param prefer `plain`（默认：**哪条清晰度高用哪条**，同清晰度优先单文件）/ `dash`（强制 DASH）
        @return `{"kind","quality","quality_label","bps", …}`:
                kind=plain -> 多 `url/size/length_ms`
                kind=dash  -> 多 `video{url,bps,id}` 与 `audio{url,bps}`
        @note 实测：**两条路各有胜负**——有的视频单文件就能给 1080P（`Barricades`），
              有的单文件只给 720P、而 **DASH 带 cookie 才给 1080P**（`luna say maybe`）。
              所以有 cookie 时**两条都问、取清晰度高的那条**；同清晰度优先单文件
              （不用合流，省一步 ffmpeg）。
              **没 cookie 时只问单文件**（匿名 DASH 实测只给到 480P，不如单文件；少一次白跑）。
        """
        if cid is None:
            cid = self.view(bvid)["cid"]
        if not cid:
            raise BilibiliPayloadError("拿不到 cid（视频详情里没有）—— 换一个视频")

        plain = self._plain_stream(bvid, cid)
        if prefer == "dash":
            return self._dash_stream(bvid, cid)
        if plain is None:
            if not self.cookie_present:
                raise BilibiliError(
                    "这条视频只给 DASH（音视频分开），而没配 config/bilibili_cookie.json —— "
                    "配上 SESSDATA 才能放高清；或者换一条有单文件版的视频")
            return self._dash_stream(bvid, cid)
        if not self.cookie_present:
            return plain                       # 匿名只走单文件（少一次白跑）
        try:
            dash = self._dash_stream(bvid, cid)
        except BilibiliError as exc:
            self.log.info("bilibili: DASH 那条没拿到（用单文件）: %s", exc)
            if isinstance(exc, BilibiliAuthError):
                # ⚑ 失效的 cookie **不能就这么算了**：记下来，让清晰度那句话如实说
                #   （否则用户以为"这条视频就这样"，而其实是 cookie 过期了）。
                self.auth_note = str(exc)
            return plain
        if _int(dash.get("quality")) > _int(plain.get("quality")):
            return dash
        return plain

    def _plain_stream(self, bvid: str, cid: int) -> Optional[Dict[str, Any]]:
        """单文件 mp4（`platform=html5`）。拿不到就返回 None（**不抛**）。"""
        data = self._playurl_data(bvid, cid, qn=80, fnval=1, platform="html5")
        seg = _first_durl(data)
        if seg is None:
            return None
        size = _int(seg.get("size"))
        length_ms = _int(seg.get("length"))
        bps = (size * 8.0 / (length_ms / 1000.0)) if (size and length_ms) else 0.0
        quality = _int(data.get("quality"))
        return {"kind": "plain", "quality": quality,
                "quality_label": quality_label(quality), "bps": bps,
                "accept_quality": list(data.get("accept_quality") or []),
                "url": str(seg.get("url") or ""), "size": size,
                "length_ms": length_ms, "bvid": bvid, "cid": cid}

    def _dash_stream(self, bvid: str, cid: int) -> Dict[str, Any]:
        """DASH（音视频分离，高清走这条）。@raise BilibiliError 一条流都没有。"""
        data = self._playurl_data(bvid, cid, qn=112 if self.cookie_present else 80,
                                  fnval=16, platform="pc")
        dash = data.get("dash") or {}
        videos = sorted((dash.get("video") or []), key=lambda d: -_int(d.get("id")))
        audios = sorted((dash.get("audio") or []), key=lambda d: -_int(d.get("bandwidth")))
        if not videos:
            raise BilibiliError("这条视频要不到播放地址（可能被版权/地区限制）—— 换一个")
        best = videos[0]
        audio = audios[0] if audios else {}
        quality = _int(data.get("quality")) or _int(best.get("id"))
        bps = float(_int(best.get("bandwidth")) + _int(audio.get("bandwidth")))
        return {"kind": "dash", "quality": quality, "quality_label": quality_label(quality),
                "bps": bps, "bvid": bvid, "cid": cid,
                "video": {"url": str(best.get("baseUrl") or ""),
                          "bps": _int(best.get("bandwidth")), "id": _int(best.get("id"))},
                "audio": {"url": str(audio.get("baseUrl") or ""),
                          "bps": _int(audio.get("bandwidth"))}}

    def _playurl_data(self, bvid: str, cid: int, *, qn: int, fnval: int,
                      platform: str) -> Mapping[str, Any]:
        url = ("%s?bvid=%s&cid=%s&qn=%d&fnval=%d&platform=%s&high_quality=0"
               % (PLAYURL_URL, _quote(bvid), cid, int(qn), int(fnval), platform))
        data = self._json(url, what="播放地址",
                          referer="https://www.bilibili.com/video/%s" % bvid)
        return data if isinstance(data, Mapping) else {}

    # ------------------------------------------------------- 登录态 ---
    def login(self, refresh: bool = False) -> Dict[str, Any]:
        """自检登录态（用主站 nav 接口）。

        @return {"logged_in": bool, "cookie_present": bool, "code": int, "why": str}
        @note **不抛** AuthError：调用方拿它决定"要不要在回复里提醒 cookie 失效"。
        """
        if self._login is not None and not refresh:
            return self._login
        out: Dict[str, Any] = {"logged_in": False, "cookie_present": self.cookie_present,
                               "code": 0, "why": ""}
        try:
            data = self._json("%s?web_location=1550101" % NAV_URL, what="登录态自检")
            out["logged_in"] = bool((data or {}).get("isLogin"))
            out["why"] = "" if out["logged_in"] else (
                "没配 SESSDATA（匿名）" if not self.cookie_present else "SESSDATA 无效或已过期")
        except BilibiliAuthError as exc:
            out["code"] = -101
            out["why"] = str(exc)
        except BilibiliError as exc:
            out["why"] = "登录态没问出来：%s" % exc
        self._login = out
        return out


def _first_durl(data: Mapping[str, Any]) -> Optional[Mapping[str, Any]]:
    durl = data.get("durl") or []
    for seg in durl:
        if isinstance(seg, Mapping) and seg.get("url"):
            return seg
    return None


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _quote(text: str) -> str:
    import urllib.parse

    return urllib.parse.quote(str(text), safe="")


