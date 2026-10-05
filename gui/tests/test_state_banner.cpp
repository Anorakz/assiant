// ============================================================================
//  gui/tests/test_state_banner.cpp — 状态视图守卫（T15-16 G-D-1）
//
//  判据（机械、可复跑 ✓）
//  ---------------------------------------------------------------------------
//    · `StateBanner`：说了什么就显示什么 ✓；**空文本 ⇒ 整条藏起来** ✓；
//      「重试」按钮**只有要求时才在** ✓，而且**只有真的点它才发信号** ✓
//      （`clickRetry()` 走真实控件 ✓，不是直接 emit ✗）。
//    · `Skeleton`：跑起来**脉冲计数会涨** ✓、**停下来就不许再涨** ✓（一正一反 ✓；
//      这一条正是 G-B-4 的教训：不可见的东西不该在后台烧唤醒 ✗）。
//
//  ⚠ 可见性用 `isHidden()` 判 ✗ 不用 `isVisible()` ——
//    裸控件（没有 parent、没 show 过）的 `isVisible()` 恒为 false ✓，
//    而"我们有没有显式把它藏起来"才是这里问的问题 ✓。
// ============================================================================
#include <QSignalSpy>
#include <QtTest/QtTest>

#include "ui/state_views.h"

class TestStateBanner : public QObject {
    Q_OBJECT

private slots:
    void bannerShowsWhatItIsTold()
    {
        StateBanner banner;
        banner.setState(StateBanner::Warn, QStringLiteral("日程读不到"));
        QCOMPARE(banner.text(), QStringLiteral("日程读不到"));
        QCOMPARE(banner.kind(), StateBanner::Warn);
        QVERIFY(!banner.isRetryVisible());
        QVERIFY2(!banner.isHidden(), "有文本就该露出来 ✓");

        banner.setState(StateBanner::Error, QStringLiteral("IPC 断了"), true);
        QCOMPARE(banner.kind(), StateBanner::Error);
        QCOMPARE(banner.text(), QStringLiteral("IPC 断了"));
        QVERIFY(banner.isRetryVisible());
    }

    void emptyTextHidesTheWholeBanner()
    {
        StateBanner banner;
        banner.setState(StateBanner::Error, QStringLiteral("出事了"), true);
        QVERIFY(!banner.isHidden());

        banner.setState(StateBanner::Info, QString());     // 空文本 ✓
        QVERIFY2(banner.isHidden(), "空文本要整条藏起来 ✓（调用方不必自己判断 ✓）");
        QVERIFY(!banner.isRetryVisible());
        QVERIFY(banner.isEmpty());

        banner.clear();
        QVERIFY(banner.isHidden());
    }

    void theRetrySignalOnlyComesFromTheRealButton()
    {
        StateBanner banner;
        QSignalSpy spy(&banner, &StateBanner::retryClicked);

        banner.setState(StateBanner::Error, QStringLiteral("失败"), false);   // 不给重试
        banner.clickRetry();
        QCOMPARE(spy.count(), 0);          // 没有按钮 ⇒ 发不出去 ✓

        banner.setState(StateBanner::Error, QStringLiteral("失败"), true);
        QVERIFY(banner.isRetryVisible());
        banner.clickRetry();
        QCOMPARE(spy.count(), 1);          // 走真实控件 ⇒ 发了 ✓

        banner.setState(StateBanner::Info, QString());                        // 收起来
        banner.clickRetry();
        QCOMPARE(spy.count(), 1);          // 收起来之后点不动 ✓
    }

    void skeletonPulsesWhileRunningAndStopsWhenStopped()
    {
        Skeleton bar;
        bar.start();
        QVERIFY(bar.isRunning());

        QTest::qWait(400);                 // 间隔 120ms ⇒ 这几百毫秒里必涨 ✓
        const int during = bar.pulses();
        QVERIFY2(during >= 1, "跑起来却不涨 —— 脉冲计数没接上 ✗");

        bar.stop();
        QVERIFY(!bar.isRunning());
        QTest::qWait(400);
        QCOMPARE(bar.pulses(), during);    // ⚠ 停了就不许再涨 ✓（牙齿打这里 ✓）
    }

    /// 防"0 == 0"空断言：计数器本身必须真的会动 ✓
    void thePulseCounterActuallyMoves()
    {
        Skeleton bar;
        QCOMPARE(bar.pulses(), 0);
        bar.start();
        QTest::qWait(300);
        QVERIFY2(bar.pulses() >= 1, "脉冲计数根本没动 ✗");
        bar.stop();
        QVERIFY(!bar.isRunning());
    }
};

QTEST_MAIN(TestStateBanner)
#include "test_state_banner.moc"
