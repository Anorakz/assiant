// ============================================================================
//  gui/src/core/bilibili_format.cpp — 纯函数实现（T11-7）
// ============================================================================
#include "core/bilibili_format.h"

namespace bilibili {

QString formatPlayCount(qint64 play)
{
    if (play <= 0) {
        return QStringLiteral("0");
    }
    if (play < 10000) {
        return QString::number(play);
    }
    // 「万」保留一位小数：128.9万 / 1.0万（不四舍五入成 129万，少一位就少一份信息）
    const double wan = static_cast<double>(play) / 10000.0;
    return QStringLiteral("%1万").arg(QString::number(wan, 'f', 1));
}

QString formatDuration(int seconds)
{
    if (seconds <= 0) {
        return QStringLiteral("0:00");
    }
    const int hours = seconds / 3600;
    const int minutes = (seconds % 3600) / 60;
    const int secs = seconds % 60;
    if (hours > 0) {
        return QStringLiteral("%1:%2:%3")
            .arg(hours)
            .arg(minutes, 2, 10, QLatin1Char('0'))
            .arg(secs, 2, 10, QLatin1Char('0'));
    }
    return QStringLiteral("%1:%2").arg(minutes).arg(secs, 2, 10, QLatin1Char('0'));
}

QString videoUrl(const QString& bvid)
{
    const QString clean = bvid.trimmed();
    if (clean.isEmpty()) {
        return QString();
    }
    return QStringLiteral("https://www.bilibili.com/video/%1").arg(clean);
}

QString previewMeta(int durationS, qint64 play)
{
    const bool hasDuration = durationS > 0;
    const bool hasPlay = play > 0;
    if (hasDuration && hasPlay) {
        return QStringLiteral("%1 · %2")
            .arg(formatDuration(durationS), formatPlayCount(play));
    }
    if (hasDuration) {
        return formatDuration(durationS);
    }
    if (hasPlay) {
        return formatPlayCount(play);
    }
    return QString();
}

int visibleCells(int viewportWidth, int cellWidth, int spacing)
{
    if (cellWidth <= 0) {
        return 1;
    }
    const int step = cellWidth + qMax(0, spacing);
    const int cells = (qMax(0, viewportWidth) + qMax(0, spacing)) / step;
    return qMax(1, cells);
}

QJsonObject videoStatePayload(qint64 positionMs, qint64 durationMs, bool playing, bool eof)
{
    QJsonObject payload;
    payload.insert(QStringLiteral("position_s"), static_cast<double>(qMax<qint64>(0, positionMs)) / 1000.0);
    payload.insert(QStringLiteral("duration_s"), static_cast<double>(qMax<qint64>(0, durationMs)) / 1000.0);
    payload.insert(QStringLiteral("playing"), playing);
    payload.insert(QStringLiteral("eof"), eof);
    return payload;
}

} // namespace bilibili
