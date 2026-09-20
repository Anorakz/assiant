// ============================================================================
//  gui/src/ui/region_host.cpp — 边缘区域容器实现
//
//  折叠做法：沿折叠轴把 maximumWidth/Height 动画到 0。
//  ⚠ 必须同时把 minimum 归零，否则布局按内容的 minimumSize 顶住、不会收缩。
//  ⚠ 折叠期间对宿主与内容都开 WA_TransparentForMouseEvents，
//    这样点击会落到下面的主区（否则"隐藏了还挡点击"）。
// ============================================================================
#include "ui/region_host.h"

#include <QHBoxLayout>
#include <QLayout>
#include <QPropertyAnimation>
#include <QVBoxLayout>

RegionHost::RegionHost(Edge edge, QWidget* content, QWidget* parent)
    : QWidget(parent), edge_(edge), content_(content)
{
    if (vertical()) {
        auto* box = new QVBoxLayout(this);
        box->setContentsMargins(0, 0, 0, 0);
        box->setSpacing(0);
        box->addWidget(content_);
    } else {
        auto* box = new QHBoxLayout(this);
        box->setContentsMargins(0, 0, 0, 0);
        box->setSpacing(0);
        box->addWidget(content_);
    }

    anim_ = new QPropertyAnimation(
        this, vertical() ? QByteArrayLiteral("maximumHeight") : QByteArrayLiteral("maximumWidth"),
        this);
    anim_->setDuration(150);

    setActive(false);   // 默认锁定常显；由 applyWakeConfig() 覆盖
}

int RegionHost::naturalSize() const
{
    if (content_ == nullptr) {
        return 0;
    }
    // ⚠ 不能只用 sizeHint()：`setFixedHeight()` 只改 min/max，**不改 sizeHint**。
    //   顶栏就是这种情况——sizeHint 是里面三个标签的 ~30px，而 min/max 被固定成 72px。
    //   只取 sizeHint 会把顶栏压扁（T3 实测：72px → 30px）。所以要与 minimumSize 取大。
    QSize hint = content_->sizeHint();
    const QSize minSize = content_->minimumSize();
    hint.setWidth(qMax(hint.width(), minSize.width()));
    hint.setHeight(qMax(hint.height(), minSize.height()));

    if (vertical()) {
        return hint.height() + innerMargins_.top() + innerMargins_.bottom();
    }
    return hint.width() + innerMargins_.left() + innerMargins_.right();
}

void RegionHost::setInnerMargins(const QMargins& margins)
{
    innerMargins_ = margins;
    if (auto* box = layout()) {
        box->setContentsMargins(margins);
    }
    if (collapsed_) {
        constraintTo(0);
    } else {
        constraintTo(naturalSize());
    }
}

int RegionHost::currentSize() const
{
    return vertical() ? height() : width();
}

void RegionHost::constraintTo(int value)
{
    if (vertical()) {
        setMinimumHeight(0);
        setMaximumHeight(value);
    } else {
        setMinimumWidth(0);
        setMaximumWidth(value);
    }
}

void RegionHost::setMouseTransparent(bool on)
{
    setAttribute(Qt::WA_TransparentForMouseEvents, on);
    if (content_ != nullptr) {
        content_->setAttribute(Qt::WA_TransparentForMouseEvents, on);
    }
}

void RegionHost::setActive(bool active)
{
    active_ = active;
    if (!active_) {
        showRegion(false);     // 锁定 = 常显
    }
}

void RegionHost::hideRegion(bool animate)
{
    if (collapsed_) {
        return;
    }
    if (expanded_ <= 0) {
        expanded_ = qMax(currentSize(), naturalSize());
    }
    if (expanded_ <= 0) {
        expanded_ = currentSize();
    }
    collapsed_ = true;
    setMouseTransparent(true);
    anim_->stop();
    anim_->setDuration(animate ? 150 : 0);
    anim_->setStartValue(expanded_);
    anim_->setEndValue(0);
    anim_->start();
}

void RegionHost::showRegion(bool animate)
{
    const int target = (expanded_ > 0) ? expanded_ : qMax(naturalSize(), currentSize());
    if (!collapsed_ && anim_->state() != QAbstractAnimation::Running) {
        constraintTo(target);
        setMouseTransparent(false);
        return;
    }
    collapsed_ = false;
    setMouseTransparent(false);   // 一恢复就允许点击（不等动画结束）
    anim_->stop();
    anim_->setDuration(animate ? 150 : 0);
    anim_->setStartValue(currentSize());
    anim_->setEndValue(target);
    anim_->start();
}
