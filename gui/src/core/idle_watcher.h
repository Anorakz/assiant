// ============================================================================
//  gui/src/core/idle_watcher.h — 唤醒机制的"计时/状态"部分（不依赖 QWidgets）
//
//  方案 §4：四个边缘区域可各自配置 活动(active) / 锁定(locked)；
//  只有活动区域会在**统一的空闲时间**（wake.idle_ms，四个区共用）到达后隐藏；
//  **点击/触摸 GUI 任意位置**（或任何输入事件）立即唤出全部已隐藏区域。
//
//  本类只负责：计时 + 状态 + 发信号（wentIdle / woke），并把诊断计数暴露出来。
//  真正的隐藏/显示动画在 ui/region_host.*，两边的契约就是这两个信号。
// ============================================================================
#pragma once

#include <QObject>

class QTimer;

namespace core {

class IdleWatcher : public QObject {
    Q_OBJECT

public:
    explicit IdleWatcher(QObject* parent = nullptr);

    /// 把本对象装成 app 级事件过滤器，自动捕获点击/按键/滚轮/触摸。
    void installOn(QObject* app);

    int idleMs() const { return idleMs_; }
    void setIdleMs(int ms);

    bool isEnabled() const { return enabled_; }
    void setEnabled(bool on);

    bool isIdle() const { return idle_; }

    /// 诊断计数（设置页 debug 显示 / 验收日志）。
    int idleCount() const { return idleCount_; }
    int wakeCount() const { return wakeCount_; }

    /// 任何用户输入都该调这个（事件过滤器内部也会调）。
    void notifyActivity();

signals:
    /// 空闲到达：活动区域该隐藏了。
    void wentIdle();
    /// 从空闲中醒来：已隐藏的区域该显示了。
    void woke();

protected:
    bool eventFilter(QObject* watched, QEvent* event) override;

private:
    void arm();

    QTimer* timer_ = nullptr;
    int idleMs_ = 5000;
    bool enabled_ = true;
    bool idle_ = false;
    int idleCount_ = 0;
    int wakeCount_ = 0;
};

} // namespace core
