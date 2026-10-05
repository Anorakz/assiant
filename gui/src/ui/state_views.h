// ============================================================================
//  gui/src/ui/state_views.h — 异常/加载的两种"状态视图"（T15-16 G-D-1）
//
//  为什么要有它们
//  ---------------------------------------------------------------------------
//    · 板端是 kiosk：**没有弹窗习惯、没有鼠标** ✓，所以"出事了"必须是**页内**能看见的一行 ✓，
//      而不是一句日志、也不是系统弹窗 ✗（G-C-4 刚把最后那个系统模态拆掉 ✓）。
//    · 每个页面各自手写"一行红字 + 一颗重试"会漂 ✓（颜色/字号/留白各写一套 ✗）⇒
//      收敛成**一个组件** ✓：颜色字号走 QSS（`objectName` ✓），不写死 ✗。
//
//  两个组件的分工（D-2 的十行矩阵与 D-3 的三页骨架都用它们 ✓）
//  ---------------------------------------------------------------------------
//    · `StateBanner`：**信息 / 警告 / 错误** 三态 + **可选「重试」** ✓；
//      文本为空 ⇒ 整条**隐藏** ✓（调用方不用自己判断可见性 ✓，少一处漏判 ✗）。
//    · `Skeleton`：**加载中**的脉冲占位 ✓；⚠ 它的定时器必须**能停** ✓
//      （G-B-4 的教训：看不见的东西不该在后台烧唤醒 ✗）。
//
//  ⚠ 纪律（G-A-2 的守卫盯着 ✓）
//  ---------------------------------------------------------------------------
//    · 不写死颜色/字号 ✗ —— 颜色用 `theme::` 常量或 QSS ✓；本文件里**没有** `#RRGGBB`、
//      也没有 `font-size: Npx` ✓（R1/R2 会红 ✓）。
//    · 「重试」按钮 `setMinimumHeight(44)` ✓ —— G-C-3 的触摸规矩（R4 盯着 ✓）。
// ============================================================================
#pragma once

#include <QString>
#include <QWidget>

class QLabel;
class QPushButton;
class QTimer;

/// 页面内的一行状态（信息 / 警告 / 错误 + 可选重试）✓
class StateBanner : public QWidget {
    Q_OBJECT

public:
    enum Kind { Info, Warn, Error };

    explicit StateBanner(QWidget* parent = nullptr);

    /// 设置内容；`text` 为空 ⇒ 整条隐藏 ✓（调用方不必自己 hide ✓）
    void setState(Kind kind, const QString& text, bool retryable = false);
    /// 收起来（= 空文本的 Info ✓）
    void clear() { setState(Info, QString(), false); }

    // ---- 供单测 / 验收 ----
    Kind kind() const { return kind_; }
    QString text() const;
    /// 「重试」按钮此刻是不是可见的 ✓
    bool isRetryVisible() const;
    bool isEmpty() const { return text().isEmpty(); }

public slots:
    /// 验收辅助：**替用户点一下**「重试」✓（走真实控件 ⇒ 真的会发信号 ✓）
    void clickRetry();

signals:
    void retryClicked();

private:
    QLabel* text_ = nullptr;
    QPushButton* retry_ = nullptr;
    Kind kind_ = Info;
};

/// 加载中的脉冲骨架 ✓（灰条呼吸 ✓；`start()/stop()` 可控 ✓）
class Skeleton : public QWidget {
    Q_OBJECT

public:
    explicit Skeleton(QWidget* parent = nullptr);
    ~Skeleton() override;

    void start();                       ///< 开始脉冲（并显示 ✓）
    void stop();                        ///< 停下（⚠ 定时器一定要停 ✗）
    bool isRunning() const;

    // ---- 供单测 / 验收 ----
    /// 脉冲**次数** ✓（判据：跑起来会涨、停下来不涨 ✓）
    int pulses() const { return pulses_; }

protected:
    void paintEvent(QPaintEvent* event) override;

private:
    QTimer* timer_ = nullptr;
    int phase_ = 0;
    int pulses_ = 0;
};
