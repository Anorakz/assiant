// ============================================================================
//  gui/tests/test_idle_watcher.cpp — 唤醒计时/状态 单测
//
//  覆盖方案 §4 的三条规则：
//    · 空闲达到 idle_ms → wentIdle（活动区域该折叠）
//    · 任何输入 → 重置计时；若刚从空闲醒来 → woke（区域该恢复）
//    · 关掉唤醒机制 = 立即恢复且不再计时
// ============================================================================
#include <QtTest/QtTest>

#include "core/idle_watcher.h"

using core::IdleWatcher;

class TestIdleWatcher : public QObject {
    Q_OBJECT

private slots:
    void goesIdleAfterTimeout();
    void activityResetsTimer();
    void wakesOnActivityAfterIdle();
    void disablingWakesAndStopsIdling();
    void changingIdleMsRearms();
};

void TestIdleWatcher::goesIdleAfterTimeout()
{
    IdleWatcher watcher;
    QSignalSpy idleSpy(&watcher, &IdleWatcher::wentIdle);
    watcher.setIdleMs(60);
    QVERIFY(!watcher.isIdle());

    QTRY_COMPARE_WITH_TIMEOUT(idleSpy.count(), 1, 1000);
    QVERIFY(watcher.isIdle());
    QCOMPARE(watcher.idleCount(), 1);
    QCOMPARE(watcher.wakeCount(), 0);
}

void TestIdleWatcher::activityResetsTimer()
{
    IdleWatcher watcher;
    QSignalSpy idleSpy(&watcher, &IdleWatcher::wentIdle);
    watcher.setIdleMs(150);

    for (int i = 0; i < 4; ++i) {
        QTest::qWait(40);
        watcher.notifyActivity();          // 每次都在超时前活动一下
    }
    QCOMPARE(idleSpy.count(), 0);
    QVERIFY(!watcher.isIdle());

    QTRY_COMPARE_WITH_TIMEOUT(idleSpy.count(), 1, 1500);
}

void TestIdleWatcher::wakesOnActivityAfterIdle()
{
    IdleWatcher watcher;
    QSignalSpy idleSpy(&watcher, &IdleWatcher::wentIdle);
    QSignalSpy wakeSpy(&watcher, &IdleWatcher::woke);
    watcher.setIdleMs(50);

    QTRY_COMPARE_WITH_TIMEOUT(idleSpy.count(), 1, 1000);
    QVERIFY(watcher.isIdle());

    watcher.notifyActivity();
    QCOMPARE(wakeSpy.count(), 1);
    QCOMPARE(watcher.wakeCount(), 1);
    QVERIFY(!watcher.isIdle());

    watcher.notifyActivity();              // 已经醒了，不该重复发 woke
    QCOMPARE(wakeSpy.count(), 1);
}

void TestIdleWatcher::disablingWakesAndStopsIdling()
{
    IdleWatcher watcher;
    QSignalSpy idleSpy(&watcher, &IdleWatcher::wentIdle);
    QSignalSpy wakeSpy(&watcher, &IdleWatcher::woke);
    watcher.setIdleMs(40);

    QTRY_COMPARE_WITH_TIMEOUT(idleSpy.count(), 1, 1000);
    watcher.setEnabled(false);
    QCOMPARE(wakeSpy.count(), 1);          // 关掉 = 立即恢复显示
    QVERIFY(!watcher.isIdle());

    QTest::qWait(200);
    QCOMPARE(idleSpy.count(), 1);          // 不再计时
    watcher.notifyActivity();
    QCOMPARE(wakeSpy.count(), 1);          // 也不再有醒来信号
}

void TestIdleWatcher::changingIdleMsRearms()
{
    IdleWatcher watcher;
    QSignalSpy idleSpy(&watcher, &IdleWatcher::wentIdle);
    watcher.setIdleMs(5000);
    watcher.setIdleMs(40);                 // 缩短后应按新值尽快触发

    QTRY_COMPARE_WITH_TIMEOUT(idleSpy.count(), 1, 1000);
    QCOMPARE(watcher.idleMs(), 40);
}

QTEST_GUILESS_MAIN(TestIdleWatcher)
#include "test_idle_watcher.moc"
