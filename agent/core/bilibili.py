# ============================================================================
#  agent/core/bilibili.py — B 站**队列内核**（Phase 7 T11-2）
#
#  它是什么: 一条搜索结果队列 —— 只存"地址与元数据"（bvid/标题/作者/时长/播放量/封面 URL）,
#            **视频内容一个字节都不下**。队列**不落盘**（和音乐队列、壁纸窗口同款）。
#
#  两个概念别混（T11-2 返工过一次, 就是因为混了）:
#      · **缓存** `_all`: 已经抓下来的页**按顺序拼起来**（只有元数据, 一页 20 条 ≈ 几 KB）。
#        抓过的页不丢 —— 否则"上一集"走不回去（老窗口模型把脑袋裁掉, 用户就再也回不到上一条）。
#      · **窗口**（给 GUI 预览栏看的）: 缓存里的一个切片, 目标 **3 × 预览栏格数 N**:
#        `[当前位置 - N, 当前位置 - N + 3N)`。窗口是**算出来的**, 不存在"裁剪"。
#        「队列缩小」= 往前走时窗口滑过去, 于是**走得越远, 后面就越缺** -> 就按方向补页。
#
#  补页规矩（老板 T11-0/D4 定的）:
#      · **往哪边走就往哪边补**: 后面不够就抓下一页（`_last_page + 1`）;
#        前面不够（当前位置离缓存开头不足 N 条）就抓上一页（`_first_page - 1`）**前插**。
#      · 目标长度 = 3 × 预览栏格数（GUI 用 `set_viewport(n)` 上报; 没上报就兜底 6 -> 18 条）,
#        硬上限 `queue.max`（默认 60, 防窗口拉得很大时狂翻页）。
#      · 排序用 **B 站综合排序**（`order=totalrank`）—— 只有它跨页一致, "往前/往后"才说得通。
#
#  边界（都按 T11-0 的实测写死）:
#      · **`page > numPages` 不报错、会回重复内容** -> 一律按 `num_pages` 判"后面没有了",
#        永远不请求越界页; `page < 1` = "前面没有了"。
#      · 结果不足 3N（总共就这么多）-> 如实说共多少条, 不当成错误。
#      · 取页失败（网络/风控）-> **能动的先动**, 动不了才如实说"搜不了（…）"。
#
#  谁用它: `agent/core/game_watch.py`（画面认出的游戏 -> `search(..., source="screen")`）/
#          `agent/tools/bilibili.py`（对话关键词 -> `source="dialogue"`）/
#          GUI 的 next/prev/pick 命令（只走 Agent, 不经过 LLM）。
# ============================================================================

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

__all__ = ["BilibiliQueue", "DEFAULT_VIEWPORT", "DEFAULT_QUEUE_MAX", "MAX_VIEWPORT"]

_log = logging.getLogger(__name__)

#: GUI 预览栏没上报格数时的兜底（6 格 -> 队列 18 条）。
DEFAULT_VIEWPORT = 6
#: 预览栏格数上限（防"窗口拉得特别大"把队列推到几百条）。
MAX_VIEWPORT = 20
#: 队列硬上限（条）。
DEFAULT_QUEUE_MAX = 60
#: 一次调用最多补几页（防一次 next 把整个搜索翻穿）。
PAGES_PER_TOPUP = 2


class BilibiliQueue(object):
    """搜索结果队列（缓存 + 3N 滑动窗口）。

    典型用法::

        queue = BilibiliQueue(api, viewport=8)
        queue.search("luna say maybe")          # 抓第 1 页, 窗口 24 条, 当前第 1 条
        queue.next()                            # 下一集（后面不够就抓下一页）
        queue.prev()                            # 上一集（前面不够就抓上一页并前插）
        queue.pick(5)                           # GUI 点了预览栏第 6 个
    """

    def __init__(self, api: Any, *, viewport: Optional[int] = None,
                 queue_max: Optional[int] = None, log: Optional[logging.Logger] = None) -> None:
        """
        @param api `agent/net/bilibili_api.py::BilibiliApi`（或任何有
                   `search(keyword, page, order)` 的同形状对象 —— 单测注入假的）
        """
        self.api = api
        self.log = log or _log
        self.viewport = self._clamp_viewport(viewport)
        self.queue_max = max(1, int(queue_max or DEFAULT_QUEUE_MAX))
        self._reset(keyword="", source="")

    # ------------------------------------------------------------ 只读 ---
    @property
    def items(self) -> List[Dict[str, Any]]:
        """**当前窗口**（拷贝，调用方改不了内部状态）。"""
        start, end = self._window()
        return [dict(item) for item in self._all[start:end]]

    def __len__(self) -> int:
        return len(self.items)

    @property
    def index(self) -> int:
        """当前条在**窗口**里的下标（`items[index]` 就是当前那条）。"""
        start, _end = self._window()
        return self._pos - start

    def target(self) -> int:
        """目标窗口长度 = 3 × 预览栏格数，且不超过 `queue.max`。"""
        return max(1, min(3 * self.viewport, self.queue_max))

    def current(self) -> Optional[Dict[str, Any]]:
        if not self._all:
            return None
        return dict(self._all[self._pos])

    def bvids(self) -> List[str]:
        """**窗口**里的 bvid（当前 GUI 能看到的那些）。"""
        return [str(item.get("bvid") or "") for item in self.items]

    def cached_count(self) -> int:
        """缓存里一共多少条（抓过的页, 不只是窗口）。"""
        return len(self._all)

    def state(self) -> Dict[str, Any]:
        """给日志/GUI/回话用的快照（**纯读**）。"""
        start, end = self._window()
        return {"keyword": self.keyword, "source": self.source,
                "count": end - start, "index": self._pos - start,
                "target": self.target(), "viewport": self.viewport,
                "cached": len(self._all), "first_page": self._first_page,
                "last_page": self._last_page, "num_pages": self.num_pages,
                "at_head": self._at_head(), "at_tail": self._at_tail(),
                "why": self.why, "current": self.current()}

    # ------------------------------------------------------- 视口/配置 ---
    def set_viewport(self, visible: Any) -> int:
        """GUI 上报"预览栏能放下几个缩略图" -> 窗口目标跟着变（并立刻按需补页）。

        @return 记下来的格数
        """
        self.viewport = self._clamp_viewport(visible)
        self._ensure_window()
        return self.viewport

    # ------------------------------------------------------------ 搜索 ---
    def search(self, keyword: str, *, source: str = "dialogue",
               order: str = "totalrank") -> Dict[str, Any]:
        """**重建**队列：清缓存 -> 抓第 1 页 -> 补满窗口。

        @param source "dialogue"（对话指定的关键词）/ "screen"（画面认出来的游戏）
        @return `{"ok","why","count","target","keyword","source","current"}`
        @note 搜索失败**不抛**（异常写进 `why`），返回 `ok=False`；调用方按 `why` 说话。
        """
        text = str(keyword or "").strip()
        if not text:
            return self._result(ok=False, why="没有关键词，不知道搜什么")
        self._reset(keyword=text, source=str(source or "dialogue"))
        if self._load_page(1) is None:
            return self._result(ok=False, why=self.why or "搜索失败了")
        if not self._all:
            # 第 1 页就是空的 = 这个词真没有结果 —— **不要**再翻第 2 页糊弄过去
            self.why = "「%s」一条都没搜到 —— 换个说法再试" % text
            return self._result(ok=False, why=self.why)
        self._pos = 0
        self._ensure_window()
        self._shortly_note()
        return self._result(ok=True, why=self.why)

    def more(self) -> Dict[str, Any]:
        """再来一批：**强制**往后抓一页（不管窗口够不够长）。"""
        if not self._all:
            return self._result(ok=False, why="队列是空的，先搜一个关键词")
        before = len(self._all)
        page = self._last_page + 1
        if self._load_page(page) is None and len(self._all) == before:
            return self._result(ok=False, why=self.why or "后面没有了")
        if len(self._all) == before:
            return self._result(ok=False, why=self.why or "后面没有了")
        self._shortly_note()
        return self._result(ok=True, why="又补了一批（缓存 %d 条）" % len(self._all))

    # ------------------------------------------------------------ 移动 ---
    def move(self, delta: int) -> Dict[str, Any]:
        """在队列里前后走一格（`+1` 下一集 / `-1` 上一集）。

        @return `{"ok","why","index","count","current"}` —— **能动的先动**;
                动不了（到头了）如实说，不抛异常。
        """
        try:
            step = int(delta)
        except (TypeError, ValueError):
            return self._result(ok=False, why="delta 得是整数")
        if not self._all:
            return self._result(ok=False, why="队列是空的")
        if step == 0:
            return self._result(ok=True, why="没动（step=0）")

        wanted = self._pos + (1 if step > 0 else -1)
        if wanted < 0:
            self._load_page(self._first_page - 1, prepend=True)     # 往前的页抓不到就算了
            wanted = self._pos - 1
        elif wanted >= len(self._all):
            self._load_page(self._last_page + 1)
            wanted = self._pos + 1
        if wanted < 0 or wanted >= len(self._all):
            side = "后面" if step > 0 else "前面"
            message = ("%s没有了（这一批一共 %d 条）—— 换个说法我可以再搜一次"
                       % (side, len(self._all)))
            if self._last_error:
                message += "；刚才取页也失败了：%s" % self._last_error
            self.why = message
            return self._result(ok=False, why=self.why)
        self._pos = wanted
        self._ensure_window()                    # 窗口滑过去了 -> 缺哪边补哪边
        self._shortly_note()
        return self._result(ok=True, why=self.why)

    def next(self) -> Dict[str, Any]:
        """下一集。"""
        return self.move(1)

    def prev(self) -> Dict[str, Any]:
        """上一集。"""
        return self.move(-1)

    def pick(self, index: Any) -> Dict[str, Any]:
        """挑**窗口**里的第 index 条（GUI 点预览图）。越界就夹到窗口范围内。"""
        if not self._all:
            return self._result(ok=False, why="队列是空的")
        try:
            wanted = int(index)
        except (TypeError, ValueError):
            return self._result(ok=False, why="index 得是整数")
        start, end = self._window()
        absolute = max(start, min(start + wanted, end - 1))
        self._pos = absolute
        self._ensure_window()
        self._shortly_note()
        return self._result(ok=True, why=self.why)

    # ------------------------------------------------------------ 清空 ---
    def clear(self) -> Dict[str, Any]:
        """清空队列 —— **关键词与来源一起清掉**（这样画面那条路可以重新接管）。"""
        count = len(self._all)
        self._reset(keyword="", source="")
        self.why = "队列清空了（原来 %d 条）" % count
        return self._result(ok=True, why=self.why)

    # ------------------------------------------------------------ 内部 ---
    @staticmethod
    def _clamp_viewport(value: Any) -> int:
        try:
            number = int(value)
        except (TypeError, ValueError):
            return DEFAULT_VIEWPORT
        return max(1, min(number, MAX_VIEWPORT))

    def _reset(self, *, keyword: str, source: str) -> None:
        self._all: List[Dict[str, Any]] = []      #: 抓过的条目（按页顺序）
        self._page_of: List[int] = []             #: 与 `_all` 平行: 每条来自第几页
        self._pos = 0                             #: 当前条在 `_all` 里的下标（绝对）
        self._first_page = 0                      #: `_all` 覆盖的最早页
        self._last_page = 0                       #: `_all` 覆盖的最晚页
        self.num_pages = 0
        self.keyword = keyword
        self.source = source
        self.why = ""
        self._last_error = ""                     #: 上一次取页失败的原因（"能动的先动"时留着说）

    def _window(self):
        """窗口范围 `[start, end)`：**当前位置前面 N 条、整窗 3N 条**。"""
        size = self.target()
        start = max(0, self._pos - self.viewport)
        end = min(len(self._all), start + size)
        return start, end

    def _at_head(self) -> bool:
        """真正到头了吗（缓存开头 + 没有更早的页）。"""
        return self._pos <= 0 and self._first_page <= 1

    def _at_tail(self) -> bool:
        return self._pos >= len(self._all) - 1 and (
            not self.num_pages or self._last_page >= self.num_pages)

    def _result(self, *, ok: bool, why: str = "") -> Dict[str, Any]:
        return {"ok": bool(ok), "why": why, "index": self.index,
                "count": len(self), "keyword": self.keyword, "source": self.source,
                "current": self.current()}

    def _load_page(self, page: int, prepend: bool = False) -> Optional[int]:
        """抓一页放进缓存。@return 新增条数；抓不了（越界/失败）返回 None。

        @note ⚠ **越界页一律不请求**: 实测 `page > numPages` 不报错、会回重复内容。
        @note 去重只在**缓存**里查一遍（同一视频不会出现两次）; 走回头路是从缓存**取**,
              不是重新去重 —— 所以往回走永远走得回去。
        """
        if page < 1:
            self.why = self.why or "前面没有了（已经是第 1 页）"
            return None
        if self.num_pages and page > self.num_pages:
            self.why = self.why or "后面没有了（一共 %d 页）" % self.num_pages
            return None
        try:
            found = self.api.search(self.keyword, page=page, order="totalrank")
        except Exception as exc:                       # noqa: BLE001 - 网络/风控都在这
            self._last_error = "搜不了（%s）—— 过一会再试" % exc
            self.why = self._last_error
            self.log.warning("bilibili: 队列取第 %d 页失败: %s", page, exc)
            return None
        items = list((found or {}).get("items") or [])
        self.num_pages = int((found or {}).get("num_pages") or 0) or self.num_pages
        known = {str(item.get("bvid") or "") for item in self._all}
        fresh = [item for item in items if str(item.get("bvid") or "") not in known]
        was_empty = not self._all
        if prepend:
            self._all = fresh + self._all
            self._page_of = [page] * len(fresh) + self._page_of
            self._pos += len(fresh)
        else:
            self._all = self._all + fresh
            self._page_of = self._page_of + [page] * len(fresh)
        if was_empty:
            self._first_page = self._last_page = page
        elif prepend:
            self._first_page = page
        else:
            self._last_page = page
        if fresh:
            self.why = ""                  # 抓到了 -> 之前那句"没有了/搜不了"作废
            self._last_error = ""
            self.log.debug("bilibili: %s第 %d 页 +%d 条（缓存 %d 条）",
                           "前插" if prepend else "追加", page, len(fresh), len(self._all))
        else:
            self.why = "第 %d 页没有新内容了" % page
        return len(fresh)

    def _ensure_window(self) -> None:
        """按**当前方向**把窗口补够（最多 `PAGES_PER_TOPUP` 页）。

        · 后面缺（缓存尾部离当前位置不足整窗）-> 往后抓页;
        · 前面缺（当前位置离缓存开头不足 N 条, 而且还有更早的页）-> 往前抓页**前插**。
        """
        size = self.target()
        for _ in range(PAGES_PER_TOPUP):
            if len(self._all) - self._pos >= size:
                break
            if self.num_pages and self._last_page >= self.num_pages:
                break                     # 到最后一页了 —— 由 `_shortly_note` 说"只凑到 N 条"
            if self._load_page(self._last_page + 1) in (None, 0):
                break
        for _ in range(PAGES_PER_TOPUP):
            if self._pos >= self.viewport or self._first_page <= 1:
                break
            if self._load_page(self._first_page - 1, prepend=True) in (None, 0):
                break

    def _shortly_note(self) -> None:
        """如实记一句"为什么窗口没满"（不静默, 也不把旧话留着误导）。"""
        size = self.target()
        if len(self._all) >= size:
            if self.why.startswith("只凑到"):
                self.why = ""
            return
        if not self.why or self.why.startswith("只凑到"):
            self.why = ("只凑到 %d 条（目标 %d）—— 这个关键词总共就这些"
                        % (len(self._all), size))
