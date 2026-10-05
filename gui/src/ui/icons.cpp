// ============================================================================
//  gui/src/ui/icons.cpp — 图标加载与染色
// ============================================================================
#include "ui/icons.h"

#include <QColor>
#include <QFileInfo>
#include <QHash>
#include <QPainter>
#include <QPixmap>
#include <QStringList>

namespace ui {

namespace {

/// 图标名清单（与 resources/icons/*.svg 一一对应）。
/// ⚠ 它的守卫在 gui/tests/test_icons.cpp：每个名字都必须能取到非空 QIcon，
///   而且数量要和 resources/icons/*.svg 对得上 —— 加图标时两边一起改。
const char* const kNames[] = {
    "home",      "model",   "system",  "settings", "prev",   "next",
    "play",      "pause",   "fullscreen", "speed", "wallpaper", "watchdog",
    "music",     "chat",    "link",    "power",    "stop",   "report",
    "clock",     "cpu",     "memory",  "sdcard",   "schedule",
};

/// T15-16 G-B-2：染色结果缓存 ✓（键 = 名字 + HexArgb 颜色 ✓）
QHash<QString, QIcon>& tintedCache()
{
    static QHash<QString, QIcon> cache;
    return cache;
}

int g_tintedRenders = 0;      ///< 真渲染次数（命中缓存不自增 ✓）

} // namespace

int tintedIconRenders()
{
    return g_tintedRenders;
}

void resetTintedIconCache()
{
    tintedCache().clear();
    g_tintedRenders = 0;
}

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
    // T15-16 G-B-2：同一 (图标, 颜色) 只渲染一次 ✓
    //   改前每次调用都重来一遍 SVG→QPixmap→SourceIn 合成 ✗ ——
    //   而它在**状态变化**时被调（播放/暂停切换、链路变化 ✓），属于白烧 ✓。
    //   ⚠ 键里带 HexArgb：把 alpha 也算进去 ✓（HUD 上同色不同透明度是两种图标 ✓）。
    const QString key = name + QLatin1Char('#') + color.name(QColor::HexArgb);
    const auto hit = tintedCache().constFind(key);
    if (hit != tintedCache().constEnd()) {
        return hit.value();
    }
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
    ++g_tintedRenders;                 // 只有真渲染才自增 ✓
    const QIcon result(tinted);
    tintedCache().insert(key, result);
    return result;
}

} // namespace ui
