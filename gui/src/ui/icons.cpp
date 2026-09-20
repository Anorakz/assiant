// ============================================================================
//  gui/src/ui/icons.cpp — 图标加载与染色
// ============================================================================
#include "ui/icons.h"

#include <QColor>
#include <QFileInfo>
#include <QPainter>
#include <QPixmap>
#include <QStringList>

namespace ui {

namespace {

/// 图标名清单（与 resources/icons/*.svg 一一对应；测试会核对数量）
const char* const kNames[] = {
    "home",      "model",   "system",  "settings", "prev",   "next",
    "play",      "pause",   "fullscreen", "speed", "wallpaper", "watchdog",
    "music",     "chat",    "link",    "power",    "stop",   "report",
    "clock",     "cpu",     "memory",  "sdcard",
};

} // namespace

QStringList iconNames()
{
    QStringList names;
    for (const char* name : kNames) {
        names << QString::fromLatin1(name);
    }
    return names;
}

QIcon icon(const QString& name)
{
    const QPixmap pixmap(QStringLiteral(":/icons/%1.svg").arg(name));
    return pixmap.isNull() ? QIcon() : QIcon(pixmap);
}

QIcon tintedIcon(const QString& name, const QColor& color)
{
    const QPixmap source(QStringLiteral(":/icons/%1.svg").arg(name));
    if (source.isNull()) {
        return QIcon();
    }
    QPixmap tinted(source.size());
    tinted.fill(Qt::transparent);
    QPainter painter(&tinted);
    painter.drawPixmap(0, 0, source);
    painter.setCompositionMode(QPainter::CompositionMode_SourceIn);   // 只保留形状
    painter.fillRect(tinted.rect(), color);
    painter.end();
    return QIcon(tinted);
}

} // namespace ui
