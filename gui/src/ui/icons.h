// ============================================================================
//  gui/src/ui/icons.h — 单色 SVG 图标（T14，22 个自绘）
//
//  图标资源在 gui/resources/icons/*.svg（.qrc 前缀 :/icons）。

//  SVG 本身画的是 #E6E6E6，但界面里不同位置要不同颜色（导航选中/未选中、
//  控制条按钮、警示），所以这里用 QPainter 的 SourceIn 合成**按需染色**：
//  真正的单色图标就该这样用，而不是每个颜色导一份资源。
// ============================================================================
#pragma once

#include <QIcon>
#include <QString>

namespace ui {

/// 按名字取图标（不带扩展名，如 "home" / "play"）。名字不认识返回空 QIcon。
QIcon icon(const QString& name);

/// 取图标并染成指定颜色（alpha 由 SVG 的描边决定）
/// ⚠ T15-16 G-B-2：同一 `(图标, 颜色)` 只**真渲染一次** ✓（结果带缓存 ✓）
QIcon tintedIcon(const QString& name, const QColor& color);

/// T15-16 G-B-2：**真正渲染过的次数**（命中缓存不算 ✓）—— 单测 / 打点判据 ✓
int tintedIconRenders();

/// 清空染色缓存并把计数归零（单测用 ✓）
void resetTintedIconCache();

/// 全部图标名（验收/文档用）
QStringList iconNames();

} // namespace ui
