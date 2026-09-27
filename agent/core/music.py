# ============================================================================
#  agent/core/music.py — 播放内核（Phase 7 T8-4）
#
#  它做什么
#  ---------------------------------------------------------------------------
#  把三样东西接起来，回答"现在在放什么、放了多久、下一首放什么":
#
#       agent/net/netease_cli.py      在 PC 上放（声音从 PC 出）
#       agent/media/music_library.py  本地库（id + tags + 播放次数）
#       Runtime 的 music 推送         → GUI 音乐条
#
#  三条"刻意的设计"（都是你定的；第 1 条在 T8-5b 改过一次）
#  ---------------------------------------------------------------------------
#  1. **队列内容由 chat 决定 / 播放控制由 GUI 决定**（T8-5b）——
#     把"谁决定什么"拆成两条互不打架的线:
#       · chat 只有两个决定: 把一首**加进**队列（`enqueue()`）或**清空**队列（`clear_queue()`）;
#       · 播放 / 暂停 / 上一首 / 下一首是 **GUI 四个按钮**的事（`toggle()` / `step()`）。
#     · 队列是**环形队列**: `step(+1)` 到尾回到第一首, `step(-1)` 到首回到最后一首
#       （T8-4 时是 `max/min` 夹住、到头报错 —— 那就不叫队列了）;
#     · **曲终自动下一首**（环形继续）; 队列空（或没安排过）才停下;
#     · "听哪首"仍然是 chat 决定的 —— 它决定队列**内容**, GUI 只是换位置。
#  2. **播放次数只在"真的听了 30 秒"之后 +1**（`count_after_s`）—— 避免点开就切刷次数；
#     同一首一次播放会话只计一次（`_session["counted"]`）。
#  3. **进度是真的**：`neteasecli player status` 每次返回 mpv 的真实 position/duration。
#     但每问一次要走一趟 ssh（0.5–1.5 s），所以**推送用的进度 = 上次真值 + 本地外推**
#     （`snapshot()`），每次 `refresh()` 都重新对齐一次 —— 既准又不刷 ssh。
#     ⚠ 队列**不进推送**（T8-5b 你定的: 队列只在内部用, GUI 不显示）——`queue_state()`
#      是给工具/日志看的。
#
#  ⚠ neteasecli 的 status 在"连不上 mpv"时返回**乐观值**（playing=true, duration=0）:
#    所以"到底在不在放"的判据是 **duration > 0**（见 `NeteaseCli.is_playing()`）。
# ============================================================================

from __future__ import annotations

import logging
import time
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from ..media import music_library as lib
from ..net.netease_cli import NeteaseCli, NeteaseCliError, PlayerFailure

__all__ = ["MusicPlayer", "MusicError", "DEFAULT_POLL_INTERVAL_S",
           "tracks_from_search"]


def tracks_from_search(payload: Any, artist: Optional[str] = None) -> List[Dict[str, Any]]:
    """把 `neteasecli search track <关键词>` 的结果整理成 [{id, name, artists, album, duration}, …]。

    @param payload 信封里的 `data`（`{"tracks": [...]}`）或整个信封（都收）
    @param artist  只留 **artists 命中这个名字** 的那些（T10-4 的"只按歌手补"就靠它）
    @note 搜出来的结果会被原样当成元数据入队（`_meta_from_detail` 认这个形状）——
          **不再逐首 track_detail**（一次补 28 首那样要走几十次 ssh）。
    """
    data = payload
    if isinstance(payload, Mapping) and "tracks" not in payload:
        data = payload.get("data")
    rows = data.get("tracks") if isinstance(data, Mapping) else None
    out: List[Dict[str, Any]] = []
    wanted = str(artist or "").strip().lower()
    for row in rows or ():
        if not isinstance(row, Mapping) or not row.get("id"):
            continue
        item = dict(row)
        if wanted:
            names = []
            for entry in (row.get("artists") or row.get("ar") or []):
                if isinstance(entry, Mapping):
                    names.append(str(entry.get("name") or "").lower())
                else:
                    names.append(str(entry).lower())
            if wanted not in names:
                continue
        out.append(item)
    return out


#: 旧名（模块内用着顺手的短名）
_tracks_from_search = tracks_from_search

_log = logging.getLogger(__name__)

#: 多久问一次 PC（秒）。3 s 是折中: 走 ssh 要 0.5–1.5 s，太密没意义;
#: 曲终/切歌的检测延迟最多也就是这个数。
DEFAULT_POLL_INTERVAL_S = 3.0

#: 认定"曲终"的余量（秒）: position 到 duration 这么近就算放完了
_END_MARGIN_S = 1.5

#: 曲终自动接下一首之前, 这一会话至少要放过这么久（秒）—— 见 `_played_long_enough`
_MIN_ADVANCE_S = 5.0


class MusicError(ValueError):
    """音乐动作失败（没在放 / 库里没有 / PC 那边出问题）。

    继承 ValueError：与仓库里其它"这次这个动作不行"的错误一致（`NeteaseCliError` 才是
    "通道出了问题"）。本类的消息一律是**给人看的**。
    """


class MusicPlayer:
    """放歌 + 计次 + 给 chat 挑候选（**不自己决定放哪首**）。"""

    def __init__(self, cli: NeteaseCli, library_file: str,
                 count_after_s: int = 30,
                 poll_interval_s: float = DEFAULT_POLL_INTERVAL_S,
                 log: Optional[logging.Logger] = None,
                 clock: Optional[Callable[[], float]] = None) -> None:
        """
        @param clock 可选: "现在几点"的替身（默认 `time.monotonic`）—— 只给测试用,
                      让"进度外推 / 计次阈值"能被确定性地验
        """
        self.cli = cli
        self.library_file = library_file
        self.count_after_s = max(1, int(count_after_s))
        self.poll_interval_s = float(poll_interval_s)
        self.log = log or _log
        self._clock = clock or time.monotonic
        #: 当前这首的 id / 元数据（库里那一行）
        self.current_id: Optional[str] = None
        self.current: Dict[str, Any] = {}
        #: 一次"播放会话"的状态: 起点 + 是否已经计过次
        self._session: Optional[Dict[str, Any]] = None
        #: 最近一次从 PC 问到的真值（推送用的进度靠它 + 本地外推）
        self._truth: Dict[str, Any] = {"position": 0.0, "duration": 0.0, "playing": False}
        self._truth_at = 0.0
        #: **环形**播放队列（内容由 chat 决定: `enqueue()` / `clear_queue()`）。
        #: GUI 的上一首/下一首在这个队列里**环绕**走（`step()`）。
        self._queue: List[str] = []
        self._queue_index = -1
        #: 曲终自动接下一首时的重入保护（`refresh()` 里会调 `play()`）
        self._advancing = False
        #: 自动补歌（T10-4）: 目标长度（0 = 不补）+ 每个歌手搜失败的时间（退避用）
        self._autofill_target = 0
        self._search_failed_at: Dict[str, float] = {}

    # ------------------------------------------------------------ 本地库 ---
    def tracks(self) -> List[Dict[str, Any]]:
        """本地库全部记录（读文件；坏行会被跳过并记日志）。"""
        loaded = lib.load(self.library_file)
        for problem in loaded["problems"]:
            self.log.warning("music: 本地库有问题: %s", problem)
        return loaded["tracks"]

    def _save(self, tracks: Sequence[Mapping[str, Any]]) -> None:
        lib.write_tracks(self.library_file, tracks)

    def find_track(self, track_id: Any) -> Optional[Dict[str, Any]]:
        """库里有没有这首（只看本地库, **不碰 PC**）。"""
        wanted = str(track_id)
        for item in self.tracks():
            if str(item.get("id")) == wanted:
                return item
        return None

    def _meta_from_detail(self, detail: Mapping[str, Any]) -> Dict[str, Any]:
        """把 neteasecli 的 `track detail` 翻成 `ensure_track` 认的元数据。"""
        info: Dict[str, Any] = {"name": detail.get("name") or ""}
        artists = detail.get("artists") or detail.get("ar") or []
        if isinstance(artists, list):
            info["artists"] = ", ".join(
                str(a.get("name")) for a in artists if isinstance(a, Mapping))
        album = detail.get("album") or detail.get("al") or {}
        if isinstance(album, Mapping):
            info["album"] = str(album.get("name") or "")
            info["year"] = album.get("publishTime")
        info["duration_ms"] = int(detail.get("duration") or 0)
        return info

    def ensure_track(self, track_id: Any, meta: Optional[Mapping[str, Any]] = None,
                     genre: Iterable[str] = ()) -> Dict[str, Any]:
        """确保这首在库里（不在就按**元数据**自动打标加进去）。

        @param meta 已知元数据（name/artists/album/duration_ms）；缺的会去问 PC
        @return 库里那一行
        @note 已存在就**原样返回**（不清 plays、不覆盖 chat 补过的 tag）
        """
        track_id = str(track_id)
        tracks = self.tracks()
        for item in tracks:
            if str(item.get("id")) == track_id:
                return item

        info = dict(meta or {})
        if not info.get("name"):
            try:
                detail = self.cli.track_detail(track_id)
            except NeteaseCliError as exc:
                self.log.warning("music: 拿不到 %s 的详情 (%s) —— 先只存 id", track_id, exc)
                detail = {}
            info.setdefault("name", detail.get("name") or "")
            artists = detail.get("artists") or detail.get("ar") or []
            if isinstance(artists, list):
                info.setdefault("artists", ", ".join(
                    str(a.get("name")) for a in artists if isinstance(a, Mapping)))
            album = detail.get("album") or detail.get("al") or {}
            if isinstance(album, Mapping):
                info.setdefault("album", str(album.get("name") or ""))
                info.setdefault("year", album.get("publishTime"))
            info.setdefault("duration_ms", int(detail.get("duration") or 0))

        year = info.get("year")
        if isinstance(year, (int, float)) and year > 10 ** 11:      # 毫秒时间戳
            year = time.gmtime(year / 1000.0).tm_year
        elif not isinstance(year, int):
            year = None

        record = lib.make_record(
            track_id, info.get("name") or track_id, info.get("artists") or "",
            info.get("album") or "", info.get("duration_ms") or 0,
            tags=lib.auto_tags(info.get("name") or "", info.get("artists") or "",
                               info.get("album") or "", genre=genre, year=year),
            source=info.get("source") or "play")
        tracks.append(record)
        self._save(tracks)
        self.log.info("music: 新歌入库 %s %s", track_id, record["name"])
        return record

    def tag_track(self, tags: Mapping[str, Any], track_id: Optional[str] = None,
                  remove: bool = False) -> Dict[str, Any]:
        """chat 补充（或删掉）tag。不给 id 就作用于**当前这首**。"""
        track_id = str(track_id or self.current_id or "")
        if not track_id:
            raise MusicError("现在没有在放的歌 —— 要打标签请给 track_id")
        tracks = self.tracks()
        for index, item in enumerate(tracks):
            if str(item.get("id")) != track_id:
                continue
            updated = lib.remove_tags(item, tags) if remove else lib.add_tags(item, tags)
            tracks[index] = updated
            self._save(tracks)
            if track_id == self.current_id:
                self.current = updated
            return updated
        raise MusicError("本地库里没有 %s 这首歌（先播一次它, 或者先导入歌单）" % track_id)

    def candidates(self, tag: Optional[str] = None, axis: Optional[str] = None,
                   sort: str = "plays_asc", limit: int = 10) -> List[Dict[str, Any]]:
        """给 chat 挑歌用的候选清单（按 tag + 播放次数）。"""
        return lib.pick(self.tracks(), tag=tag, axis=axis, sort=sort, limit=limit)

    def summary(self) -> Dict[str, Any]:
        """库的摘要（工具回话用）。"""
        return lib.summarise(self.tracks())

    # ------------------------------------------------------------ 播放 ---
    def _live_duration(self, retry_s: float = 1.2) -> Tuple[float, Dict[str, Any]]:
        """问一次 PC: (此刻的 duration, 状态)。**刚 play 完可能瞬时读到 0, 会重试一次。**

        ⚠ 为什么需要重试（板端实测）: `track play` 走交互会话（schtasks /it）起 mpv,
          紧接着从 ssh 问 `player status`, 偶尔会读到 `duration=0`（neteasecli 自己的
          播放器状态还没写好）—— 于是"暂停/继续"会误报"PC 上现在没有在放的歌"。
          等 1.2 s 再问一次就好; 两次都是 0 才认定"真的没有播放器"。
        """
        data = self.cli.status()
        duration = float(data.get("duration") or 0)
        if duration <= 0 and retry_s > 0:
            time.sleep(retry_s)
            data = self.cli.status()
        return float(data.get("duration") or 0), data

    def play(self, track_id: Any, queue: Optional[Sequence[str]] = None,
             meta: Optional[Mapping[str, Any]] = None,
             genre: Iterable[str] = (), quality: Optional[str] = None) -> Dict[str, Any]:
        """放一首（并在库里登记 + 记住这次挑选的候选顺序）。

        @param queue 这次挑选的候选 id 顺序（GUI 的上一首/下一首就在它里面走）
        @raise MusicError PC 那边没放起来（原因由 `NeteaseCli` 给出, 原样带上）
        """
        track_id = str(track_id)
        record = self.ensure_track(track_id, meta=meta, genre=genre)
        try:
            self.cli.play(track_id, quality=quality)
        except NeteaseCliError as exc:              # 含 PlayerFailure
            raise MusicError(str(exc))

        self.current_id = track_id
        self.current = record
        now = self._clock()
        self._session = {"started_at": now, "counted": False}
        self._truth = {"position": 0.0,
                       "duration": float(record.get("duration_ms") or 0) / 1000.0,
                       "playing": True}
        self._truth_at = now
        if queue:
            self._queue = [str(x) for x in queue]
            self._queue_index = self._queue.index(track_id) if track_id in self._queue else -1
        self.log.info("music: 开始播放 %s %s", track_id, record.get("name"))
        return record

    def toggle(self) -> Dict[str, Any]:
        """播放/暂停（GUI 那个按钮；**不是"决定放什么"**）。

        @note PC 上没在放时**从队列当前位置起播**（T8-5b）—— 否则"chat 安排了队列、
              GUI 按播放却没反应"。队列也是空的就如实报错, 并说清该先做什么。
        """
        duration, data = self._live_duration()
        if duration <= 0:
            record = self.start_from_queue()
            return {"paused": False, "started": True,
                    "track_id": self.current_id, "name": record.get("name"),
                    "message": "队列里起播"}
        paused = bool(data.get("paused"))
        result = self.cli.pause()
        self._truth["playing"] = paused            # 原本暂停 -> 现在在放, 反之亦然
        self._truth_at = self._clock()
        return {"paused": not paused, "message": str(result.get("message") or "")}

    def pause(self) -> Dict[str, Any]:
        duration, data = self._live_duration()
        if duration <= 0:
            raise MusicError("PC 上现在没有在放的歌")
        if not data.get("paused"):
            self.cli.pause()
        self._truth["playing"] = False
        self._truth_at = self._clock()
        return {"paused": True}

    def resume(self) -> Dict[str, Any]:
        duration, data = self._live_duration()
        if duration <= 0:
            # 没有播放器: 与"播放"按钮同款 —— 从队列当前位置起播（T8-5b）
            record = self.start_from_queue()
            return {"paused": False, "started": True, "track_id": self.current_id,
                    "name": record.get("name")}
        if data.get("paused"):
            self.cli.pause()
        self.refresh()                              # 重新对齐真值
        return {"paused": False}

    def stop(self) -> Dict[str, Any]:
        """停止（曲终/用户喊停）—— 清掉会话状态, 但**不改库**。"""
        try:
            self.cli.stop()
        except NeteaseCliError as exc:
            self.log.warning("music: stop 失败 (已忽略): %s", exc)
        self._session = None
        self._truth = {"position": float(self._truth.get("position") or 0), "duration": 0.0,
                       "playing": False}
        self._truth_at = self._clock()
        return {"stopped": True, "track": self.current_id}

    def seek(self, seconds: float, absolute: bool = False) -> Dict[str, Any]:
        result = self.cli.seek(seconds, absolute=absolute)
        self.refresh()
        return {"message": str(result.get("message") or "")}

    def set_volume(self, level: int) -> Dict[str, Any]:
        result = self.cli.set_volume(level)
        return {"message": str(result.get("message") or "")}

    # ------------------------------------------------- 环形队列（T8-5b）---
    #  ⚠ 分工: **内容**由 chat 决定（enqueue / clear_queue）, **走位**由 GUI 决定
    #    （step ±1）; 队列只在内部用, 不推给 GUI（你 T8-5b 定的）。
    def queue_state(self) -> Dict[str, Any]:
        """队列现状（给工具回话 / 日志；**不推送**）。"""
        current = (self._queue[self._queue_index]
                   if 0 <= self._queue_index < len(self._queue) else None)
        return {"size": len(self._queue), "index": self._queue_index,
                "current": current, "ids": list(self._queue)}

    def enqueue(self, track_id: Any, meta: Optional[Mapping[str, Any]] = None,
                genre: Iterable[str] = (), replace: bool = False,
                verify: bool = True) -> Dict[str, Any]:
        """把一首**加进环形队列**（chat 的两个决定之一）。

        @param replace True = 先清空队列, 这首成为唯一一首（"换一批"用一句话说完）
        @param verify  True 时: 库里没有这首就**去 PC 问一次** —— 问不到就报错。
                       ⚠ 板端实测（T8-5b）0.6B 会**编造 id**（把 `track1` 这种写进来）,
                         不校验的话它会安静地排进队列, 直到"放不出来"才暴露。
        @return {"track_id","name","added","replaced","queue"}
        @raise MusicError verify 失败（PC 上没有这个 id）
        @note 只在库里登记（`ensure_track`）—— **不在这里起播**: 响不响由调用方决定
              （Runtime 在"什么都没在放"时会起播一次）。
        """
        track_id = str(track_id)
        record = self.find_track(track_id)
        if record is None and verify:
            try:
                detail = self.cli.track_detail(track_id)
            except NeteaseCliError as exc:
                raise MusicError("PC 上没有 id=%s 这首歌（%s）—— 先用 search 拿到真 id"
                                 % (track_id, exc))
            if not detail:
                raise MusicError("PC 上没有 id=%s 这首歌 —— 先用 search 拿到真 id" % track_id)
            meta = dict(self._meta_from_detail(detail), **(meta or {}))
        record = record or self.ensure_track(track_id, meta=meta, genre=genre)

        if replace:
            self._queue = [track_id]
            self._queue_index = 0
            added, replaced = True, True
        else:
            replaced = False
            if track_id in self._queue:
                added = False
                if self._queue_index < 0:
                    self._queue_index = self._queue.index(track_id)
            else:
                self._queue.append(track_id)
                added = True
                if self._queue_index < 0:          # 第一首进队 -> 光标落在它身上
                    self._queue_index = len(self._queue) - 1
        self.log.info("music: 进队列 %s %s (新增=%s 替换=%s, 队 %d 首)",
                      track_id, record.get("name"), added, replaced, len(self._queue))
        return {"track_id": track_id, "name": record.get("name"), "added": added,
                "replaced": replaced, "queue": self.queue_state()}

    def clear_queue(self) -> Dict[str, Any]:
        """清空队列（chat 的另一个决定）。

        @return {"cleared", "queue"}
        @note **不停播放** —— 停/放是 GUI 按钮的事; 当前这首放完后队列空, 自然停下。
        """
        size = len(self._queue)
        self._queue = []
        self._queue_index = -1
        if size:
            self.log.info("music: 队列被清空（原来 %d 首）", size)
        return {"cleared": size, "queue": self.queue_state()}

    # ------------------------------------------------- 自动补歌（T10-4）---
    def set_target(self, target: Any) -> int:
        """队列**目标长度**（0 = 不自动补歌）。@return 记下来的那个数。"""
        try:
            self._autofill_target = max(0, int(target or 0))
        except (TypeError, ValueError):
            self._autofill_target = 0
        return self._autofill_target

    def target(self) -> int:
        """当前目标长度（0 = 不补）。"""
        return int(getattr(self, "_autofill_target", 0) or 0)

    def queue_ids(self) -> List[str]:
        """队列里的 id（老 -> 新）。"""
        return [str(item) for item in self._queue]

    def remove(self, track_ids: Sequence[Any]) -> Dict[str, Any]:
        """把这些歌**从播放队列里去掉**（T10 第 5 条；不动本地库、不动播放状态）。

        @return {"removed", "current_removed", "queue"}
        @note 正在放的那首被去掉时**光标退一格**: 调用方接着 `step(1)` 就是"跳到下一首"
              （环形, 到尾部会绕回开头）。这样"不想听正在放的这首"不会把它继续放下去。
        """
        wanted = {str(item) for item in (track_ids or ()) if str(item)}
        if not wanted or not self._queue:
            return {"removed": [], "current_removed": False, "queue": self.queue_state()}
        current_was = self.current_id
        index_before = self._queue_index
        removed = [tid for tid in self._queue if tid in wanted]
        if not removed:
            return {"removed": [], "current_removed": False, "queue": self.queue_state()}
        self._queue = [tid for tid in self._queue if tid not in wanted]
        current_removed = bool(current_was) and str(current_was) in wanted
        if not self._queue:
            self._queue_index = -1
        elif current_removed:
            # 退一格 -> 下一次 step(1) 正好落在"被去掉那首原来后面那首"（环形）
            self._queue_index = max(0, index_before) - 1
        else:
            self._queue_index = max(0, min(index_before, len(self._queue) - 1))
        self.log.info("music: 从队列里去掉 %s（少了 %d 首, 正在放的那首%s被去掉）",
                      "、".join(removed[:3]) + ("…" if len(removed) > 3 else ""),
                      len(removed), "" if current_removed else "没")
        return {"removed": removed, "current_removed": current_removed,
                "queue": self.queue_state()}

    def refill(self, profile: Optional[Mapping[str, Any]] = None, *,
               muted_artists: Sequence[str] = (), muted_tracks: Sequence[str] = (),
               by_artist: bool = True, search_per_cycle: int = 3,
               search_limit: int = 10, search_backoff_s: float = 300.0,
               now: Optional[float] = None) -> Dict[str, Any]:
        """把队列补到**目标长度**（T10-4）: 先吃本地库, 不够再**按歌手**去 PC 搜。

        @param profile 用户画像（`rank_tracks` 用它排本地库; 没有就按"听得最少的"）
        @param by_artist 允许"按歌手去 PC 搜"这一段（唯一会出去找的路, 你定的）
        @param search_per_cycle 每个 tick 最多搜几个歌手; `search_limit` 每个歌手最多收几首
        @param search_backoff_s 同一个歌手搜失败后多久不再试
        @return {"filled", "from_library", "from_search", "searched", "short_by",
                 "skipped", "why"} —— 日志与测试都看它
        @note **只从本地库 + 按歌手搜**; 搜到的歌用搜索结果里的元数据直接入队
              （`verify=False`）—— 否则一次补 28 首要几十次 `track_detail`。
        """
        from . import user_profile

        moment = float(now if now is not None else time.time())
        target = self.target()
        filled: List[str] = []
        notes: List[str] = []
        if target <= 0:
            return {"filled": [], "from_library": 0, "from_search": 0, "searched": [],
                    "short_by": 0, "skipped": {}, "why": ["没开自动补歌（target=0）"]}

        muted_track_ids = {str(item) for item in (muted_tracks or ())}
        muted_artist_names = {str(item) for item in (muted_artists or ())}

        # ---- ① 本地库 ----
        library = self.tracks()
        while len(self._queue) < target:
            ranked = user_profile.rank_tracks(
                profile, library, exclude=self._queue,
                muted_artists=sorted(muted_artist_names),
                muted_tracks=sorted(muted_track_ids))
            if not ranked:
                break
            top = ranked[0]
            try:
                self.enqueue(top["id"], meta=top, verify=False)
            except MusicError as exc:
                notes.append("本地库那首 %s 排不进去: %s" % (top["id"], exc))
                break
            filled.append(str(top["id"]))
        from_library = len(filled)

        # ---- ② 本地库不够 -> **按歌手**去 PC 搜（唯一会出去找的路）----
        searched: List[str] = []
        skipped: Dict[str, str] = {}
        from_search = 0
        artists = [str(row.get("name")) for row in
                   (((profile or {}).get("music") or {}).get("artist") or [])
                   if float(row.get("weight") or 0) > 0]
        if len(self._queue) < target and not artists:
            notes.append("画像里还没有歌手偏好 —— 只吃本地库（等画像攒够 2000 字）")
        if len(self._queue) < target and by_artist and artists:
            attempts = 0
            for artist in artists:
                # ⚠ 上限按**尝试次数**算: 搜失败的不进 `searched`, 只数成功的会一个 tick
                #   把整张歌手表都试一遍（板端实测到过）。
                if len(self._queue) >= target or attempts >= max(1, int(search_per_cycle)):
                    break
                if artist in muted_artist_names:
                    skipped[artist] = "不想听这个歌手"
                    continue
                failed_at = float(self._search_failed_at.get(artist) or 0.0)
                if failed_at and (moment - failed_at) < float(search_backoff_s):
                    skipped[artist] = "刚搜失败过, 退避中"
                    continue
                attempts += 1
                try:
                    data = self.cli.search("track", artist, limit=max(1, int(search_limit)))
                except NeteaseCliError as exc:
                    self._search_failed_at[artist] = moment
                    skipped[artist] = "搜不了: %s" % exc
                    self.log.warning("music: 按歌手搜 %s 失败（%s 秒后再试）: %s",
                                     artist, int(search_backoff_s), exc)
                    continue
                searched.append(artist)
                for track in _tracks_from_search(data, artist):
                    if len(self._queue) >= target:
                        break
                    track_id = str(track["id"])
                    if track_id in self._queue or track_id in muted_track_ids:
                        continue
                    try:
                        self.enqueue(track_id, meta=self._meta_from_detail(track),
                                     verify=False)
                    except MusicError as exc:
                        notes.append("搜到的 %s 排不进去: %s" % (track_id, exc))
                        continue
                    filled.append(track_id)
                    from_search += 1
                self._search_failed_at.pop(artist, None)

        short_by = max(0, target - len(self._queue))
        if filled:
            self.log.info("music: 自动补歌 +%d 首（本地库 %d / 按歌手搜 %d；搜了 %s）-> 队 %d/%d",
                          len(filled), from_library, from_search,
                          "、".join(searched) or "-", len(self._queue), target)
        if short_by and not filled:
            notes.append("补不满: 队里 %d 首, 差 %d（本地库 %d 首, 搜了 %s）"
                         % (len(self._queue), short_by, len(library),
                            "、".join(searched) or "-"))
        return {"filled": filled, "from_library": from_library, "from_search": from_search,
                "searched": searched, "short_by": short_by, "skipped": skipped,
                "why": notes}

    def start_from_queue(self, index: Optional[int] = None) -> Dict[str, Any]:
        """从队列**当前**（或指定）位置起播 —— GUI 的"播放"按钮在 PC 上没在放时用它。

        @raise MusicError 队列是空的（chat 还没安排过歌）
        """
        if not self._queue:
            raise MusicError("队列是空的 —— 先让对话安排一首（例如「放一首听得最少的」），"
                             "再按播放")
        if index is not None:
            self._queue_index = int(index) % len(self._queue)
        elif self._queue_index < 0:
            self._queue_index = 0
        return self.play(self._queue[self._queue_index])

    def step(self, delta: int) -> Dict[str, Any]:
        """在**环形队列**里前后走一格（GUI 的上一首 / 下一首）。

        @raise MusicError 队列是空的（chat 还没安排过歌）
        @note 环形: 到尾回到第一首、到首回到最后一首; 队列只有一首时 = **重放这一首**。
        @note 这里**不改队列内容** —— 只是换个位置（T8-4 时的"到头就报错"已按 T8-5b 改掉）。
        """
        if not self._queue:
            raise MusicError("队列是空的 —— 先让对话安排一首（例如「放一首听得最少的」），"
                             "之后上一首/下一首才在这个队列里走")
        if self._queue_index < 0:
            self._queue_index = 0
        self._queue_index = (self._queue_index + int(delta)) % len(self._queue)
        return self.play(self._queue[self._queue_index])

    def _advance(self) -> bool:
        """曲终: 环形队列往后走一首并接着放。@return 真的换了才 True。

        ⚠ 由 `refresh()` 调（跑在轮询的线程里）—— `play()` 里要走 ssh, 所以这里
          **不放进事件循环**; 重入用 `_advancing` 挡住。
        """
        if self._advancing or not self._queue:
            return False
        self._advancing = True
        try:
            if self.current_id in self._queue:
                # 正常情形: 当前这首在队列里 -> 往后走一格（环形）
                self._queue_index = (self._queue.index(self.current_id) + 1) % len(self._queue)
            else:
                # 队列被换过（chat 清空/替换）而当前这首不在里面 -> 从光标处开始
                self._queue_index = max(0, self._queue_index) % len(self._queue)
            self.play(self._queue[self._queue_index])
            self.log.info("music: 曲终 -> 环形队列下一首 %s（第 %d/%d 首）",
                          self._queue[self._queue_index],
                          self._queue_index + 1, len(self._queue))
            return True
        except MusicError as exc:
            self.log.warning("music: 曲终想接下一首但失败 (%s) —— 停下", exc)
            return False
        finally:
            self._advancing = False

    # ------------------------------------------------------------ 状态 ---
    def refresh(self) -> Dict[str, Any]:
        """问一次 PC 的真值 + 处理计次/曲终。@return 快照（同 `snapshot()`）。"""
        now = self._clock()
        try:
            data = self.cli.status()
        except NeteaseCliError as exc:
            self.log.warning("music: 问状态失败: %s", exc)
            self._truth["playing"] = False
            self._truth_at = now
            return self.snapshot(now)

        duration = float(data.get("duration") or 0)
        position = float(data.get("position") or 0)
        playing = duration > 0 and not bool(data.get("paused"))
        self._truth = {"position": position, "duration": duration, "playing": playing}
        self._truth_at = now

        if duration <= 0:
            # 没有播放器在放。两种可能: ① 这首**放完了**（板端实测: neteasecli 在曲终时
            # 连 mpv 一起收掉, 于是这里读到 duration=0）② 用户按了 GUI 的停止（那时
            # `_session` 已经被 `stop()` 清掉）。判据就是 `_session` 还在不在。
            if self._session is not None:
                if self._played_long_enough(now) and self._advance():
                    return self.snapshot(self._clock())
                self.log.info("music: 播放结束（PC 上已经没有 mpv 在放）")
            self._session = None
            return self.snapshot(now)

        if self._session is None:
            # 可能是"上一次刷新之后才开始的"（比如在 PC 上手动放的）—— 现记一次会话
            self._session = {"started_at": now, "counted": position >= self.count_after_s}

        if not self._session["counted"] and position >= self.count_after_s:
            self._session["counted"] = True
            self._count_play()

        if playing and position >= max(0.0, duration - _END_MARGIN_S):
            # 曲终: 环形队列里还有下一首就接着放（T8-5b 你定的）, 否则停下
            if self._advance():
                return self.snapshot(self._clock())
            self.log.info("music: 这首放完了（%.0f/%.0f s）—— 队列里没有下一首, 停下",
                          position, duration)
            self._session = None
            self._truth["playing"] = False
        return self.snapshot(now)

    def _played_long_enough(self, now: float) -> bool:
        """这一会话真的放过一会儿吗?（挡住"PC 上根本没放起来"时的连环自动切歌）

        @note 阈值 `_MIN_ADVANCE_S`: 每个 action 都要走 ssh + schtasks, 一条命令往返
              0.5~3 s; 立刻又读到 duration=0 就说明**播放没起来**, 这时不该顺着队列
              一路切下去（板端实测 mpv 起不来时会这样）。
        """
        if self._session is None:
            return False
        started = float(self._session.get("started_at") or now)
        return (now - started) >= _MIN_ADVANCE_S

    def _count_play(self) -> None:
        """真的听了 30 s —— 给当前这首 +1（同一会话只加一次）。"""
        track_id = self.current_id
        if not track_id:
            return
        try:
            tracks = self.tracks()
            for index, item in enumerate(tracks):
                if str(item.get("id")) != track_id:
                    continue
                tracks[index] = lib.bump_play(item)
                self.current = tracks[index]
                self._save(tracks)
                self.log.info("music: 记一次播放 %s %s（共 %d 次）", track_id,
                              self.current.get("name"), self.current["plays"])
                return
            self.log.warning("music: 想计次但库里没有 %s", track_id)
        except lib.MusicLibraryError as exc:
            self.log.warning("music: 计次写盘失败 (已忽略): %s", exc)

    def snapshot(self, now: Optional[float] = None) -> Dict[str, Any]:
        """给推送用的状态（**不碰 PC**: 上次真值 + 本地外推）。

        @return {"track_id","title","artist","album","position_s","duration_s",
                 "playing","plays","tags"}
        @note `position_s` 是"锚定在 mpv 真值上的估算": 每次 refresh 都重新对齐,
              中间靠本地时钟走（暂停就停住、不超过时长）。
        """
        now = self._clock() if now is None else now
        position = float(self._truth.get("position") or 0.0)
        duration = float(self._truth.get("duration") or 0.0)
        playing = bool(self._truth.get("playing"))
        if playing and self._session is not None:
            position += max(0.0, now - float(self._truth_at))
        if duration > 0:
            position = min(position, duration)
        record = self.current or {}
        if duration <= 0 and record.get("duration_ms"):
            duration = float(record["duration_ms"]) / 1000.0
        return {
            "track_id": self.current_id,
            "title": record.get("name") or "",
            "artist": record.get("artists") or "",
            "album": record.get("album") or "",
            "position_s": round(position, 1),
            "duration_s": round(duration, 1),
            "playing": playing,
            "plays": int(record.get("plays") or 0),
            "tags": record.get("tags") or {},
        }

    def __repr__(self) -> str:
        return "<MusicPlayer current=%s playing=%s queue=%d>" % (
            self.current_id or "-", self.snapshot()["playing"], len(self._queue))
