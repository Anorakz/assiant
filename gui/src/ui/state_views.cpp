// ============================================================================
//  gui/src/ui/state_views.cpp — 状态视图的实现（T15-16 G-D-1）
//
//  ⚠ 本文件**刻意不写死颜色/字号** ✓：
//    · 文字颜色/背景/圆角由 QSS 按 `objectName` + `kind` 属性给 ✓（见 base_style.h ✓）；
//    · 脉冲条的两种深浅直接用 `theme::` 常量算 ✓（不出现 `#RRGGBB` ✓，R1 不会红 ✓）。
// ============================================================================
#include "ui/state_views.h"
#include "ui/theme.h"       // T15-16：视觉常量（唯一来源 ✓）

#include <QHBoxLayout>
#include <QLabel>
#include <QPainter>
#include <QPushButton>
#include <QStyle>
#include <QTimer>
#include <QVariant>     // ⚠ `setProperty` 要它：没有这个 include，QString→QVariant 的隐式转换找不到 ✗（编译期实测 ✓）

// ---------------------------------------------------------------------------
//  StateBanner
// ---------------------------------------------------------------------------
StateBanner::StateBanner(QWidget* parent)
    : QWidget(parent)
{
    setObjectName(QStringLiteral("StateBanner"));

    auto* box = new QHBoxLayout(this);
    box->setContentsMargins(12, 8, 12, 8);
    box->setSpacing(8);

    text_ = new QLabel(this);
    text_->setObjectName(QStringLiteral("StateBannerText"));
    text_->setWordWrap(true);
    box->addWidget(text_, 1);

    retry_ = new QPushButton(QStringLiteral("重试"), this);
    retry_->setObjectName(QStringLiteral("StateBannerRetry"));
    retry_->setMinimumHeight(44);      // G-C-3：触摸目标 ≥44 ✓（R4 盯着 ✓）
    retry_->hide();
    box->addWidget(retry_, 0);

    connect(retry_, &QPushButton::clicked, this, &StateBanner::retryClicked);

    hide();                            // 默认什么都不说 ✓
}

void StateBanner::setState(Kind kind, const QString& text, bool retryable)
{
    kind_ = kind;
    text_->setText(text);

    const bool hasText = !text.isEmpty();
    retry_->setVisible(hasText && retryable);
    setVisible(hasText);

    // QSS 靠这个属性区分 info / warn / error ✓（改完要让样式重算 ✓，否则颜色不跟着变 ✗）
    // ⚠ 三目里的 `const char*` 不能隐式转 `QVariant` ✗（编译期实测 ✓）⇒ 显式用 QString ✓
    const QString kindName = (kind == Error) ? QStringLiteral("error")
                                             : ((kind == Warn) ? QStringLiteral("warn")
                                                               : QStringLiteral("info"));
    setProperty("kind", kindName);
    if (style() != nullptr) {
        style()->unpolish(this);
        style()->polish(this);
    }
}

QString StateBanner::text() const
{
    return text_ != nullptr ? text_->text() : QString();
}

bool StateBanner::isRetryVisible() const
{
    return retry_ != nullptr && retry_->isVisible();
}

void StateBanner::clickRetry()
{
    // ⚠ 判据用 `!isHidden()` 而不是 `isVisible()` ✗ —— 后者在没有 parent/没 show 过的控件上
    //   恒为 false ✓（单测里就点不动了 ✗）；"有没有被我们自己藏起来"才是这里要问的 ✓
    //   （`setState()` 里就是用 `setVisible()` 控的 ✓）。
    if (retry_ != nullptr && !retry_->isHidden()) {
        retry_->click();               // 走真实控件 ✓（不是直接 emit ✗）
    }
}

// ---------------------------------------------------------------------------
//  Skeleton
// ---------------------------------------------------------------------------
Skeleton::Skeleton(QWidget* parent)
    : QWidget(parent)
{
    setObjectName(QStringLiteral("Skeleton"));
    setMinimumHeight(12);
    setSizePolicy(QSizePolicy::Expanding, QSizePolicy::Fixed);

    timer_ = new QTimer(this);
    timer_->setInterval(120);
    connect(timer_, &QTimer::timeout, this, [this]() {
        ++phase_;
        ++pulses_;                     // 判据：跑起来会涨 ✓
        update();
    });
}

Skeleton::~Skeleton()
{
    if (timer_ != nullptr) {
        timer_->stop();                // ⚠ 析构也要停 ✓（G-B-4 的教训 ✓）
    }
}

void Skeleton::start()
{
    show();
    if (timer_ != nullptr && !timer_->isActive()) {
        timer_->start();
    }
}

void Skeleton::stop()
{
    if (timer_ != nullptr) {
        timer_->stop();                // ⚠ 停了就不许再醒 ✗（G-B-4 的教训：不可见 = 别烧唤醒 ✓）
    }
}

bool Skeleton::isRunning() const
{
    return timer_ != nullptr && timer_->isActive();
}

void Skeleton::paintEvent(QPaintEvent* event)
{
    Q_UNUSED(event);
    QPainter painter(this);
    painter.setRenderHint(QPainter::Antialiasing, true);

    // 两档深浅交替 = "呼吸" ✓（只用 theme:: 常量算 ✓，不出现字面色值 ✓）
    QColor base(QLatin1String(theme::kPanelHover));
    base.setAlphaF((phase_ % 2) == 0 ? 0.35 : 0.65);
    painter.setBrush(base);
    painter.setPen(Qt::NoPen);
    painter.drawRoundedRect(rect().adjusted(0, 0, -1, -1), 6, 6);
}
