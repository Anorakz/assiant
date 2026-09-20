// ============================================================================
//  gui/tests/test_onboard_ctl.cpp — onboard 控制的纯逻辑单测（T6）
//
//  真正调 DBus 的部分不在这里测（那要有桌面会话总线）；这里测**决策规则**：
//  哪种输入源需要软键盘。DBus 那一侧由验收脚本用 gdbus 查 Visible 属性证明。
// ============================================================================
#include <QtTest/QtTest>

#include "services/onboard_ctl.h"

class TestOnboardCtl : public QObject {
    Q_OBJECT

private slots:
    void onlyKeyboardWantsOnboard();
    void unknownSourceDoesNotWantOnboard();
    void probeDoesNotCrashWithoutOnboard();
};

void TestOnboardCtl::onlyKeyboardWantsOnboard()
{
    QVERIFY(OnboardCtl::wantsOnboard(QStringLiteral("keyboard")));
    // PC（主机键盘）与命令行都不需要屏上软键盘
    QVERIFY(!OnboardCtl::wantsOnboard(QStringLiteral("pc")));
    QVERIFY(!OnboardCtl::wantsOnboard(QStringLiteral("terminal")));
}

void TestOnboardCtl::unknownSourceDoesNotWantOnboard()
{
    // 配置写错时**不要**乱弹键盘
    QVERIFY(!OnboardCtl::wantsOnboard(QString()));
    QVERIFY(!OnboardCtl::wantsOnboard(QStringLiteral("Keyboard")));   // 大小写敏感
    QVERIFY(!OnboardCtl::wantsOnboard(QStringLiteral("nonsense")));
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
