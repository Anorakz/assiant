// ============================================================================
//  gui/src/ui/top_bar.h — 上区域·通栏顶栏（T4，按验收意见调整）
//
//  方案 §3 F1。**只显示一个三态连接指示**：
//      已连接（绿） / 重连中（黄） / 未连接（灰）
//  它把「Agent 链路」与「串流主机」两件事合成一个结论；两边的**细节写日志**，
//  不再占界面（`[ui] 连接: … (agent=…, 主机=…)`）。
//
//  另外：不再显示 debug 字母标记，debug 只影响日志详细程度（see MainWindow）。
// ============================================================================
#pragma once

#include <QFrame>
#include <QString>

class QLabel;
class QTimer;

class TopBar : public QFrame {
    Q_OBJECT

public:
    /// 三态连接结论（把 agent 链路 + 串流主机合成一个）
    enum class LinkState {
        Disconnected,   ///< 未连接（还没连上过）
        Reconnecting,   ///< 重连中（Agent 断了在重连，或已连上 Agent 但主机未就绪）
        Connected,      ///< 已连接（Agent 在 + 主机在）
    };

    explicit TopBar(QWidget* parent = nullptr);

    void setLinkState(LinkState state);

    /// 协议 §3 的模式值（SLEEP/IDLE/STUDY/GAME）；传空字符串 = 未知。
    void setMode(const QString& mode);

    /// 直接设定时钟文本（截图/单测用；不调则每秒自动走）。
    void setClockText(const QString& text);

private:
    void refreshClock();

    QLabel* linkDot_ = nullptr;
    QLabel* linkText_ = nullptr;
    QLabel* modeBadge_ = nullptr;
    QLabel* clock_ = nullptr;
    QTimer* clockTimer_ = nullptr;
    LinkState linkState_ = LinkState::Disconnected;
};
