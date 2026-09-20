// ============================================================================
//  gui/src/core/lyrics.h — 歌词来源接口（D5：实时字幕=歌词，先占位）
//
//  方案 §3.3 的"实时字幕"槽位要显示歌词，但板端目前**没有歌词来源**，
//  协议 `music` 也只有 title/playing。所以这里先把接口定下来：
//  将来接歌词服务时实现 LyricsProvider 即可，UI 与调用方不用改。
//
//  当前的实现是 NullLyricsProvider（永远不可用），UI 据此显示"未接入"占位。
// ============================================================================
#pragma once

#include <QString>

namespace core {

class LyricsProvider {
public:
    virtual ~LyricsProvider() = default;

    /// 来源是否可用。false → UI 显示"未接入"占位而不是空白。
    virtual bool available() const = 0;

    /// 当前应显示的那一行歌词；没有则返回空字符串。
    virtual QString currentLine() const = 0;
};

/// 占位实现：没有歌词来源。任何取值都返回"不可用/空"。
class NullLyricsProvider : public LyricsProvider {
public:
    bool available() const override { return false; }
    QString currentLine() const override { return QString(); }
};

} // namespace core
