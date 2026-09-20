// ============================================================================
//  gui/tests/test_system_stats.cpp — 系统采集的解析与读取单测（T10）
//
//  真实来源的路径都在板端核过；这里用**假根目录**造一份 /proc、/sys，
//  既能验解析，又能验"读不到 → 明确失败"（不许把读不到当成 0）。
// ============================================================================
#include <QDir>
#include <QFile>
#include <QTemporaryDir>
#include <QtTest/QtTest>

#include "core/system_stats.h"

using core::CpuTimes;
using core::MemInfo;
using core::SystemStats;

class TestSystemStats : public QObject {
    Q_OBJECT

private slots:
    void parseCpuTimesTotalsOnly();
    void parseCpuTimesRejectsGarbage();
    void cpuUsageIsDeltaBased();
    void parseMemInfoRealShape();
    void parseNpuLoadRealOutput();
    void parseMilliCelsiusRealValue();
    void parseIpAddrOutputRealShape();
    void fakeRootReadsEverything();
    void fakeRootMissingFilesFailClearly();
};

void TestSystemStats::parseCpuTimesTotalsOnly()
{
    // 只认 "cpu " 总计行，cpu0/cpu1 必须被跳过
    const QString stat =
        QStringLiteral("cpu  100 2 30 900 10 0 0 0 0 0\n"
                       "cpu0 25 0 8 225 2 0 0 0 0 0\n"
                       "intr 12345\n");
    CpuTimes times;
    QVERIFY(SystemStats::parseCpuTimes(stat, &times));
    QCOMPARE(times.user, quint64(100));
    QCOMPARE(times.nice, quint64(2));
    QCOMPARE(times.system, quint64(30));
    QCOMPARE(times.idle, quint64(900));
    QCOMPARE(times.iowait, quint64(10));
    QCOMPARE(times.total(), quint64(1042));
    QCOMPARE(times.busy(), quint64(132));      // total - idle - iowait
}

void TestSystemStats::parseCpuTimesRejectsGarbage()
{
    CpuTimes times;
    QVERIFY(!SystemStats::parseCpuTimes(QStringLiteral("intr 1 2 3\n"), &times));
    QVERIFY(!SystemStats::parseCpuTimes(QStringLiteral("cpu  1 2\n"), &times));   // 字段太少
    QVERIFY(!SystemStats::parseCpuTimes(QString(), &times));
}

void TestSystemStats::cpuUsageIsDeltaBased()
{
    CpuTimes before;
    before.user = 100; before.system = 100; before.idle = 800;      // busy 200 / total 1000
    CpuTimes after = before;
    after.user += 100;      // 新增 100 忙
    after.idle += 100;      // 新增 100 闲
    // 增量 busy=100, total=200 → 50%
    QCOMPARE(SystemStats::cpuUsagePercent(before, after), 50.0);

    // 时间片没前进 → -1（采样太密），不能报 0% 骗人
    QCOMPARE(SystemStats::cpuUsagePercent(before, before), -1.0);
}

void TestSystemStats::parseMemInfoRealShape()
{
    const QString meminfo =
        QStringLiteral("MemTotal:        3901234 kB\n"
                       "MemFree:          123456 kB\n"
                       "MemAvailable:    2000000 kB\n"
                       "Buffers:           12345 kB\n"
                       "Cached:           800000 kB\n"
                       "SwapTotal:             0 kB\n");
    MemInfo info;
    QVERIFY(SystemStats::parseMemInfo(meminfo, &info));
    QCOMPARE(info.totalKb, quint64(3901234));
    QCOMPARE(info.availableKb, quint64(2000000));
    QCOMPARE(info.usedKb(), quint64(1901234));
    QVERIFY(info.usedPercent() > 48.0 && info.usedPercent() < 49.0);

    // 没有 MemAvailable 的老内核 → 退回 free+buffers+cached（不能崩、不能当 0）
    MemInfo old;
    QVERIFY(SystemStats::parseMemInfo(
        QStringLiteral("MemTotal: 1000 kB\nMemFree: 100 kB\nBuffers: 100 kB\nCached: 300 kB\n"),
        &old));
    QCOMPARE(old.usedKb(), quint64(500));
}

void TestSystemStats::parseNpuLoadRealOutput()
{
    // 板端真实输出：注意冒号后有两个空格
    double percent = -1;
    QVERIFY(SystemStats::parseNpuLoad(QStringLiteral("NPU load:  0%"), &percent));
    QCOMPARE(percent, 0.0);
    QVERIFY(SystemStats::parseNpuLoad(QStringLiteral("NPU load: 85%"), &percent));
    QCOMPARE(percent, 85.0);
    QVERIFY(!SystemStats::parseNpuLoad(QStringLiteral("npu busy"), &percent));
}

void TestSystemStats::parseMilliCelsiusRealValue()
{
    bool ok = false;
    QCOMPARE(SystemStats::parseMilliCelsius(QStringLiteral("31250"), &ok), 31.25);
    QVERIFY(ok);
    SystemStats::parseMilliCelsius(QStringLiteral("abc"), &ok);
    QVERIFY(!ok);
}

void TestSystemStats::parseIpAddrOutputRealShape()
{
    const QString text =
        QStringLiteral("1: lo: <LOOPBACK,UP,LOWER_UP> mtu 65536 qdisc noqueue state UNKNOWN\n"
                       "    inet 127.0.0.1/8 scope host lo\n"
                       "5: wlan0: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500 qdisc mq state UP\n"
                       "    inet 192.168.137.30/24 brd 192.168.137.255 scope global dynamic wlan0\n"
                       "6: eth0: <BROADCAST,MULTICAST> mtu 1500 qdisc noop state DOWN\n");
    const QVector<core::NetAddr> nets = SystemStats::parseIpAddrOutput(text);
    QCOMPARE(nets.size(), 3);
    QCOMPARE(nets.at(0).iface, QStringLiteral("lo"));
    QVERIFY(nets.at(0).up);
    QCOMPARE(nets.at(0).ipv4, QStringLiteral("127.0.0.1"));
    QCOMPARE(nets.at(1).iface, QStringLiteral("wlan0"));
    QVERIFY(nets.at(1).up);
    QCOMPARE(nets.at(1).ipv4, QStringLiteral("192.168.137.30"));
    QCOMPARE(nets.at(2).iface, QStringLiteral("eth0"));
    QVERIFY(!nets.at(2).up);                 // DOWN
    QVERIFY(nets.at(2).ipv4.isEmpty());
}

void TestSystemStats::fakeRootReadsEverything()
{
    QTemporaryDir tmp;
    QVERIFY(tmp.isValid());
    const QString root = tmp.path();

    // 造一棵假 /proc、/sys（文件内容用板端真实形状）
    const auto write = [&root](const QString& rel, const QString& content) {
        QDir().mkpath(QFileInfo(root + rel).absolutePath());
        QFile file(root + rel);
        QVERIFY(file.open(QIODevice::WriteOnly));
        file.write(content.toUtf8());
        file.close();
    };
    write(QStringLiteral("/proc/stat"),
          QStringLiteral("cpu  100 0 50 850 0 0 0 0 0 0\ncpu0 25 0 12 212 0 0 0 0 0 0\n"));
    write(QStringLiteral("/proc/meminfo"),
          QStringLiteral("MemTotal: 1000000 kB\nMemAvailable: 400000 kB\n"));
    write(QStringLiteral("/sys/kernel/debug/rknpu/load"), QStringLiteral("NPU load: 42%\n"));
    write(QStringLiteral("/sys/class/devfreq/fde40000.npu/cur_freq"), QStringLiteral("600000000\n"));
    write(QStringLiteral("/sys/class/devfreq/dmc/cur_freq"), QStringLiteral("324000000\n"));
    write(QStringLiteral("/sys/class/thermal/thermal_zone0/type"), QStringLiteral("soc-thermal\n"));
    write(QStringLiteral("/sys/class/thermal/thermal_zone0/temp"), QStringLiteral("31250\n"));

    SystemStats stats(root);
    QCOMPARE(stats.rootPrefix(), root + QStringLiteral("/"));

    CpuTimes times;
    QVERIFY(stats.readCpuTimes(&times));
    QCOMPARE(times.idle, quint64(850));

    const MemInfo mem = stats.readMemInfo();
    QCOMPARE(mem.totalKb, quint64(1000000));
    QCOMPARE(mem.usedKb(), quint64(600000));

    double npu = -1;
    QVERIFY(stats.readNpuLoad(&npu));
    QCOMPARE(npu, 42.0);

    const QVector<core::FreqEntry> freqs = stats.readFreqs();
    QCOMPARE(freqs.size(), 2);
    bool sawNpu = false;
    for (const core::FreqEntry& entry : freqs) {
        if (entry.name.contains(QStringLiteral("npu"))) {
            sawNpu = true;
            QCOMPARE(entry.hz, quint64(600000000));
        }
    }
    QVERIFY(sawNpu);

    const QVector<core::ThermalEntry> thermals = stats.readThermals();
    QCOMPARE(thermals.size(), 1);
    QCOMPARE(thermals.first().type, QStringLiteral("soc-thermal"));
    QCOMPARE(thermals.first().celsius, 31.25);
}

void TestSystemStats::fakeRootMissingFilesFailClearly()
{
    QTemporaryDir tmp;
    QVERIFY(tmp.isValid());
    SystemStats stats(tmp.path());          // 空目录：什么都没有

    CpuTimes times;
    QVERIFY(!stats.readCpuTimes(&times));   // 必须明确失败
    const MemInfo mem = stats.readMemInfo();
    QCOMPARE(mem.totalKb, quint64(0));      // 读不到 → total 为 0（界面显示"不可读"）
    QCOMPARE(mem.usedPercent(), -1.0);
    double npu = -1;
    QVERIFY(!stats.readNpuLoad(&npu));
    QVERIFY(stats.readFreqs().isEmpty());
    QVERIFY(stats.readThermals().isEmpty());
    QCOMPARE(stats.cpuUsageSince(times), -1.0);
}

QTEST_APPLESS_MAIN(TestSystemStats)
#include "test_system_stats.moc"
