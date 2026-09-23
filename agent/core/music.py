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
#  三条"刻意的设计"（都是你定的）
#  ---------------------------------------------------------------------------
#  1. **下一首由 chat 决定** —— 这里**不做自动轮转**: 曲终就停（推一条 playing=false），
#     要不要接着放、放哪首由对话决定。所以本模块只提供"按 tag/次数排序给候选"
#     （`candidates()`）与"在 chat 给过的候选顺序里前后走"（`step()`，给 GUI 的上一首/下一首）。
#  2. **播放次数只在"真的听了 30 秒"之后 +1**（`count_after_s`）—— 避免点开就切刷次数；
#     同一首一次播放会话只计一次（`_session["counted"]`）。
#  3. **进度是真的**：`neteasecli player status` 每次返回 mpv 的真实 position/duration。
#     但每问一次要走一趟 ssh（0.5–1.5 s），所以**推送用的进度 = 上次真值 + 本地外推**
#     （`snapshot()`），每次 `refresh()` 都重新对齐一次 —— 既准又不刷 ssh。
#
#  ⚠ neteasecli 的 status 在"连不上 mpv"时返回**乐观值**（playing=true, duration=0）:
#    所以"到底在不在放"的判据是 **duration > 0**（见 `NeteaseCli.is_playing()`）。
# ============================================================================

from __future__ import annotations

import logging
import time
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence

from ..media import music_library as lib
from ..net.netease_cli import NeteaseCli, NeteaseCliError, PlayerFailure

__all__ = ["MusicPlayer", "MusicError", "DEFAULT_POLL_INTERVAL_S"]

_log = logging.getLogger(__name__)

#: 多久问一次 PC（秒）。3 s 是折中: 走 ssh 要 0.5–1.5 s，太密没意义;
#: 曲终/切歌的检测延迟最多也就是这个数。
DEFAULT_POLL_INTERVAL_S = 3.0

#: 认定"曲终"的余量（秒）: position 到 duration 这么近就算放完了
_END_MARGIN_S = 1.5


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
        #: chat 挑歌时给出的候选顺序（GUI 的上一首/下一首在这个队列里走）
        self._queue: List[str] = []
        self._queue_index = -1

    # ------------------------------------------------------------ 本地库 ---
    def tracks(self) -> List[Dict[str, Any]]:
        """本地库全部记录（读文件；坏行会被跳过并记日志）。"""
        loaded = lib.load(self.library_file)
        for problem in loaded["problems"]:
            self.log.warning("music: 本地库有问题: %s", problem)
        return loaded["tracks"]

    def _save(self, tracks: Sequence[Mapping[str, Any]]) -> None:
        lib.write_tracks(self.library_file, tracks)

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
        """播放/暂停（GUI 那个按钮；**不是"决定放什么"**）。"""
        duration, data = self._live_duration()
        if duration <= 0:
            raise MusicError("PC 上现在没有在放的歌 —— 先说一句「放一首…」")
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
            raise MusicError("PC 上现在没有在放的歌")
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

    # ------------------------------------------------- 队列（上一首/下一首）---
    def step(self, delta: int) -> Dict[str, Any]:
        """在**这次挑选的候选顺序**里前后走一格。

        @raise MusicError 还没有队列（没从对话里挑过歌）
        @note 队列由 `play(queue=…)` 记下 —— 也就是"chat 挑歌时的候选顺序",
              所以"放哪首"仍然是 chat 决定的, 这里只是换个位置。
        """
        if not self._queue:
            raise MusicError("还没有候选队列 —— 先从对话里挑一次歌（例如「放一首听得最少的」），"
                             "之后上一首/下一首才在这个队列里走")
        if self._queue_index < 0:
            self._queue_index = 0
        target = max(0, min(len(self._queue) - 1, self._queue_index + int(delta)))
        if target == self._queue_index:
            raise MusicError("已经在队列的%s了" % ("开头" if delta < 0 else "末尾"))
        self._queue_index = target
        return self.play(self._queue[target], queue=self._queue)

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
            # 没有播放器在放（mpv 起不来 / 已经停了）—— 曲终就停在这里, **不自动续**
            if self._session is not None:
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
            self.log.info("music: 这首放完了（%.0f/%.0f s）—— 按约定**停下**等对话决定下一首",
                          position, duration)
            self._session = None
            self._truth["playing"] = False
        return self.snapshot(now)

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
