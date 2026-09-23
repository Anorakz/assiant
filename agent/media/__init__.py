# ============================================================================
#  agent/media/__init__.py — 本地媒体库（Phase 7 T8）
#
#  目前只有一个东西: `music_library` —— 本地音乐库 `config/music_library.jsonl`
#  （一行一首歌: id + tags + 播放次数）。**不再依赖云歌单**: 云歌单只用来导入一次 id。
#
#  ⚠ 与 `agent/vision/` 的分工: 那边是**壁纸**的标签数据（SigLIP 打的向量 + 标签）,
#    这边是**音乐**的本地库（元数据 + chat 补的 tag + 播放次数）。两者都住 config/ 下、
#    都是派生/本地数据、都进 .gitignore, 但骨架完全不同（音乐这边**没有**向量 ——
#    我们没做音频特征, 这一点在 docs/music.md 里写明了）。
#
#  ⚠ 本包 import 时不联网、不读文件、不 import numpy; 纯逻辑, 开发机也能跑。
# ============================================================================

from .music_library import (
    DEFAULT_LIBRARY_FILE,
    RECORD_VERSION,
    TAG_AXES_AUTO,
    MusicLibraryError,
    add_tags,
    auto_tags,
    bump_play,
    default_library_file,
    guess_lang,
    load,
    make_record,
    pick,
    read_tracks,
    remove_tags,
    resolve_library_file,
    summarise,
    tag_counts,
    upsert_tracks,
    write_tracks,
)

__all__ = [
    "MusicLibraryError",
    "RECORD_VERSION",
    "DEFAULT_LIBRARY_FILE",
    "TAG_AXES_AUTO",
    # 路径
    "default_library_file",
    "resolve_library_file",
    # 记录 / 读写
    "make_record",
    "load",
    "read_tracks",
    "write_tracks",
    # 合并 / 打标 / 计数
    "upsert_tracks",
    "add_tags",
    "remove_tags",
    "bump_play",
    # 查询
    "pick",
    "tag_counts",
    "summarise",
    # 自动打标
    "auto_tags",
    "guess_lang",
]
