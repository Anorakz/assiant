// ============================================================================
//  gui/src/ui/bottom_bar.cpp — 下区域容器实现
// ============================================================================
#include "ui/bottom_bar.h"

#include "ui/music_bar.h"

#include <QHBoxLayout>
#include <QLabel>
#include <QStackedWidget>
#include <QVBoxLayout>

BottomBar::BottomBar(QWidget* parent)
    : QWidget(parent)
{
    auto* root = new QVBoxLayout(this);
    root->setContentsMargins(0, 0, 0, 0);
    root->setSpacing(0);

    stack_ = new QStackedWidget(this);
    stack_->setObjectName(QStringLiteral("BottomStack"));

    // ---- 页 0：音乐条 ----
    music_ = new MusicBar(stack_);
    stack_->addWidget(music_);

    // ---- 页 1：B站封面缩略图（T9 填充；现在只放一个"未接入"占位）----
    cover_ = new QWidget(stack_);
    cover_->setObjectName(QStringLiteral("CoverPage"));
    auto* coverBox = new QHBoxLayout(cover_);
    coverBox->setContentsMargins(0, 0, 0, 0);
    coverBox->setSpacing(10);
    auto* coverText = new QLabel(QStringLiteral("B站视频封面未接入"), cover_);
    coverText->setObjectName(QStringLiteral("MusicPlaceholder"));
    auto* coverTag = new QLabel(QStringLiteral("未接入"), cover_);
    coverTag->setObjectName(QStringLiteral("PlaceholderTag"));
    coverBox->addWidget(coverText);
    coverBox->addWidget(coverTag);
    coverBox->addStretch(1);
    stack_->addWidget(cover_);

    root->addWidget(stack_);
    setMode(QString());
}

void BottomBar::setMode(const QString& mode)
{
    // 只有游戏模式才换成封面；其它情况（含还没收到 status）都显示音乐条
    const bool game = (mode == QLatin1String("GAME"));
    stack_->setCurrentWidget(game ? cover_ : music_);
}

QString BottomBar::pageName() const
{
    return (stack_->currentWidget() == cover_) ? QStringLiteral("cover")
                                               : QStringLiteral("music");
}
