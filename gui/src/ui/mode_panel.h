// ============================================================================
//  gui/src/ui/mode_panel.h — 右区域上段：模式切换按钮区（方案 §3 首页可变按钮）
//
//  按钮**可变**（T12-3 起）：
//      IDLE 或未知 → [睡眠][学习][游戏]                  三个入口
//      STUDY/GAME  → [退出当前模式][睡眠][另一个活跃模式]  三个（跨模式由 Agent 走路径）
//      SLEEP       → [退出当前模式]                      一个（睡眠屏不提供跳进游戏/学习）
//  规则本体在 core::ViewState::modeSwitchChoices()（可单测），这里只做摆放。
// ============================================================================
#pragma once

#include <QString>
#include <QVector>
#include <QWidget>

class QLabel;
class QPushButton;
class QVBoxLayout;

class ModePanel : public QWidget {
    Q_OBJECT

public:
    explicit ModePanel(QWidget* parent = nullptr);

    /// 按当前模式重建按钮（空字符串 = 还没收到 status）。
    void setMode(const QString& mode);
    QString mode() const { return mode_; }

    /// 当前摆出来的、指向 `target` 的那颗按钮；没有就返回 nullptr（大小写不敏感）。
    /// @note 验收用 `--mode-demo` 就靠它点**真实控件**（走信号 → 真发 switch_mode）。
    QPushButton* buttonFor(const QString& target) const;

signals:
    /// 用户点了某个**目标**模式：SLEEP / STUDY / GAME / IDLE（后者=退出当前模式）
    void modeRequested(const QString& value);

private:
    void rebuild();

    QVBoxLayout* box_ = nullptr;
    QLabel* title_ = nullptr;
    /// 按钮池：模式在 3 个和 1 个之间来回切时复用，**不用 deleteLater**。
    /// （deleteLater 的旧按钮在事件循环处理前仍是子控件，会让 findChildren/遍历看到幽灵按钮）
    QVector<QPushButton*> buttons_;
    QString mode_;
};
