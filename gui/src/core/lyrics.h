// ============================================================================
//  gui/src/core/lyrics.h — 歌词：时间轴 + 预留接口的真实实现（T15-16）
//
//  分两层，别混：
//    · `TimedLyrics`  —— **纯数据 + 纯函数**：一份 `{t, text, tr}` 时间轴，和
//      "给一个播放位置，现在该显示哪一句、下一句是什么、下一次换行在几秒"。
//      它**不认识时钟、也不认识控件**（所以能被单测钉得死死的）。
//    · `LyricsProvider` / `TimedLyricsProvider` —— D5 就预留好的那层接口。
//      界面只认 `LyricsProvider*`，将来换歌词源（本地 LRC / 别的服务）不用动界面。
//
//  数据从哪来（协议侧）
//  ---------------------------------------------------------------------------
//  `music` 推送里那四个字段（见 docs/ipc-protocol.md §3）：
//      lyric_ok(bool) / lyric_lines([{t,text,tr}…]) / lyric_rev(number) / lyric_reason(string)
//  字段名**只在本文件的 `applyPayload()` 里出现一次** —— 协议改名就改那一处。
//
//  三条口径（与 agent/core/lyrics.py 成对，见 T15-16 方案）
//  ---------------------------------------------------------------------------
//    · **显示串优先译文**：`tr` 非空取 `tr`，否则取原文（用户 2026-10-04 决定）。
//    · **空行不产出行**：间奏那种只有时间戳的行在 Agent 侧就丢掉了，所以这里
//      "上一句继续显示"是选行逻辑天然给的（`indexAt` 找的是最后一行 `t <= 位置`）。
//    · **`rev` 变了就整份换掉**：Agent 只在内容真变时 +1（协议 §3 写了）。
//    ⚠ **时间轴里的 `t` 已经算进 `music.lyric_offset_ms` 的补偿** —— 这里不再移位。
// ============================================================================
#pragma once

#include <QJsonArray>
#include <QJsonObject>
#include <QString>
#include <QVector>

namespace core {

/// 一行歌词。
struct LyricLine {
    double t = 0.0;   ///< 起始秒（**已含 offset 补偿**）
    QString text;     ///< 原文
    QString tr;       ///< 译文（空串 = 这一行没有译文）

    /// 该显示的那一句：**优先译文**，没有译文就退回原文。
    QString shown() const { return tr.isEmpty() ? text : tr; }
};

/// 带时间轴的歌词（纯数据 + 纯函数）。
class TimedLyrics {
public:
    /// 从一条 `music` 推送里取歌词（`lyric_*` 四个字段；缺字段按"还没有"处理）。
    /// @return 这份歌词**换了没有**（`rev` 或内容变了才算换）
    bool applyPayload(const QJsonObject& music);

    /// 直接给一份（测试/演示用）。`ok=false` 时 `reason` 是给人看的一句话。
    void setLines(const QJsonArray& rows, int rev, bool ok, const QString& reason = QString());

    /// 清空（例：还没收到任何 `music` 推送）。
    void clear();

    /// 有没有可用歌词（= 协议里的 `lyric_ok`）。注意：`false` 且 `reason()` 为空 =
    /// **还没取到**（新曲目刚起播那几秒），不是"这首歌没有歌词"。
    bool hasLyric() const { return ok_; }
    bool isEmpty() const { return lines_.isEmpty(); }
    QString reason() const { return reason_; }
    int rev() const { return rev_; }
    int count() const { return lines_.size(); }
    const QVector<LyricLine>& lines() const { return lines_; }

    /// 当前该显示第几行。没有任何一行 `t <= positionS`（前奏里）时给 **-1**。
    int indexAt(double positionS) const;

    /// 该显示的那一句（`indexAt` 给不出行时是空串）。
    QString lineAt(double positionS) const;

    /// 下一句（已经是最后一句 / 还没到第一句时是空串）。
    QString nextLineAt(double positionS) const;

    /// **下一次换行发生在第几秒**（没有下一次给 -1）。
    /// 调用方据此把定时器**只定到那一刻**，而不是每 100 ms 轮询一次。
    double nextChangeAt(double positionS) const;

private:
    QVector<LyricLine> lines_;
    int rev_ = 0;
    bool ok_ = false;
    QString reason_;
};

// ---------------------------------------------------------------------------
//  D5 预留的接口（实现见下）
// ---------------------------------------------------------------------------
class LyricsProvider {
public:
    virtual ~LyricsProvider() = default;

    /// 来源是否可用。false → UI 显示"未接入"占位而不是空白。
    virtual bool available() const = 0;

    /// 当前应显示的那一行歌词；没有则返回空字符串。
    virtual QString currentLine() const = 0;

    /// 下一行（T15-16：音乐条右列的第二行）。没有就给空串。
    virtual QString nextLine() const { return QString(); }

    /// 没有歌词时给人看的一句话（空 = 还没取到）。
    virtual QString reason() const { return QString(); }

    /// 当前播放位置（秒）。**默认空实现**（占位/别的来源可以不理它）：
    /// 界面在每个 tick / 每次收到推送时先喂位置，再问上面那三样。
    virtual void setPosition(double positionS) { (void)positionS; }
};

/// 占位实现：没有歌词来源。任何取值都返回"不可用/空"。
class NullLyricsProvider : public LyricsProvider {
public:
    bool available() const override { return false; }
    QString currentLine() const override { return QString(); }
};

/// **真实现**：把一份 `TimedLyrics` 包成接口。
///
/// ⚠ 这个接口是"无参拉取"的（D5 的形状），所以位置得由调用方**先喂进来**：
///      provider.setPosition(当前播放秒);   // 每个 tick / 每次收到推送时
///      provider.currentLine() / nextLine();
/// 播放/暂停的判定在调用方（它才知道 `playing`）—— 暂停时别再喂新位置即可。
class TimedLyricsProvider : public LyricsProvider {
public:
    /// @param lyrics 生命周期由调用方保证（通常和界面同寿）
    explicit TimedLyricsProvider(const TimedLyrics* lyrics = nullptr) : lyrics_(lyrics) {}

    void setSource(const TimedLyrics* lyrics) { lyrics_ = lyrics; }
    void setPosition(double positionS) override { position_ = positionS; }

    bool available() const override;
    QString currentLine() const override;
    QString nextLine() const override;
    QString reason() const override;

private:
    const TimedLyrics* lyrics_ = nullptr;
    double position_ = 0.0;
};

} // namespace core
