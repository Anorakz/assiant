// ============================================================================
//  gui/src/core/idle_watcher.cpp — 唤醒计时/状态实现
// ============================================================================
#include "core/idle_watcher.h"

#include <QEvent>
#include <QTimer>

namespace core {

IdleWatcher::IdleWatcher(QObject* parent)
    : QObject(parent)
{
    timer_ = new QTimer(this);
    timer_->setSingleShot(true);
    connect(timer_, &QTimer::timeout, this, [this]() {
        if (!enabled_) {
            return;
        }
        idle_ = true;
        ++idleCount_;
        emit wentIdle();
    });
    arm();
}

void IdleWatcher::installOn(QObject* app)
{
    if (app != nullptr) {
        app->installEventFilter(this);
    }
}

void IdleWatcher::arm()
{
    if (enabled_) {
        timer_->start(idleMs_);
    }
}

void IdleWatcher::setIdleMs(int ms)
{
    idleMs_ = (ms < 0) ? 0 : ms;
    if (enabled_ && !idle_) {
        arm();                     // 正在计时就按新值重新计时
    }
}

void IdleWatcher::setEnabled(bool on)
{
    if (enabled_ == on) {
        return;
    }
    enabled_ = on;
    if (!enabled_) {
        timer_->stop();
        if (idle_) {               // 关掉唤醒机制 = 立即恢复显示
            idle_ = false;
            ++wakeCount_;
            emit woke();
        }
    } else {
        arm();
    }
}

void IdleWatcher::notifyActivity()
{
    if (!enabled_) {
        return;
    }
    if (idle_) {
        idle_ = false;
        ++wakeCount_;
        emit woke();
    }
    arm();
}

bool IdleWatcher::eventFilter(QObject* watched, QEvent* event)
{
    switch (event->type()) {
    case QEvent::MouseButtonPress:
    case QEvent::KeyPress:
    case QEvent::Wheel:
    case QEvent::TouchBegin:
        notifyActivity();
        break;
    default:
        break;
    }
    return QObject::eventFilter(watched, event);   // 不吃事件，照常派发
}

} // namespace core
