// ============================================================================
//  gui/src/core/system_stats.cpp — 系统与网络采集实现
// ============================================================================
#include "core/system_stats.h"

#include <QDir>
#include <QFile>
#include <QFileInfo>
#include <QProcess>
#include <QRegularExpression>
#include <QStringList>

namespace core {

quint64 CpuTimes::total() const
{
    return user + nice + system + idle + iowait + irq + softirq + steal;
}

quint64 CpuTimes::busy() const
{
    return total() - idle - iowait;
}

quint64 MemInfo::usedKb() const
{
    if (availableKb > 0 && availableKb <= totalKb) {
        return totalKb - availableKb;
    }
    const quint64 fallback = freeKb + buffersKb + cachedKb;
    return (fallback < totalKb) ? (totalKb - fallback) : 0;
}

double MemInfo::usedPercent() const
{
    if (totalKb == 0) {
        return -1.0;
    }
    return 100.0 * double(usedKb()) / double(totalKb);
}

SystemStats::SystemStats(const QString& rootPrefix)
    : root_(rootPrefix.isEmpty() ? QStringLiteral("/") : rootPrefix)
{
    if (!root_.endsWith(QLatin1Char('/'))) {
        root_ += QLatin1Char('/');
    }
}

// ---------------------------------------------------------------- 纯解析 ---

bool SystemStats::parseCpuTimes(const QString& statText, CpuTimes* out)
{
    if (out == nullptr) {
        return false;
    }
    const QStringList lines = statText.split(QLatin1Char('\n'));
    for (const QString& line : lines) {
        if (!line.startsWith(QLatin1String("cpu "))) {
            continue;                    // "cpu " 是总计行；cpu0/cpu1 会被跳过
        }
        const QStringList parts = line.split(QRegularExpression(QStringLiteral("\\s+")),
                                             QString::SkipEmptyParts);
        if (parts.size() < 5) {
            return false;
        }
        quint64 values[8] = {0, 0, 0, 0, 0, 0, 0, 0};
        for (int i = 1; i < parts.size() && i <= 8; ++i) {
            values[i - 1] = parts.at(i).toULongLong();
        }
        out->user = values[0];
        out->nice = values[1];
        out->system = values[2];
        out->idle = values[3];
        out->iowait = values[4];
        out->irq = values[5];
        out->softirq = values[6];
        out->steal = values[7];
        return true;
    }
    return false;
}

bool SystemStats::parseMemInfo(const QString& meminfoText, MemInfo* out)
{
    if (out == nullptr) {
        return false;
    }
    bool sawTotal = false;
    const QStringList lines = meminfoText.split(QLatin1Char('\n'));
    for (const QString& line : lines) {
        const int colon = line.indexOf(QLatin1Char(':'));
        if (colon <= 0) {
            continue;
        }
        const QString key = line.left(colon).trimmed();
        QString value = line.mid(colon + 1).trimmed();
        value.remove(QLatin1String("kB"));
        const quint64 kb = value.trimmed().toULongLong();

        if (key == QLatin1String("MemTotal")) {
            out->totalKb = kb;
            sawTotal = true;
        } else if (key == QLatin1String("MemAvailable")) {
            out->availableKb = kb;
        } else if (key == QLatin1String("MemFree")) {
            out->freeKb = kb;
        } else if (key == QLatin1String("Buffers")) {
            out->buffersKb = kb;
        } else if (key == QLatin1String("Cached")) {
            out->cachedKb = kb;
        }
    }
    return sawTotal;
}

bool SystemStats::parseNpuLoad(const QString& loadText, double* percent)
{
    if (percent == nullptr) {
        return false;
    }
    // 形如 "NPU load:  0%" 或 "NPU load: 85%, ..."
    QRegularExpression re(QStringLiteral("([0-9]+(?:\\.[0-9]+)?)\\s*%"));
    const QRegularExpressionMatch match = re.match(loadText);
    if (!match.hasMatch()) {
        return false;
    }
    bool ok = false;
    const double value = match.captured(1).toDouble(&ok);
    if (!ok) {
        return false;
    }
    *percent = value;
    return true;
}

double SystemStats::parseMilliCelsius(const QString& raw, bool* ok)
{
    bool localOk = false;
    const double milli = raw.trimmed().toDouble(&localOk);
    if (ok != nullptr) {
        *ok = localOk;
    }
    return localOk ? milli / 1000.0 : 0.0;
}

double SystemStats::cpuUsagePercent(const CpuTimes& prev, const CpuTimes& now)
{
    const quint64 totalDelta = now.total() - prev.total();
    if (totalDelta == 0) {
        return -1.0;                     // 没前进（采样太密/时间片没变）
    }
    const quint64 busyDelta = now.busy() - prev.busy();
    return 100.0 * double(busyDelta) / double(totalDelta);
}

QVector<NetAddr> SystemStats::parseIpAddrOutput(const QString& text)
{
    QVector<NetAddr> result;
    NetAddr current;
    bool hasCurrent = false;

    const QStringList lines = text.split(QLatin1Char('\n'));
    for (const QString& rawLine : lines) {
        const QString line = rawLine.trimmed();
        if (line.isEmpty()) {
            continue;
        }
        // "5: wlan0: <BROADCAST,...UP...> mtu 1500 ..."
        QRegularExpression ifaceRe(QStringLiteral("^\\d+:\\s+([^:]+):\\s*<([^>]*)>"));
        const QRegularExpressionMatch ifaceMatch = ifaceRe.match(line);
        if (ifaceMatch.hasMatch()) {
            if (hasCurrent) {
                result.append(current);
            }
            current = NetAddr();
            current.iface = ifaceMatch.captured(1).trimmed();
            current.up = ifaceMatch.captured(2).contains(QLatin1String("UP"));
            hasCurrent = true;
            continue;
        }
        if (hasCurrent && line.startsWith(QLatin1String("inet "))) {
            const QStringList parts = line.split(QRegularExpression(QStringLiteral("\\s+")),
                                                 QString::SkipEmptyParts);
            if (parts.size() >= 2) {
                current.ipv4 = parts.at(1).section(QLatin1Char('/'), 0, 0);
            }
        }
    }
    if (hasCurrent) {
        result.append(current);
    }
    return result;
}

// ---------------------------------------------------------------- 读板子 ---

QString SystemStats::readTextFile(const QString& absolutePath) const
{
    QFile file(absolutePath);
    if (!file.open(QIODevice::ReadOnly)) {
        return QString();
    }
    // ⚠ procfs/sysfs 是 st_size==0 的伪文件，绝对不能靠 atEnd()/readLine 判断
    return QString::fromUtf8(file.readAll()).trimmed();
}

CpuTimes SystemStats::readCpuTimes() const
{
    CpuTimes times;
    readCpuTimes(&times);
    return times;
}

bool SystemStats::readCpuTimes(CpuTimes* out) const
{
    const QString text = readTextFile(root_ + QStringLiteral("proc/stat"));
    if (text.isEmpty()) {
        return false;
    }
    return parseCpuTimes(text, out);
}

MemInfo SystemStats::readMemInfo() const
{
    MemInfo info;
    const QString text = readTextFile(root_ + QStringLiteral("proc/meminfo"));
    if (!text.isEmpty()) {
        parseMemInfo(text, &info);
    }
    return info;
}

bool SystemStats::readNpuLoad(double* percent) const
{
    const QString text = readTextFile(root_ + QStringLiteral("sys/kernel/debug/rknpu/load"));
    if (text.isEmpty()) {
        return false;
    }
    return parseNpuLoad(text, percent);
}

QVector<FreqEntry> SystemStats::readFreqs() const
{
    QVector<FreqEntry> result;
    QDir dir(root_ + QStringLiteral("sys/class/devfreq"));
    const QStringList names = dir.entryList(QDir::Dirs | QDir::NoDotAndDotDot, QDir::Name);
    for (const QString& name : names) {
        const QString cur = readTextFile(dir.filePath(name + QStringLiteral("/cur_freq")));
        if (cur.isEmpty()) {
            continue;
        }
        FreqEntry entry;
        entry.name = name;
        entry.hz = cur.toULongLong();
        result.append(entry);
    }
    return result;
}

QVector<ThermalEntry> SystemStats::readThermals() const
{
    QVector<ThermalEntry> result;
    QDir dir(root_ + QStringLiteral("sys/class/thermal"));
    const QStringList names =
        dir.entryList({QStringLiteral("thermal_zone*")}, QDir::Dirs | QDir::NoDotAndDotDot, QDir::Name);
    for (const QString& name : names) {
        const QString type = readTextFile(dir.filePath(name + QStringLiteral("/type")));
        const QString temp = readTextFile(dir.filePath(name + QStringLiteral("/temp")));
        if (temp.isEmpty()) {
            continue;
        }
        bool ok = false;
        const double celsius = parseMilliCelsius(temp, &ok);
        if (!ok) {
            continue;
        }
        ThermalEntry entry;
        entry.type = type.isEmpty() ? name : type;
        entry.celsius = celsius;
        result.append(entry);
    }
    return result;
}

QVector<NetAddr> SystemStats::readNetAddrs() const
{
    // 地址：直接问 `ip -4 addr show`（比自己解析 /proc/net 稳），解析交给纯函数
    QProcess ip;
    ip.start(QStringLiteral("ip"),
             {QStringLiteral("-4"), QStringLiteral("addr"), QStringLiteral("show")});
    if (ip.waitForFinished(2000) && ip.exitCode() == 0) {
        const QVector<NetAddr> parsed =
            parseIpAddrOutput(QString::fromUtf8(ip.readAllStandardOutput()));
        if (!parsed.isEmpty()) {
            return parsed;
        }
    }
    // 退路：至少报出接口名与 up/down（/sys/class/net/*/operstate）
    QVector<NetAddr> result;
    QDir netDir(root_ + QStringLiteral("sys/class/net"));
    const QStringList ifaces =
        netDir.entryList(QDir::Dirs | QDir::NoDotAndDotDot, QDir::Name);
    for (const QString& iface : ifaces) {
        NetAddr entry;
        entry.iface = iface;
        entry.up = readTextFile(root_ + QStringLiteral("sys/class/net/%1/operstate").arg(iface))
                   == QLatin1String("up");
        result.append(entry);
    }
    return result;
}

QStringList SystemStats::readInterfaceNames() const
{
    QDir netDir(root_ + QStringLiteral("sys/class/net"));
    return netDir.entryList(QDir::Dirs | QDir::NoDotAndDotDot, QDir::Name);
}

double SystemStats::cpuUsageSince(const CpuTimes& prev) const{
    CpuTimes now;
    if (!readCpuTimes(&now)) {
        return -1.0;
    }
    return cpuUsagePercent(prev, now);
}

} // namespace core

