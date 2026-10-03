# -*- coding: utf-8 -*-
"""agent/core/paths.py —— 配置里的数据文件路径 → 绝对路径（T15-3 / 3-6a）

原来五个模块各写了一份同样的五行（锚点文件、统计文件、音乐库、壁纸数据…）：
相对路径按**仓库根**解析，绝对路径原样规范化。收敛成一个函数；
`root` 由调用方传（各模块自己的 `_repo_root()` 保留，避免再引入一处重复）。
"""
from __future__ import annotations

import os
def resolve_config_path(configured, default, root: str) -> str:
    """把配置里的路径解析成绝对路径。

    @param configured 配置里写的值（None / 空 / 非字符串 → 用 default）
    @param default    默认值：**字符串或可调用**（可调用时**只在需要时**求值 ——
                      实测踩过：`default_library_file()` 会去读配置，提前求值会改变行为）
    @param root       相对路径的基准（通常是仓库根）
    @note 非字符串一律当"没配"：`str(configured or "")` 会把数字 123 变成 "123"，
          当成文件名去找 —— 那是错的行为。
    """
    text = configured.strip() if isinstance(configured, str) else ""
    if not text:
        return default() if callable(default) else default
    if os.path.isabs(text):
        return os.path.normpath(text)
    return os.path.normpath(os.path.join(root, text))
