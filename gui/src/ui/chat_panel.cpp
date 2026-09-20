// ============================================================================
//  gui/src/ui/chat_panel.cpp — 对话区实现
// ============================================================================
#include "ui/chat_panel.h"

#include <QAction>
#include <QHBoxLayout>
#include <QLabel>
#include <QLineEdit>
#include <QMenu>
#include <QPushButton>
#include <QScrollArea>
#include <QScrollBar>
#include <QToolButton>
#include <QVBoxLayout>

namespace {

/// 一行消息：左/右对齐靠"两侧伸缩"实现（不用 QListWidget 的自定义 item，省一层）
QWidget* makeBubbleRow(QWidget* parent, QLabel* bubble, bool fromUser)
{
    auto* row = new QWidget(parent);
    auto* box = new QHBoxLayout(row);
    box->setContentsMargins(0, 0, 0, 0);
    box->setSpacing(0);
    if (fromUser) {
        box->addStretch(1);
        box->addWidget(bubble);
    } else {
        box->addWidget(bubble);
        box->addStretch(1);
    }
    return row;
}

} // namespace

ChatPanel::ChatPanel(QWidget* parent)
    : QWidget(parent)
{
    auto* root = new QVBoxLayout(this);
    root->setContentsMargins(0, 0, 0, 0);
    root->setSpacing(8);

    banner_ = new QLabel(QStringLiteral("未连接：消息发不出去"), this);
    banner_->setObjectName(QStringLiteral("LinkBanner"));
    banner_->setVisible(false);
    root->addWidget(banner_);

    hint_ = new QLabel(this);
    hint_->setObjectName(QStringLiteral("ChatSystem"));
    hint_->setWordWrap(true);
    hint_->setVisible(false);
    root->addWidget(hint_);

    scroll_ = new QScrollArea(this);
    scroll_->setObjectName(QStringLiteral("ChatScroll"));
    scroll_->setWidgetResizable(true);
    scroll_->setFrameShape(QFrame::NoFrame);
    scroll_->setHorizontalScrollBarPolicy(Qt::ScrollBarAlwaysOff);

    list_ = new QWidget(scroll_);
    list_->setObjectName(QStringLiteral("ChatList"));
    listBox_ = new QVBoxLayout(list_);
    listBox_->setContentsMargins(4, 4, 4, 4);
    listBox_->setSpacing(10);
    listBox_->addStretch(1);          // 让消息从上往下堆（新消息追加在 stretch 之前）
    scroll_->setWidget(list_);
    root->addWidget(scroll_, 1);

    thinking_ = new QLabel(QStringLiteral("思考中…"), this);
    thinking_->setObjectName(QStringLiteral("ChatSystem"));
    thinking_->setVisible(false);
    root->addWidget(thinking_);

    // ---- 底部输入行 ----
    auto* inputRow = new QWidget(this);
    auto* inputBox = new QHBoxLayout(inputRow);
    inputBox->setContentsMargins(0, 0, 0, 0);
    inputBox->setSpacing(8);

    input_ = new QLineEdit(inputRow);
    input_->setObjectName(QStringLiteral("ChatInput"));
    input_->setPlaceholderText(QStringLiteral("说点什么…"));
    input_->setMinimumHeight(48);
    connect(input_, &QLineEdit::returnPressed, this, [this]() { doSend(); });

    send_ = new QPushButton(QStringLiteral("发送"), inputRow);
    send_->setObjectName(QStringLiteral("ChatSend"));
    send_->setMinimumHeight(48);
    send_->setMinimumWidth(96);
    connect(send_, &QPushButton::clicked, this, [this]() { doSend(); });

    // ---- 输入源小按钮（T6）：PC / 命令行 / 键盘 三选 ----
    typeButton_ = new QToolButton(inputRow);
    typeButton_->setObjectName(QStringLiteral("InputTypeButton"));
    typeButton_->setPopupMode(QToolButton::InstantPopup);
    typeButton_->setMinimumHeight(48);
    typeButton_->setMinimumWidth(92);
    typeButton_->setToolTip(QStringLiteral("选择输入源"));
    typeMenu_ = new QMenu(typeButton_);
    const struct {
        const char* key;
        const char* label;
    } kTypes[] = {
        {"keyboard", "键盘"},
        {"pc", "PC"},
        {"terminal", "命令行"},
    };
    for (const auto& type : kTypes) {
        QAction* action = typeMenu_->addAction(QString::fromUtf8(type.label));
        action->setCheckable(true);
        action->setData(QString::fromUtf8(type.key));
        connect(action, &QAction::triggered, this,
                [this, action]() { setInputType(action->data().toString()); });
    }
    typeButton_->setMenu(typeMenu_);

    inputBox->addWidget(input_, 1);
    inputBox->addWidget(typeButton_);
    inputBox->addWidget(send_);
    root->addWidget(inputRow);

    setInputType(inputType_);     // 初始按 gui.yaml 的默认值显示
    setLinkUp(false);
    setInputEnabled(true);
}

void ChatPanel::doSend()
{
    const QString text = input_->text().trimmed();
    if (text.isEmpty()) {
        return;
    }
    if (!linkUp_) {
        appendSystem(QStringLiteral("没发出去：与 Agent 未连接"));
        return;
    }
    input_->clear();
    emit sendRequested(text);
}

void ChatPanel::appendBubble(const QString& text, bool fromUser)
{
    auto* bubble = new QLabel(text, list_);
    bubble->setObjectName(fromUser ? QStringLiteral("ChatBubbleUser")
                                   : QStringLiteral("ChatBubbleAssistant"));
    bubble->setWordWrap(true);
    bubble->setTextInteractionFlags(Qt::TextSelectableByMouse);
    // 气泡最宽约占对话区的 85%，避免长文本把行撑爆
    bubble->setMaximumWidth(280);

    QWidget* row = makeBubbleRow(list_, bubble, fromUser);
    // 插到 stretch 之前（最后一项是 stretch）
    listBox_->insertWidget(listBox_->count() - 1, row);
    rows_.append(row);

    while (rows_.size() > kMaxMessages) {
        QWidget* oldest = rows_.takeFirst();
        listBox_->removeWidget(oldest);
        oldest->deleteLater();
    }
    scrollToBottom();
}

void ChatPanel::appendUser(const QString& text)
{
    appendBubble(text, true);
}

void ChatPanel::appendAssistant(const QString& text)
{
    appendBubble(text, false);
}

void ChatPanel::appendSystem(const QString& text)
{
    auto* label = new QLabel(text, list_);
    label->setObjectName(QStringLiteral("ChatSystem"));
    label->setWordWrap(true);
    listBox_->insertWidget(listBox_->count() - 1, label);
    scrollToBottom();
}

void ChatPanel::scrollToBottom()
{
    // 布局还没算完时直接设最大值会跳不到位，排到事件循环下一轮
    QScrollBar* bar = scroll_->verticalScrollBar();
    QMetaObject::invokeMethod(
        this,
        [bar]() {
            if (bar != nullptr) {
                bar->setValue(bar->maximum());
            }
        },
        Qt::QueuedConnection);
}

void ChatPanel::setInputType(const QString& type)
{
    inputType_ = type;
    const QList<QAction*> actions = typeMenu_->actions();
    QString label = type;
    for (QAction* action : actions) {
        const bool hit = (action->data().toString() == type);
        action->setChecked(hit);
        if (hit) {
            label = action->text();
        }
    }
    typeButton_->setText(label + QStringLiteral(" ▾"));
    emit inputTypeChanged(type);
}

void ChatPanel::setInputHint(const QString& text)
{
    hint_->setText(text);
    hint_->setVisible(!text.isEmpty());
}

void ChatPanel::setThinking(bool on)
{
    thinking_->setVisible(on);
}

void ChatPanel::setLinkUp(bool up)
{
    linkUp_ = up;
    banner_->setVisible(!up);
    send_->setEnabled(up && inputEnabled_);
}

void ChatPanel::setInputEnabled(bool on)
{
    inputEnabled_ = on;
    input_->setEnabled(on);
    send_->setEnabled(linkUp_ && on);
}

void ChatPanel::clear()
{
    for (QWidget* row : rows_) {
        listBox_->removeWidget(row);
        row->deleteLater();
    }
    rows_.clear();
}
