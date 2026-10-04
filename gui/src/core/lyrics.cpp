// ============================================================================
//  gui/src/core/lyrics.cpp — 歌词时间轴的实现（T15-16-4）
//
//  只有三件事：**收一份载荷**、**按位置选行**、**报下一次换行在几秒**。
//  时钟与控件都不在这里（见 lyrics.h 顶部的分层说明）。
// ============================================================================
#include "core/lyrics.h"

#include <QJsonValue>

#include <algorithm>

namespace core {

namespace {

/// 一行 JSON -> LyricLine；形状不对给 `ok=false`（坏行**跳过**，不抛也不造假的 0.0）。
bool parseLine(const QJsonValue& value, LyricLine* out)
{
    if (!value.isObject()) {
        return false;
    }
    const QJsonObject row = value.toObject();
    const QJsonValue stamp = row.value(QStringLiteral("t"));
    if (!stamp.isDouble()) {
        return false;                       // 没有时间戳的行没法同步，直接丢
    }
    out->t = stamp.toDouble();
    out->text = row.value(QStringLiteral("text")).toString();
    out->tr = row.value(QStringLiteral("tr")).toString();
    return true;
}

} // namespace

bool TimedLyrics::applyPayload(const QJsonObject& music)
{
    const int rev = music.value(QStringLiteral("lyric_rev")).toInt(rev_);
    const bool ok = music.value(QStringLiteral("lyric_ok")).toBool(false);
    const QString reason = music.value(QStringLiteral("lyric_reason")).toString();
    const QJsonArray rows = music.value(QStringLiteral("lyric_lines")).toArray();
    // ⚠ 按协议，内容真变了 `lyric_rev` 必然跟着变（docs/ipc-protocol.md §3）—— 所以
    //   rev/ok/reason 三个都一样就当"没换"，不逐行比（那既要有 operator==，又要在
    //   "对端发了新行却忘了升 rev"这种不守规矩的情况下做歧义判断）。
    if (rev == rev_ && ok == ok_ && reason == reason_) {
        return false;
    }
    setLines(rows, rev, ok, reason);
    return true;
}

void TimedLyrics::setLines(const QJsonArray& rows, int rev, bool ok, const QString& reason)
{
    QVector<LyricLine> parsed;
    parsed.reserve(rows.size());
    for (const QJsonValue& value : rows) {
        LyricLine line;
        if (parseLine(value, &line)) {
            parsed.append(line);
        }
    }
    // Agent 侧已经排好序了，这里再排一次是**防御**：客户端不该假设对端永远守规矩。
    std::stable_sort(parsed.begin(), parsed.end(),
                     [](const LyricLine& a, const LyricLine& b) { return a.t < b.t; });
    lines_ = parsed;
    rev_ = rev;
    ok_ = ok && !parsed.isEmpty();          // "说有歌词却一行都没有" -> 按没有处理
    reason_ = ok_ ? QString() : reason;
}

void TimedLyrics::clear()
{
    lines_.clear();
    ok_ = false;
    reason_.clear();
}

int TimedLyrics::indexAt(double positionS) const
{
    // 最后一行 `t <= positionS`（前奏里一行都不到 -> -1）
    int found = -1;
    for (int i = 0; i < lines_.size(); ++i) {
        if (lines_.at(i).t <= positionS) {
            found = i;
        } else {
            break;                              // 已排序：后面只会更大
        }
    }
    return found;
}

QString TimedLyrics::lineAt(double positionS) const
{
    const int index = indexAt(positionS);
    return index < 0 ? QString() : lines_.at(index).shown();
}

QString TimedLyrics::nextLineAt(double positionS) const
{
    const int next = indexAt(positionS) + 1;
    return next >= lines_.size() ? QString() : lines_.at(next).shown();
}

double TimedLyrics::nextChangeAt(double positionS) const
{
    const int next = indexAt(positionS) + 1;
    return next >= lines_.size() ? -1.0 : lines_.at(next).t;
}

// -------------------------------------------------------------- Provider ---
bool TimedLyricsProvider::available() const
{
    return lyrics_ != nullptr && lyrics_->hasLyric();
}

QString TimedLyricsProvider::currentLine() const
{
    return lyrics_ == nullptr ? QString() : lyrics_->lineAt(position_);
}

QString TimedLyricsProvider::nextLine() const
{
    return lyrics_ == nullptr ? QString() : lyrics_->nextLineAt(position_);
}

QString TimedLyricsProvider::reason() const
{
    return lyrics_ == nullptr ? QString() : lyrics_->reason();
}

} // namespace core
