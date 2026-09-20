// ============================================================================
//  gui/src/ui/chat_panel.h — 右区域下段：对话区（消息列表 + 底部输入行）
//
//  方案 §3.1：**只保留一段消息流**（不再有"最近一次对话"单行），底部为
//  输入框 + [发送]（T6 会在同一行补"输入类型"小按钮）。
//
//  显示约定
//  ---------------------------------------------------------------------------
//    · 用户消息靠右（亮色气泡）；助手消息靠左（深色气泡）
//    · 最多保留 kMaxMessages 条，超出的从最旧开始删（板端内存有限）
//    · 发送后进入"思考中…"，收到 llm 才结束
//    · 连接不在"已连接"时：顶部灰条提示 + 发送按钮禁用
// ============================================================================
#pragma once

#include <QString>
#include <QVector>
#include <QWidget>

class QLabel;
class QLineEdit;
class QMenu;
class QPushButton;
class QScrollArea;
class QToolButton;
class QVBoxLayout;

class ChatPanel : public QWidget {
    Q_OBJECT

public:
    static constexpr int kMaxMessages = 200;

    explicit ChatPanel(QWidget* parent = nullptr);

    void appendUser(const QString& text);
    void appendAssistant(const QString& text);
    /// 系统提示（例如"消息没发出去：未连接"）：居中灰字，不算一条对话
    void appendSystem(const QString& text);
    /// 输入源提示（常驻一行，空字符串=隐藏）——不占消息流，避免切来切去刷屏
    void setInputHint(const QString& text);

    void setThinking(bool on);
    /// 连接是否可用（false = 顶部显示提示条 + 禁止发送）
    void setLinkUp(bool up);
    void setInputEnabled(bool on);

    int messageCount() const { return rows_.size(); }

    // 供单测 / 验收演示使用：直接拿到真实控件
    QLineEdit* input() const { return input_; }
    QPushButton* sendButton() const { return send_; }
    QToolButton* inputTypeButton() const { return typeButton_; }
    void clear();

    /// 当前输入源：pc | terminal | keyboard（T6：输入框右侧小按钮三选）
    QString inputType() const { return inputType_; }
    void setInputType(const QString& type);

signals:
    /// 用户点了发送（或按回车）；非空文本才发
    void sendRequested(const QString& text);

    /// 用户切换了输入源（pc / terminal / keyboard）
    void inputTypeChanged(const QString& type);

private:
    void doSend();
    void appendBubble(const QString& text, bool fromUser);
    void scrollToBottom();

    QLabel* banner_ = nullptr;
    QLabel* hint_ = nullptr;
    QScrollArea* scroll_ = nullptr;
    QWidget* list_ = nullptr;
    QVBoxLayout* listBox_ = nullptr;
    QLabel* thinking_ = nullptr;
    QLineEdit* input_ = nullptr;
    QToolButton* typeButton_ = nullptr;
    QMenu* typeMenu_ = nullptr;
    QPushButton* send_ = nullptr;
    QString inputType_ = QStringLiteral("keyboard");

    QVector<QWidget*> rows_;     ///< 消息行（含思考中那一行）
    bool linkUp_ = false;
    bool inputEnabled_ = true;
};
