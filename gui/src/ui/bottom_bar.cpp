// ============================================================================
//  gui/src/ui/bottom_bar.cpp — 下区域容器实现
// ============================================================================
#include "ui/bottom_bar.h"

#include "ui/bilibili_cover.h"
#include "ui/music_bar.h"

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

    // ---- 页 1：B 站封面（T11-7：真控件，不再是"未接入"占位）----
    cover_ = new BilibiliCover(stack_);
    stack_->addWidget(cover_);

    root->addWidget(stack_);
    setMode(QString());
}

QWidget* BottomBar::coverPage() const
{
    return cover_;
}

void BottomBar::setMode(const QString& mode)
{
    // 只有游戏模式才换成封面；其它情况（含还没收到 status）都显示音乐条
    const bool game = (mode == QLatin1String("GAME"));
    QWidget* page = game ? static_cast<QWidget*>(cover_) : static_cast<QWidget*>(music_);
    stack_->setCurrentWidget(page);
}

QString BottomBar::pageName() const
{
    return (stack_->currentWidget() == cover_) ? QStringLiteral("cover")
                                               : QStringLiteral("music");
}
