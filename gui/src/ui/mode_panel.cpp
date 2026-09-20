// ============================================================================
//  gui/src/ui/mode_panel.cpp — 模式切换按钮区实现
// ============================================================================
#include "ui/mode_panel.h"

#include <QLabel>
#include <QPushButton>
#include <QVariant>
#include <QVBoxLayout>

#include "core/view_state.h"

namespace {

QString labelOf(const QString& target)
{
    if (target == QLatin1String("SLEEP")) {
        return QStringLiteral("睡眠");
    }
    if (target == QLatin1String("STUDY")) {
        return QStringLiteral("学习");
    }
    if (target == QLatin1String("GAME")) {
        return QStringLiteral("游戏");
    }
    if (target == QLatin1String("IDLE")) {
        return QStringLiteral("退出当前模式");
    }
    return target;
}

} // namespace

ModePanel::ModePanel(QWidget* parent)
    : QWidget(parent)
{
    box_ = new QVBoxLayout(this);
    box_->setContentsMargins(0, 0, 0, 0);
    box_->setSpacing(6);    // 按钮之间别拉太开（验收：区域太高、按钮太大）
    box_->addStretch(1);    // 末尾留一个伸缩：标题与按钮都插在它前面（从上往下堆）

    // ⚠ 直接 rebuild()，不要走 setMode(QString())：mode_ 初值就是空串，
    //   会被 setMode 的"值没变就跳过"提前返回，结果一个按钮都不建（单测抓到的）。
    rebuild();
}

void ModePanel::setMode(const QString& mode)
{
    if (mode_ == mode) {
        return;             // 模式没变就别重建（免得把按钮按下的反馈打断）
    }
    mode_ = mode;
    rebuild();
}

void ModePanel::rebuild()
{
    const QStringList choices = core::ViewState::modeSwitchChoices(mode_);
    const bool exitOnly = (choices.size() == 1);

    if (title_ == nullptr) {
        title_ = new QLabel(this);
        title_->setObjectName(QStringLiteral("AreaTitle"));
        box_->insertWidget(box_->count() - 1, title_);   // 插在末尾伸缩之前
    }
    title_->setText(exitOnly ? QStringLiteral("当前模式") : QStringLiteral("切换模式"));

    // 按钮池：需要几个建几个，多余的隐藏。切模式来回切时按钮被复用，
    // 不会像 deleteLater 那样留下"待删除但仍是子控件"的幽灵按钮。
    while (buttons_.size() < choices.size()) {
        auto* button = new QPushButton(this);
        button->setObjectName(QStringLiteral("ModeButton"));
        button->setCursor(Qt::PointingHandCursor);
        connect(button, &QPushButton::clicked, this, [this, button]() {
            emit modeRequested(button->property("modeTarget").toString());
        });
        buttons_.append(button);
        box_->insertWidget(box_->count() - 1, button);   // 插在末尾伸缩之前
    }

    for (int i = 0; i < buttons_.size(); ++i) {
        QPushButton* button = buttons_.at(i);
        if (i >= choices.size()) {
            button->setVisible(false);          // 隐藏而不是删除（池里留着复用）
            continue;
        }
        const QString target = choices.at(i);
        button->setProperty("modeTarget", target);
        button->setText(labelOf(target));
        // 用 setFixedHeight（而不是 setMinimumHeight）：布局的 sizeHint 也按它算，
        // 面板高度才能"跟着内容走"（验收：区域太高、按钮太大）
        button->setFixedHeight(exitOnly ? 64 : 44);
        button->setVisible(true);
    }
}
