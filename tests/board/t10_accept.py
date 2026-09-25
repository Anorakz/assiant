#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""T10 板端验收: **三格窗口 + 画像消费**（Phase 7 T10-6）。

跑法（板端, 仓库根）::

    python3 tests/board/t10_accept.py            # 不调模型（心情 = unknown, 只验机制）
    python3 tests/board/t10_accept.py --llm      # 起本机 llama-server, 真的问一次心情

验的是这四件事（都用**真代码 + 真数据 + 真壁纸目录**）:

    A. 三格窗口: next / prev / stage / pick 各自怎么动 prev/current/next;
       stage **不切屏、不计数**; 画像挑不动时**退回文件名顺序**。
    B. 队列动作: 负反馈点名的歌（`cleared.track_ids`）从队列里没了 + 立刻补;
       **只有两个已知心情不同**才重置队列, 重置时留住正在放的那首。
    C. 端到端: 真 `_profile_tick()`（攒够字 -> 后台构建 -> 落盘 -> 重挑 next + 队列动作）。
    D. **只动副本**: 真 `config/*.jsonl` 从头到尾一个字节都不改（脚本自己核对 md5）。

⚠ 这是**验收脚本**（要真数据、真目录、真模型）, 不进 `scripts/test-python.*` 的常规清单。
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import io
import json
import logging
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from agent.config import load_config                       # noqa: E402
from agent.core import user_profile                        # noqa: E402
from agent.core.chat_memory import ChatMemory              # noqa: E402
from agent.main import Runtime                             # noqa: E402

if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
REAL_FILES = ("config/wall_data.jsonl", "config/music_library.jsonl", "config/user_profile.jsonl")

_FAILED = []


def check(name, ok, detail=""):
    print("  %s %s%s" % ("✔" if ok else "✘", name, ("  [%s]" % detail) if detail else ""))
    if not ok:
        _FAILED.append(name)


def md5(path):
    if not os.path.exists(path):
        return "-"
    digest = hashlib.md5()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def usage_map(path):
    out = {}
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            item = json.loads(line)
            if item.get("kind") == "vocab":
                continue
            out[os.path.basename(str(item.get("path")))] = int(item.get("used") or 0)
    return out


def short(path):
    return os.path.basename(path) if path else None


def show_window(rt, title):
    window = rt._window_names()
    print("  %-14s prev=%-28s current=%-28s next=%s"
          % (title, window["prev"], window["current"], window["next"]))


def shows(rt):
    """队列里每首的『歌手 - 歌名』（给人看的）。`tags.artist` 是**列表**。"""
    by_id = {str(t["id"]): t for t in rt.music.tracks()}
    out = []
    for track_id in rt.music.queue_ids():
        track = by_id.get(track_id) or {}
        tags = track.get("tags") or {}
        out.append("%s-%s" % ("、".join(artists_of(track)) or "?",
                              str(track.get("name"))[:12]))
    return out


def artists_of(track):
    """一首歌的歌手名（`tags.artist` 可能是 str 也可能是 list）。"""
    value = (track.get("tags") or {}).get("artist")
    if isinstance(value, str):
        return [value] if value else []
    return [str(item) for item in (value or ()) if item]


def pick_library_artist(rt, profile_artists):
    """挑一个**画像里也有、库里也真有**的歌手（负反馈要拿它当靶子）。"""
    library = {name for track in rt.music.tracks() for name in artists_of(track)}
    for name in profile_artists:
        if name in library:
            return name
    for name in sorted(library):
        return name
    return None


def prepare_copies():
    """真数据 -> /tmp 副本（T10-6 的铁律: 验收不许动真文件）。"""
    tmp = tempfile.mkdtemp(prefix="t10-")
    wall = os.path.join(tmp, "wall_data.jsonl")
    library = os.path.join(tmp, "music_library.jsonl")
    profile = os.path.join(tmp, "user_profile.jsonl")
    shutil.copyfile(os.path.join(REPO, "config/wall_data.jsonl"), wall)
    shutil.copyfile(os.path.join(REPO, "config/music_library.jsonl"), library)
    if os.path.exists(os.path.join(REPO, "config/user_profile.jsonl")):
        shutil.copyfile(os.path.join(REPO, "config/user_profile.jsonl"), profile)
    return tmp, wall, library, profile


def build_config(wall, library, profile, *, autofill_target=6, trigger_chars=60):
    config = load_config("config")
    config = json.loads(json.dumps(config, default=str))       # 深拷贝, 不碰缓存
    config.setdefault("wallpaper", {}).setdefault("tagging", {})
    config["wallpaper"]["tagging"]["data_file"] = wall         # 计数/索引都指副本
    config.setdefault("music", {})
    config["music"]["library_file"] = library                  # 清零只清副本
    config["music"]["autofill"] = {"enabled": True, "target": autofill_target,
                                   "by_artist": False}         # 验收不出去搜, 只吃本地库
    config["profile"] = {"enabled": True, "trigger_chars": trigger_chars,
                         "trigger_turns": 99, "file": profile}
    return config


async def part_a_window(rt):
    print("\n== A. 三格窗口（真壁纸目录 + 真索引副本 + 真画像副本）")
    print("  壁纸目录 %s: %d 张" % (rt.wallpaper.directory, len(rt.wallpaper.scan())))
    show_window(rt, "起始")

    chosen = rt._choose_next()
    print("  画像挑出来的『下一个』: %s（basis=%s; %s）"
          % (short(chosen.get("path")), chosen.get("basis"), chosen.get("why_text")))
    check("画像参与了挑图（basis=profile）", chosen.get("basis") == "profile",
          str(chosen.get("basis")))

    first = rt.next_wallpaper(1)
    show_window(rt, "① next")
    window1 = rt._window_names()
    check("next 之后屏幕上就是那张", window1["current"] == short(first.get("path")))
    check("next 之后『下一个』是预挑好的", bool(window1["next"]))

    second = rt.next_wallpaper(1)
    show_window(rt, "② next")
    check("连叫两次不是同一张", short(second.get("path")) != short(first.get("path")),
          "%s vs %s" % (short(first.get("path")), short(second.get("path"))))

    rt.next_wallpaper(-1)
    show_window(rt, "③ prev")
    window3 = rt._window_names()
    check("prev 回到真实历史（=①那张）", window3["current"] == short(first.get("path")),
          "%s vs %s" % (window3["current"], short(first.get("path"))))

    before = usage_map(rt._cfg("wallpaper", "tagging", "data_file"))
    staged = rt.next_wallpaper(1, "ip=EVA", None, stage=True)
    after = usage_map(rt._cfg("wallpaper", "tagging", "data_file"))
    window4 = rt._window_names()
    show_window(rt, "④ stage")
    check("stage 不切屏（current 没动）", window4["current"] == window3["current"])
    check("stage 改了『下一个』", window4["next"] == short(staged.get("path")),
          "%s vs %s" % (window4["next"], short(staged.get("path"))))
    check("stage 不推屏（pushed=False）", staged.get("pushed") is False and staged.get("staged") is True)
    check("stage 不计使用次数", before == after)

    advanced = rt.next_wallpaper(1)
    window5 = rt._window_names()
    show_window(rt, "⑤ 推进")
    check("推进后屏幕上就是刚预备的那张",
          short(advanced.get("path")) == short(staged.get("path")),
          "%s vs %s" % (short(advanced.get("path")), short(staged.get("path"))))
    check("推进后又补上了一个『下一个』", bool(window5["next"]))

    picked = rt.next_wallpaper(1, "ip=EVA")
    window6 = rt._window_names()
    show_window(rt, "⑥ pick")
    check("pick 报了 match 详情（第几名/候选数）", "match" in picked, str(picked.get("match")))
    check("pick 后『下一个』也补上了", bool(window6["next"]))

    profile_file = rt.profile_file
    os.rename(profile_file, profile_file + ".off")
    try:
        fallback = rt._choose_next()
    finally:
        os.rename(profile_file + ".off", profile_file)
    check("画像不可用 -> 退回文件名顺序（basis=order）", fallback.get("basis") == "order",
          "%s / %s" % (fallback.get("basis"), fallback.get("why_text")))


async def part_b_queue(rt, config, library):
    print("\n== B. 队列动作（真音乐库副本; 补歌只吃本地库, 不出去搜）")
    await rt._start_music()
    if rt._music_task is not None:                 # 只要队列, 不要轮询
        rt._music_task.cancel()
        rt._music_task = None
    if rt.music is None:
        print("  !! 音乐没起来（music.enabled / pc_host）—— 跳过 B/C")
        return False

    records = user_profile.read_records(rt.profile_file)
    record = records[-1] if records else None
    result = rt.music.refill(record, by_artist=False)
    print("  补歌: 本地库 %d 首, 还差 %d（%s）"
          % (result["from_library"], result["short_by"], "；".join(result["why"]) or "-"))
    print("  队列(%d): %s" % (len(rt.music.queue_ids()), shows(rt)))
    check("队列补起来了", len(rt.music.queue_ids()) > 0, str(len(rt.music.queue_ids())))

    artists = [str(row["name"]) for row in ((record or {}).get("music") or {}).get("artist") or []]
    artist = pick_library_artist(rt, artists)
    print("  挑一个『不想听』的歌手: %s（画像里的歌手: %s）" % (artist, artists[:3]))
    check("音乐库里有歌手可用", bool(artist), str(artist))

    memory = ChatMemory()
    memory.add_user("不想听 %s 了，换个人吧，听腻了" % artist, source="gui", state="idle")
    fresh = await user_profile.build_profile(memory, config=config, llm=None, history=records)
    affected = [str(item) for item in fresh["cleared"]["track_ids"]]
    in_queue = [item for item in affected if item in rt.music.queue_ids()]
    print("  负反馈: %s; 清 0 的源文件计数 %d 首; 其中 %d 首在队列里"
          % (fresh["feedback"][:1], fresh["cleared"]["tracks"], len(in_queue)))
    check("认出了歌手负反馈", bool(fresh["feedback"]), str(fresh["feedback"]))
    check("点名 -> 类似的全部进了 cleared.track_ids", bool(affected), "%d 首" % len(affected))
    check("要清的那几首确实在队列里（否则这条验了个空）", bool(in_queue),
          "%d/%d" % (len(in_queue), len(affected)))

    # ⚠ 顺序照**生产路径**来: `_build_profile_task()` 先 `append_record()` 再
    #   `_apply_profile_effects()`。反过来的话补歌读不到新的 muted 名单, 会**刚去掉又补回来**。
    user_profile.append_record(rt.profile_file, fresh)
    before_ids = rt.music.queue_ids()
    rt._apply_profile_effects(fresh, records[-1] if records else None)
    after_ids = rt.music.queue_ids()
    print("  队列 %d -> %d 首: %s" % (len(before_ids), len(after_ids), shows(rt)))
    check("负反馈那几首从队列里没了", not [item for item in in_queue if item in after_ids])
    check("去掉之后立刻补了队列（且没把去掉的又补回来）",
          len(after_ids) > len(before_ids) - len(in_queue), "%d 首" % len(after_ids))

    rt.music.current_id = after_ids[0] if after_ids else None
    keep = rt.music.current_id
    reset = rt._reset_queue_on_mood_change({"mood": {"label": "tired"}},
                                           {"mood": {"label": "happy"}})
    print("  心情 happy -> tired: %s" % reset)
    check("两个已知心情不同 -> 重置队列", bool(reset))
    check("重置时留住正在放的那首", bool(keep) and keep in rt.music.queue_ids(), str(keep))
    check("心情没变 -> 什么都不做",
          rt._reset_queue_on_mood_change({"mood": {"label": "tired"}},
                                         {"mood": {"label": "tired"}}) is None)
    check("unknown 不算变化（不许因此清队列）",
          rt._reset_queue_on_mood_change({"mood": {"label": "unknown"}},
                                         {"mood": {"label": "tired"}}) is None)
    return True


async def part_c_end_to_end(rt):
    print("\n== C. 端到端: 真 _profile_tick()（攒够 -> 后台构建 -> 落盘 -> 重挑 next + 队列动作）")
    before_lines = len(user_profile.read_records(rt.profile_file))
    before_queue = rt.music.queue_ids() if rt.music else []
    show_window(rt, "构建前")
    records = user_profile.read_records(rt.profile_file)
    artists = [str(row["name"]) for row in ((records[-1] if records else {}).get("music")
                                            or {}).get("artist") or []]
    artist = pick_library_artist(rt, artists) if rt.music else None
    print("  用好恶里的歌手 %s 当靶子; 队列现在是 %s" % (artist, shows(rt) if rt.music else "-"))
    for _ in range(3):                              # 每句都带歌手名, 负反馈才抓得到
        rt.chat_memory.add_user(
            "今天有点累，学了一整天了，眼睛也酸。%s 那首听得有点腻了，不太想再听，"
            "换个人吧，安静点的就好，我现在不想动脑子，谢谢你陪着我。" % artist,
            source="gui", state="study")
        rt.chat_memory.add_reply("好，我记下了，你先歇会儿", source="gui", state="study")
    rt.chat_memory.add_user("顺便记一下，我家的猫叫豆豆，今天很乖。", source="gui", state="study")
    print("  攒了 %d 字 / %d 轮（触发线 %d 字）"
          % (rt.chat_memory.pending_chars(), rt.chat_memory.pending_turns(),
             rt._profile_trigger_chars))
    rt._profile_tick()
    check("攒够就触发了构建", rt._profile_task is not None)
    if rt._profile_task is None:
        return
    record = await rt._profile_task
    check("构建成功（没抛 / 没返回 None）", record is not None)
    if record is None:
        return
    lines = user_profile.read_records(rt.profile_file)
    affected = [str(item) for item in record["cleared"]["track_ids"]]
    print("  记录 +%d 行; 心情=%s/%s 置信=%s（判心情 %s ms）; 清零 图%d/歌%d; 负反馈 %d 条; 摘录 %d 字"
          % (len(lines) - before_lines, record["mood"]["label"], record["mood"]["zh"],
             record["mood"]["confidence"], record["mood"].get("ms"),
             record["cleared"]["wall_images"], record["cleared"]["tracks"],
             len(record["feedback"]),
             int((record.get("mood") or {}).get("excerpt_chars") or 0)))
    check("落盘了（副本文件多了一行）", len(lines) == before_lines + 1)
    text = json.dumps(record, ensure_ascii=False)
    check("记录里没有整段对话（无关的那句不在里面）", "豆豆" not in text)
    check("认出了负反馈", bool(record["feedback"]), str(record["feedback"][:1]))
    check("类似的全部进了 cleared.track_ids", bool(affected), "%d 首" % len(affected))
    if rt.music:
        after_queue = rt.music.queue_ids()
        print("  队列 %d -> %d 首: %s" % (len(before_queue), len(after_queue), shows(rt)))
        check("负反馈那几首不在队列里（去掉之后没被补歌补回来）",
              not [item for item in affected if item in after_queue])
        check("队列又被补起来了（本地库不够就少几首, 但不会空）", bool(after_queue))
    show_window(rt, "构建后")
    check("构建完『下一个』是有的（画像变了要重挑）", bool(rt.wallpaper.window()["next"])
          or not rt.wallpaper.scan())


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--llm", action="store_true", help="起本机 llama-server, 真问一次心情")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    real_before = {name: md5(os.path.join(REPO, name)) for name in REAL_FILES}
    print("== 真数据 md5（跑之前）")
    for name, digest in real_before.items():
        print("  %-32s %s" % (name, digest))

    tmp, wall, library, profile = prepare_copies()
    print("  副本目录: %s" % tmp)
    config = build_config(wall, library, profile)
    runtime = Runtime(config=config, start_native=False, start_terminal=False,
                      log=logging.getLogger("t10"))
    await runtime._start_state_and_tools()
    await runtime._start_profile()
    print("  画像文件 = %s（副本）" % runtime.profile_file)
    if args.llm:
        await runtime._start_llm()
        mode = getattr(runtime.llm, "mode", None)
        print("  LLM = %s" % (mode() if callable(mode) else mode))

    try:
        await part_a_window(runtime)
        ok = await part_b_queue(runtime, config, library)
        if ok:
            await part_c_end_to_end(runtime)
    finally:
        await runtime.stop()

    print("\n== 真数据 md5（跑之后）")
    real_after = {name: md5(os.path.join(REPO, name)) for name in REAL_FILES}
    for name, digest in real_after.items():
        same = real_before[name] == digest
        print("  %-32s %s %s" % (name, digest, "没动 ✔" if same else "**变了** ✘"))
        if not same:
            _FAILED.append("真文件被改: %s" % name)

    print("\n== 结果: %s" % ("全部通过" if not _FAILED else "失败 %d 项: %s"
                           % (len(_FAILED), "、".join(_FAILED))))
    return 1 if _FAILED else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
