// ============================================================================
//  gui/src/ui/region_host.h — 边缘区域容器（负责隐藏/唤出的动画与命中测试）
//
//  方案 §4：四个区域（上/下/左/右）各包一个 RegionHost。
//    · active（活动）：空闲后折叠（滑出）并在折叠期**不拦截点击**
//    · locked（锁定）：常显，永不折叠
//  折叠轴：上/下 = 高度，左/右 = 宽度；动画 150ms。
// ============================================================================
#pragma once

#include <QMargins>
#include <QWidget>

class QPropertyAnimation;

class RegionHost : public QWidget {
    Q_OBJECT

public:
    enum class Edge { Top, Bottom, Left, Right };

    RegionHost(Edge edge, QWidget* content, QWidget* parent = nullptr);

    /// active=true 表示"空闲后隐藏"；false = 锁定常显（会立即展开）。
    void setActive(bool active);
    bool isActive() const { return active_; }

    void showRegion(bool animate = true);
    void hideRegion(bool animate = true);
    bool isCollapsed() const { return collapsed_; }

    /// 区域内容与宿主边缘之间的内边距（左区域做成圆角卡片时用；默认 0）。
    /// 会影响展开尺寸：naturalSize() = 内容 sizeHint + 内边距。
    void setInnerMargins(const QMargins& margins);
    QMargins innerMargins() const { return innerMargins_; }

private:
    bool vertical() const { return edge_ == Edge::Top || edge_ == Edge::Bottom; }
    int naturalSize() const;
    int currentSize() const;
    void constraintTo(int value);
    void setMouseTransparent(bool on);

    Edge edge_;
    QWidget* content_ = nullptr;
    QMargins innerMargins_;
    bool active_ = false;
    bool collapsed_ = false;
    int expanded_ = 0;
    QPropertyAnimation* anim_ = nullptr;
};
