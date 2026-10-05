// ============================================================================
//  gui/src/ui/sys_page.h — 系统与网络页（T10）
//
//  左边一块是「指标瓷砖」：CPU 占用 / 内存 / NPU 负载 / 频率（npu·dmc·gpu·rkvdec）
//  / 温度（soc·gpu）；右边是网络（各接口 IP + 串流主机状态）+ 看门狗启停（占位）。
//
//  读不到的项一律显示「不可读」而不是 0：板子上 /sys/kernel/debug 可能没挂、
//  某个 devfreq 可能消失，装成 0 会骗人（方案 §7）。
// ============================================================================
#pragma once

#include <QString>
#include <QWidget>

#include "core/system_stats.h"

class QLabel;
class StateBanner;   // T15-16 ②：hint 换状态条 ✓
class QPushButton;
class QGridLayout;
class QTimer;

class SysPage : public QWidget {
    Q_OBJECT

public:
    /// stats 可注入（单测传假根目录）；不传就用真机
    explicit SysPage(QWidget* parent = nullptr);
    SysPage(const core::SystemStats& stats, QWidget* parent = nullptr);

    /// 采一次数据刷新界面。返回 false = 关键来源（/proc）读不到。
    bool refresh();

    /// 串流主机状态（顶栏那三态由 MainWindow 推过来）
    void setStreamHostState(const QString& text);

    /// 看门狗按钮（占位；点了只给说明）
    QPushButton* watchdogButton() const { return watchdog_; }
    QString noteText() const { return noteText_; }
    void triggerWatchdog();

    // 供单测/验收核对界面上真实显示的值（取不到返回空）
    QString metricText(const QString& key) const;

    /// T15-16 遗留②：这条现在是**状态条** ✓（不再是裸 `QLabel` ✗）
    /// ⚠ 名字从 `hintLabel()` 改成 `hintBanner()` ✓ —— 它的 `text()` 与可见性语义都变了 ✓。
    StateBanner* hintBanner() const { return hint_; }

private:
    void build();
    QLabel* addMetric(QGridLayout* grid, int row, int col, const QString& key,
                      const QString& title);
    void setMetric(const QString& key, const QString& text);

    core::SystemStats stats_;
    core::CpuTimes lastCpu_;
    bool hasLastCpu_ = false;

    QHash<QString, QLabel*> values_;
    StateBanner* hint_ = nullptr;   ///< T15-16 ②：读不到 /proc 时的错误条 ✓
    QLabel* streamHost_ = nullptr;
    QPushButton* watchdog_ = nullptr;
    QString noteText_;
};
