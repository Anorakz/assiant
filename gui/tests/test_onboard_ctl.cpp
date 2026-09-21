// ============================================================================
//  gui/tests/test_onboard_ctl.cpp — onboard 控制的纯逻辑单测（T6 / S10）
//
//  真正调 DBus 的部分不在这里测（那要有桌面会话总线）；这里测**决策规则**：
//    · 哪种输入源需要软键盘（只有 keyboard）
//    · **什么时候**该弹（S10：只有输入框拿到焦点；启动/切输入源都不弹）
//  DBus 那一侧由真机截图 + 日志证明。
// ============================================================================
#include <QtTest/QtTest>

#include "services/onboard_ctl.h"

class TestOnboardCtl : public QObject {
    Q_OBJECT

private slots:
    void onlyKeyboardWantsOnboard();
    void unknownSourceDoesNotWantOnboard();
    void shouldShowOnlyWhenInputFocused();
    void shouldShowRespectsOnboardAuto();
    void probeDoesNotCrashWithoutOnboard();
};

void TestOnboardCtl::onlyKeyboardWantsOnboard()
{
    QVERIFY(OnboardCtl::wantsOnboard(QStringLiteral("keyboard")));
    // 命令行不需要屏上软键盘
    QVERIFY(!OnboardCtl::wantsOnboard(QStringLiteral("terminal")));
    // "pc"（宿主机键盘）这个输入源已在 Phase 6 C4 移除 —— 现在它落到"不认识"那条路,
    // 断言依然成立 (未知取值不弹键盘), 见下一个用例
}

void TestOnboardCtl::unknownSourceDoesNotWantOnboard()
{
    // 配置写错时**不要**乱弹键盘
    QVERIFY(!OnboardCtl::wantsOnboard(QString()));
    QVERIFY(!OnboardCtl::wantsOnboard(QStringLiteral("Keyboard")));   // 大小写敏感
    QVERIFY(!OnboardCtl::wantsOnboard(QStringLiteral("nonsense")));
}

void TestOnboardCtl::shouldShowOnlyWhenInputFocused()
{
    // S10 的核心政策：**只有输入框拿到焦点才弹**
    QVERIFY(OnboardCtl::shouldShow(true, QStringLiteral("keyboard"), true));
    // 启动/切输入源（还没点输入框）→ 不弹
    QVERIFY(!OnboardCtl::shouldShow(true, QStringLiteral("keyboard"), false));
    // 命令行输入源永远不弹
    QVERIFY(!OnboardCtl::shouldShow(true, QStringLiteral("terminal"), true));
    QVERIFY(!OnboardCtl::shouldShow(true, QStringLiteral("terminal"), false));
    // 配置写错（未知输入源）也不弹
    QVERIFY(!OnboardCtl::shouldShow(true, QStringLiteral("nonsense"), true));
}

void TestOnboardCtl::shouldShowRespectsOnboardAuto()
{
    // gui.onboard_auto = false：完全不碰 onboard，哪怕输入框有焦点
    QVERIFY(!OnboardCtl::shouldShow(false, QStringLiteral("keyboard"), true));
    QVERIFY(!OnboardCtl::shouldShow(false, QStringLiteral("keyboard"), false));
}

void TestOnboardCtl::probeDoesNotCrashWithoutOnboard()
{
    // 测试环境里可能没有 onboard 进程（或没有会话总线）：probe 必须返回 false 而不是崩
    OnboardCtl ctl;
    QString detail;
    const bool ok = ctl.probe(&detail);
    QVERIFY(!detail.isEmpty());               // 无论成功失败都要给出可读说明
    if (!ok) {
        QVERIFY(!ctl.available());
        QString error;
        QVERIFY(!ctl.show(&error));           // 不可用时 show 返回 false 并给原因
        QVERIFY(!error.isEmpty());
    }
}

QTEST_GUILESS_MAIN(TestOnboardCtl)
#include "test_onboard_ctl.moc"
