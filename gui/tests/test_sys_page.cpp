// ============================================================================
//  gui/tests/test_sys_page.cpp — 系统与网络页控件级测试（T10）
//
//  用**假根目录**喂数据，验界面上真的显示了这些值；再验读不到时显示"不可读"
//  而不是 0（方案 §7：读不到不许装成 0）。
// ============================================================================
#include <QDir>
#include "ui/state_views.h"   // T15-16 ②：hint 换成 StateBanner ⇒ 要完整类型 ✓
#include <QFile>
#include <QLabel>
#include <QPushButton>
#include <QTemporaryDir>
#include <QtTest/QtTest>

#include "core/system_stats.h"
#include "ui/sys_page.h"

class TestSysPage : public QObject {
    Q_OBJECT

private slots:
    /// T15-16 ②：读不到 /proc ⇒ 状态条出现并说清原因 ✓；**读到时要收掉** ✓（防横幅赖着不走 ✗）
    void hintBannerAppearsOnFailureAndClearsWhenHealthy();
    void showsFakeValues();
    void unreadableShowsPlaceholderNotZero();
    void watchdogIsPlaceholder();
};

namespace {

/// 造一棵假 /proc、/sys 并返回临时目录（调用方负责 QTemporaryDir 的生命周期）
void writeFake(QTemporaryDir& tmp)
{
    const QString root = tmp.path();
    const auto write = [&root](const QString& rel, const QString& content) {
        QDir().mkpath(QFileInfo(root + rel).absolutePath());
        QFile file(root + rel);
        file.open(QIODevice::WriteOnly);
        file.write(content.toUtf8());
        file.close();
    };
    write(QStringLiteral("/proc/stat"),
          QStringLiteral("cpu  100 0 50 850 0 0 0 0 0 0\n"));
    write(QStringLiteral("/proc/meminfo"),
          QStringLiteral("MemTotal: 1000000 kB\nMemAvailable: 400000 kB\n"));
    write(QStringLiteral("/sys/kernel/debug/rknpu/load"), QStringLiteral("NPU load: 42%\n"));
    write(QStringLiteral("/sys/class/devfreq/fde40000.npu/cur_freq"), QStringLiteral("600000000\n"));
    write(QStringLiteral("/sys/class/devfreq/dmc/cur_freq"), QStringLiteral("324000000\n"));
    write(QStringLiteral("/sys/class/thermal/thermal_zone0/type"), QStringLiteral("soc-thermal\n"));
    write(QStringLiteral("/sys/class/thermal/thermal_zone0/temp"), QStringLiteral("31250\n"));
}

} // namespace

void TestSysPage::showsFakeValues()
{
    QTemporaryDir tmp;
    QVERIFY(tmp.isValid());
    writeFake(tmp);

    SysPage page(core::SystemStats(tmp.path()));
    QCOMPARE(page.metricText(QStringLiteral("mem")), QStringLiteral("0.57 GB / 0.95 GB"));
    QCOMPARE(page.metricText(QStringLiteral("npu")), QStringLiteral("42 %"));
    QCOMPARE(page.metricText(QStringLiteral("freq_npu")), QStringLiteral("600 MHz"));
    QCOMPARE(page.metricText(QStringLiteral("freq_dmc")), QStringLiteral("324 MHz"));
    QCOMPARE(page.metricText(QStringLiteral("temp_soc")), QStringLiteral("31.3 ℃"));  // 31.25 四舍五入
    // CPU 第一次采样没有上一次做差 → 显示"采集中…"（不能瞎报 0%）
    QCOMPARE(page.metricText(QStringLiteral("cpu")), QStringLiteral("采集中…"));
    // 没有 devfreq 目录项 → 明确"不可读"
    QCOMPARE(page.metricText(QStringLiteral("freq_gpu")), QStringLiteral("不可读"));
}

void TestSysPage::unreadableShowsPlaceholderNotZero()
{
    QTemporaryDir tmp;
    QVERIFY(tmp.isValid());              // 空目录，什么都没有
    SysPage page(core::SystemStats(tmp.path()));
    QCOMPARE(page.metricText(QStringLiteral("cpu")), QStringLiteral("不可读"));
    QCOMPARE(page.metricText(QStringLiteral("mem")), QStringLiteral("不可读"));
    QCOMPARE(page.metricText(QStringLiteral("npu")), QStringLiteral("不可读"));
    QCOMPARE(page.metricText(QStringLiteral("temp_soc")), QStringLiteral("不可读"));
    // 关键来源读不到 → 页面上要有可读的提示
    QVERIFY(page.hintBanner() != nullptr);
    QVERIFY(page.hintBanner()->text().contains(QStringLiteral("/proc")));
}

void TestSysPage::watchdogIsPlaceholder()
{
    QTemporaryDir tmp;
    QVERIFY(tmp.isValid());
    SysPage page(core::SystemStats(tmp.path()));
    QVERIFY(page.watchdogButton() != nullptr);
    QVERIFY(page.watchdogButton()->text().contains(QStringLiteral("未启用")));

    page.triggerWatchdog();               // 协议没有看门狗命令 → 只给说明
    QVERIFY(page.noteText().contains(QStringLiteral("未接入")));
    QVERIFY(page.watchdogButton()->text().contains(QStringLiteral("未接入")));
    page.triggerWatchdog();
    QVERIFY(page.watchdogButton()->text().contains(QStringLiteral("未启用")));
}


/// T15-16 ②：三段断言 —— ① 读不到 ⇒ 有横幅且说了原因 ✓；② 给**读到**的页面**先塞一条旧错误** ✓
/// 再 `refresh()` ⇒ **旧横幅必须被收掉** ✓（这条直接冲着 `clear()` 去 ✓，也是牙齿打的位置 ✓）。
void TestSysPage::hintBannerAppearsOnFailureAndClearsWhenHealthy()
{
    QTemporaryDir empty;                        // 空目录 ⇒ 读不到 ✓
    QVERIFY(empty.isValid());
    SysPage bad(core::SystemStats(empty.path()));
    QVERIFY(bad.hintBanner() != nullptr);
    QVERIFY2(!bad.hintBanner()->isEmpty(), "读不到 /proc 却没出状态条 ✗");
    QVERIFY2(bad.hintBanner()->text().contains(QStringLiteral("读不到 /proc")),
             qPrintable(bad.hintBanner()->text()));

    QTemporaryDir good;                         // 有假 /proc ⇒ 读得到 ✓
    QVERIFY(good.isValid());
    writeFake(good);
    SysPage ok(core::SystemStats(good.path()));
    QVERIFY(ok.hintBanner() != nullptr);
    QVERIFY2(ok.hintBanner()->isEmpty(), "一开始就读得到，却已经有横幅了 ✗");

    // ⚠ 关键一条：**旧错误必须被收掉** ✗（否则用户会一直看到"读不到 /proc"✓ 而其实早就好了 ✓）
    ok.hintBanner()->setState(StateBanner::Error, QStringLiteral("上一次的错误"));
    ok.refresh();
    QVERIFY2(ok.hintBanner()->isEmpty(),
             qPrintable(QStringLiteral("refresh 成功了，上一次的错误横幅还留着 ✗：%1")
                            .arg(ok.hintBanner()->text())));
}

QTEST_MAIN(TestSysPage)
#include "test_sys_page.moc"
