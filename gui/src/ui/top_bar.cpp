// ============================================================================
//  gui/src/ui/top_bar.cpp — 顶栏实现（按 T4 验收意见调整）
//
//  验收要求：
//    · 模式徽标别太大，**上下各留一点间隙**
//    · 只显示三态连接（已连接/重连中/未连接），agent/主机细节进日志
//    · 取消 debug 字母标记
//
//  配色方案 §8：底 #2B2D31 / 次文字 #9AA0A6 / 主文字 #E6E6E6；
//  状态色：正常 #4ADE80、警告 #F59E0B、未连接 #6B7280；
//  SLEEP #6B7280 / IDLE #7AA2F7 / STUDY #4ADE80 / GAME #F59E0B。
// ============================================================================
#include "ui/top_bar.h"
#include "ui/theme.h"    // T15-16 G-A-1b：视觉常量（唯一来源）

#include <QHBoxLayout>
#include <QLabel>
#include <QTime>
#include <QTimer>

namespace {

const char* const kBars = theme::kBg;   ///< 徽标上的文字色（深色，压在亮底上）

QString modeColor(const QString& mode)
{
    if (mode == QLatin1String("SLEEP")) {
        return QLatin1String(theme::kOffline);
    }
    if (mode == QLatin1String("IDLE")) {
        return QLatin1String(theme::kAccent);
    }
    if (mode == QLatin1String("STUDY")) {
        return QLatin1String(theme::kOk);
    }
    if (mode == QLatin1String("GAME")) {
        return QLatin1String(theme::kWarn);
    }
    return QLatin1String(theme::kDivider);
}

QString modeLabel(const QString& mode)
{
    if (mode == QLatin1String("SLEEP")) {
        return QStringLiteral("睡眠");
    }
    if (mode == QLatin1String("IDLE")) {
        return QStringLiteral("空闲");
    }
    if (mode == QLatin1String("STUDY")) {
        return QStringLiteral("学习");
    }
    if (mode == QLatin1String("GAME")) {
        return QStringLiteral("游戏");
    }
    return QStringLiteral("——");
}

} // namespace

TopBar::TopBar(QWidget* parent)
    : QFrame(parent)
{
    setObjectName(QStringLiteral("TopBar"));
    setFixedHeight(72);

    auto* box = new QHBoxLayout(this);
    // 上下各 14px：徽标与文字都不贴边（验收：徽标太大、上下要留间隙）
    box->setContentsMargins(24, 14, 24, 14);
    box->setSpacing(18);

    linkDot_ = new QLabel(QStringLiteral("●"), this);
    linkDot_->setObjectName(QStringLiteral("TopBarText"));
    linkText_ = new QLabel(this);
    linkText_->setObjectName(QStringLiteral("TopBarText"));

    modeBadge_ = new QLabel(this);
    modeBadge_->setObjectName(QStringLiteral("ModeBadge"));
    modeBadge_->setAlignment(Qt::AlignCenter);
    modeBadge_->setFixedHeight(36);          // 上下各留 ~4px 余量，不顶到栏边
    modeBadge_->setMinimumWidth(96);

    clock_ = new QLabel(this);
    clock_->setObjectName(QStringLiteral("TopBarClock"));

    box->addWidget(linkDot_);
    box->addWidget(linkText_);
    box->addSpacing(6);
    box->addWidget(modeBadge_);
    box->addStretch(1);                       // 通栏：两端分开
    box->addWidget(clock_);

    setLinkState(LinkState::Disconnected);
    setMode(QString());
    setClockText(QStringLiteral("--:--:--"));

    clockTimer_ = new QTimer(this);
    clockTimer_->setInterval(1000);
    connect(clockTimer_, &QTimer::timeout, this, [this]() { refreshClock(); });
    clockTimer_->start();
    refreshClock();
}

void TopBar::refreshClock()
{
    clock_->setText(QTime::currentTime().toString(QStringLiteral("HH:mm:ss")));
}

void TopBar::setClockText(const QString& text)
{
    clock_->setText(text);
}

void TopBar::setLinkState(LinkState state)
{
    linkState_ = state;
    QString color;
    QString text;
    switch (state) {
    case LinkState::Connected:
        color = QLatin1String(theme::kOk);
        text = QStringLiteral("已连接");
        break;
    case LinkState::Reconnecting:
        color = QLatin1String(theme::kWarn);
        text = QStringLiteral("重连中");
        break;
    case LinkState::Disconnected:
    default:
        color = QLatin1String(theme::kOffline);
        text = QStringLiteral("未连接");
        break;
    }
    linkDot_->setStyleSheet(QStringLiteral("color:%1; background:transparent;").arg(color));
    linkText_->setText(text);
}

void TopBar::setMode(const QString& mode)
{
    modeBadge_->setText(modeLabel(mode));
    modeBadge_->setStyleSheet(
        QStringLiteral("background:%1; color:%2; border-radius:6px;"
                       "padding:0 12px; font-size:%3px; font-weight:bold;")
            .arg(modeColor(mode), QLatin1String(kBars), QString::number(theme::kFontMd)));
}
