#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""T12-5 板端验收: 日程配置的**文本级增删**（对真配置的**副本**跑, 真文件只做 md5 核对）。

跑法（板端, 仓库根）::

    python3 tests/board/t12_writers_accept.py

验的是这五件事（都用**真配置 + 真 Scheduler**）:

    A. 加: 每周那条进 `recurring`、带日期的那条进 `oneoff`; 用真的
       `ScheduleEvent.from_config` 读回来就是同一条（状态/时刻/星期都对）。
    B. 查重: 同样的 `state + start + days` 再来一次 -> 不写, 只说"已经有一条一样的了"。
    C. 删: `remove_entry_in_file()` 认得出它在哪个序列, 删完**逐字节**回到原文
       （注释数不变、长度不变）。
    D. 真文件没被碰: `config/config.yaml` 前后 md5 一样。
    E. 换行: CRLF 的副本走一遍, 出来的还是纯 CRLF; `.bak` 逐字节等于"上一次写之前"。

⚠ 这是**验收脚本**（要真配置 + 板端 python3.8）, 不进 `scripts/test-python.*` 的常规清单。
⚠ T12-7 会往这个脚本里继续加"真 Agent + 日程到点 + `set_schedule` 工具热生效"那几段。
"""
from __future__ import annotations

import hashlib
import io
import os
import sys
import tempfile

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

import yaml                                                        # noqa: E402

from agent.core import schedule_config as sc                       # noqa: E402
from agent.core.scheduler import Scheduler, entry_matcher          # noqa: E402
from agent.core.state_machine import StateMachine                  # noqa: E402
from agent.io import ChatInputBus                                  # noqa: E402

if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
REAL_CONFIG = os.path.join(REPO, "config", "config.yaml")

_FAILED = []


def check(name, ok, detail=""):
    print("  %s %s%s" % ("✔" if ok else "✘", name, ("  [%s]" % detail) if detail else ""))
    if not ok:
        _FAILED.append(name)


def md5(path):
    digest = hashlib.md5()
    with open(path, "rb") as handle:
        digest.update(handle.read())
    return digest.hexdigest()


def load(path):
    """用**真的** Scheduler 读一份配置（事件与警告都由它算）。"""
    with open(path, "rb") as handle:
        data = yaml.safe_load(handle.read().decode("utf-8"))
    return Scheduler(state=StateMachine(), bus=ChatInputBus(), config=data)


def main():
    if not os.path.isfile(REAL_CONFIG):
        print("找不到 %s —— 这个验收要在板端仓库根跑" % REAL_CONFIG)
        return 2
    before = md5(REAL_CONFIG)

    tmpdir = tempfile.mkdtemp(prefix="t12-writers-")
    tmp = os.path.join(tmpdir, "config.yaml")
    with open(REAL_CONFIG, "rb") as src, open(tmp, "wb") as dst:
        dst.write(src.read())
    with open(tmp, "rb") as handle:
        orig = handle.read()

    print("A. 加两条（每周 + 一次性）")
    wrote, why = sc.add_entry_in_file(tmp, {"state": "study", "start": "9:30",
                                            "days": ["mon", "wed"]})
    check("加 recurring", wrote and not why, why or "09:30 -> STUDY / [mon, wed]")
    wrote, why = sc.add_entry_in_file(tmp, {"state": "sleep", "start": "22:05",
                                            "date": "2026-12-31"})
    check("加 oneoff", wrote and not why, why or "2026-12-31 22:05 -> SLEEP")

    scheduler = load(tmp)
    added = [e for e in scheduler.events if e.start in ((9, 30), (22, 5))]
    check("真的 Scheduler 读回来 2 条", len(added) == 2, str([e.label() for e in added]))
    recurring = [e for e in added if e.on is None]
    oneoff = [e for e in added if e.on is not None]
    check("recurring 的 state/星期", bool(recurring) and recurring[0].state.value == "study"
          and recurring[0].days == {0, 2},
          "state=%s days=%s" % (recurring and recurring[0].state.value,
                                sorted(recurring[0].days) if recurring else None))
    check("oneoff 的 state/日期", bool(oneoff) and oneoff[0].state.value == "sleep"
          and oneoff[0].on.isoformat() == "2026-12-31",
          oneoff and oneoff[0].on.isoformat())

    print("B. 查重")
    wrote, why = sc.add_entry_in_file(tmp, {"state": "study", "start": "09:30",
                                            "days": ["mon", "wed"]},
                                      matches=entry_matcher(recurring[0]))
    check("同一条再来一次不写", not wrote and "已经有一条一样的了" in why, why)

    print("C. 删掉刚加的两条")
    removed, why, found = sc.remove_entry_in_file(tmp, entry_matcher(recurring[0]))
    check("删 recurring", removed and found["key"] == "recurring",
          "key=%s start=%s" % (found and found["key"], found and found["fields"].get("start")))
    with open(tmp, "rb") as handle:
        before_last = handle.read()
    removed, why, found = sc.remove_entry_in_file(tmp, entry_matcher(oneoff[0]))
    check("删 oneoff", removed and found["key"] == "oneoff", why)

    with open(tmp, "rb") as handle:
        after = handle.read()
    check("删完逐字节回到原文", after == orig, "%d -> %d 字节" % (len(orig), len(after)))
    check("注释一条不少", after.count(b"#") == orig.count(b"#"),
          "%d 条注释" % after.count(b"#"))

    print("D. 真配置没被碰")
    check("config.yaml md5 不变", md5(REAL_CONFIG) == before, before)

    print("E. CRLF")
    crlf = os.path.join(tmpdir, "crlf.yaml")
    with open(crlf, "wb") as handle:
        handle.write(orig.replace(b"\n", b"\r\n"))
    with open(crlf, "rb") as handle:
        crlf_before = handle.read()
    sc.add_entry_in_file(crlf, {"state": "idle", "start": "07:00"})
    with open(crlf, "rb") as handle:
        raw = handle.read()
    check("加完还是纯 CRLF", b"\n" not in raw.replace(b"\r\n", b""),
          "%d 行 CRLF" % raw.count(b"\r\n"))
    check(".bak = 上一次写之前那份", read_bytes(sc_backup(tmp)) == before_last)
    check("CRLF 的 .bak 逐字节相等", read_bytes(sc_backup(crlf)) == crlf_before)

    for path in (tmp, sc_backup(tmp), crlf, sc_backup(crlf)):
        if os.path.exists(path):
            os.unlink(path)
    os.rmdir(tmpdir)

    print("")
    if _FAILED:
        print("FAILED: %d 项 -> %s" % (len(_FAILED), ", ".join(_FAILED)))
        return 1
    print("OK: T12-5 的文本级增删在板端全部通过")
    return 0


def sc_backup(path):
    return path + sc.BACKUP_SUFFIX


def read_bytes(path):
    with open(path, "rb") as handle:
        return handle.read()


if __name__ == "__main__":
    sys.exit(main())
