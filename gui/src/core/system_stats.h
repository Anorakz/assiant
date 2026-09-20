// ============================================================================
//  gui/src/core/system_stats.h — 系统与网络数据采集（T10）
//
//  板端实测可读的来源（本文件的路径都在真机上核过）：
//      CPU 占用   /proc/stat                （两次采样的差值）
//      内存       /proc/meminfo
//      NPU 负载   /sys/kernel/debug/rknpu/load       形如 "NPU load:  0%"
//      频率       /sys/class/devfreq/*/cur_freq      dmc 324M / npu 600M / gpu 200M / rkvdec 297M
//      温度       /sys/class/thermal/thermal_zone*/{type,temp}   soc 31250 → 31.25℃
//      网络       `ip -4 addr show` 的解析（也可直接读 /sys/class/net）
//
//  设计：**rootPrefix 可注入**（默认 "/"），所以单测能造一棵假 /proc、/sys 来验解析，
//  不依赖板端。纯解析函数另外单独暴露，便于精确断言。
// ============================================================================
#pragma once

#include <QString>
#include <QVector>

namespace core {

struct CpuTimes {
    quint64 user = 0;
    quint64 nice = 0;
    quint64 system = 0;
    quint64 idle = 0;
    quint64 iowait = 0;
    quint64 irq = 0;
    quint64 softirq = 0;
    quint64 steal = 0;

    quint64 total() const;
    quint64 busy() const;      ///< total - idle - iowait
};

struct MemInfo {
    quint64 totalKb = 0;
    quint64 availableKb = 0;
    quint64 freeKb = 0;
    quint64 buffersKb = 0;
    quint64 cachedKb = 0;

    quint64 usedKb() const;        ///< total - available（available 缺失时退回 free+buffers+cached）
    double usedPercent() const;    ///< 0..100；total 为 0 时返回 -1
};

struct FreqEntry {
    QString name;                  ///< dmc / npu / gpu / rkvdec ...
    quint64 hz = 0;
};

struct ThermalEntry {
    QString type;                  ///< soc-thermal / gpu-thermal ...
    double celsius = 0.0;
};

struct NetAddr {
    QString iface;
    QString ipv4;                  ///< 空 = 没地址
    bool up = false;
};

class SystemStats {
public:
    /// rootPrefix 默认 "/"；单测传假根目录（例如 /tmp/xxx/）
    explicit SystemStats(const QString& rootPrefix = QString());

    // ---------------- 纯解析（静态、可单测） ----------------
    /// 从 /proc/stat 的 "cpu ..." 首行解析累计时间片（单位 jiffies）
    static bool parseCpuTimes(const QString& statText, CpuTimes* out);
    /// 从 /proc/meminfo 解析（只取用到的几项）
    static bool parseMemInfo(const QString& meminfoText, MemInfo* out);
    /// 从 "NPU load:  0%" 解析出百分比
    static bool parseNpuLoad(const QString& loadText, double* percent);
    /// 毫摄氏度字符串 → 摄氏度（31250 → 31.25）
    static double parseMilliCelsius(const QString& raw, bool* ok);
    /// 两次采样的 CPU 占用百分比（0..100）；时间片没前进时返回 -1
    static double cpuUsagePercent(const CpuTimes& prev, const CpuTimes& now);
    /// 解析 `ip -4 addr show` 的输出 → 接口/IP/up 状态
    static QVector<NetAddr> parseIpAddrOutput(const QString& text);

    // ---------------- 读板子 ----------------
    QString readTextFile(const QString& absolutePath) const;   ///< 读不到返回空
    CpuTimes readCpuTimes() const;
    bool readCpuTimes(CpuTimes* out) const;
    MemInfo readMemInfo() const;
    bool readNpuLoad(double* percent) const;
    QVector<FreqEntry> readFreqs() const;
    QVector<ThermalEntry> readThermals() const;
    QVector<NetAddr> readNetAddrs() const;
    /// /sys/class/net 下的接口名（用来区分"接口不存在"与"接口存在但没地址"）
    QStringList readInterfaceNames() const;
    /// 相对上一次采样算 CPU 占用；读不到返回 -1
    double cpuUsageSince(const CpuTimes& prev) const;

    QString rootPrefix() const { return root_; }

private:
    QString root_;                 ///< 以 "/" 结尾
};

} // namespace core
