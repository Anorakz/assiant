# -*- coding: utf-8 -*-
"""agent/core/notes.py —— "攒话 + 取走即清空"的唯一实现（T15-3 / 3-6a）

原来 `BilibiliBuffer` / `GameWatcher` / `StudyWatcher` 各写了一份一模一样的
`notes()`（三行：拷贝、清空、返回）。三处的语义都是"把要如实告诉用户的话取走，
取走即清空"，所以收敛成这一个函数 —— 它**就地清空**传入的列表，
调用方只要 `return drain(self._notes)`，不需要重新赋值。
"""
from __future__ import annotations

from typing import List


def drain(notes: List[str]) -> List[str]:
    """取走并清空：返回副本，原列表清空（**就地**，调用方无需重新赋值）。"""
    out = list(notes)
    notes.clear()
    return out
