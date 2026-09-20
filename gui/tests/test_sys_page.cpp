// ============================================================================
//  gui/tests/test_sys_page.cpp — 系统与网络页控件级测试（T10）
//
//  用**假根目录**喂数据，验界面上真的显示了这些值；再验读不到时显示"不可读"
//  而不是 0（方案 §7：读不到不许装成 0）。
// ============================================================================
#include <QDir>
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
    QVERIFY(page.hintLabel() != nullptr);
    QVERIFY(page.hintLabel()->text().contains(QStringLiteral("/proc")));
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

QTEST_MAIN(TestSysPage)
#include "test_sys_page.moc"
