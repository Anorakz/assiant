// ============================================================================
//  gui/src/ui/sys_page.cpp — 系统与网络页实现
// ============================================================================
#include "ui/sys_page.h"
#include "ui/state_views.h"    // T15-16 ②：hint 用 StateBanner ✓

#include <QDebug>
#include <QFrame>
#include <QGridLayout>
#include <QHBoxLayout>
#include <QHash>
#include <QLabel>
#include <QPushButton>
#include <QVBoxLayout>

using core::FreqEntry;
using core::NetAddr;
using core::ThermalEntry;

namespace {

/// 把 Hz 变成好看的分档（324000000 → "324 MHz"）
QString formatHz(quint64 hz)
{
    if (hz == 0) {
        return QStringLiteral("0 MHz");
    }
    if (hz >= 1000000000ULL) {
        return QStringLiteral("%1 GHz").arg(double(hz) / 1e9, 0, 'f', 2);
    }
    return QStringLiteral("%1 MHz").arg(double(hz) / 1e6, 0, 'f', 0);
}

QString formatGb(quint64 kb)
{
    return QStringLiteral("%1 GB").arg(double(kb) / 1024.0 / 1024.0, 0, 'f', 2);
}

} // namespace

SysPage::SysPage(QWidget* parent)
    : QWidget(parent)
    , stats_(core::SystemStats())
{
    build();
    refresh();
}

SysPage::SysPage(const core::SystemStats& stats, QWidget* parent)
    : QWidget(parent)
    , stats_(stats)
{
    build();
    refresh();
}

void SysPage::build()
{
    auto* root = new QHBoxLayout(this);
    root->setContentsMargins(0, 0, 0, 0);
    root->setSpacing(12);

    // ---------------- 左：指标瓷砖 ----------------
    auto* left = new QFrame(this);
    left->setObjectName(QStringLiteral("AreaFrame"));
    auto* leftBox = new QVBoxLayout(left);
    leftBox->setContentsMargins(16, 14, 16, 14);
    leftBox->setSpacing(10);

    auto* title = new QLabel(QStringLiteral("系统资源"), left);
    title->setObjectName(QStringLiteral("AreaTitle"));
    leftBox->addWidget(title);

    auto* grid = new QGridLayout();
    grid->setHorizontalSpacing(10);
    grid->setVerticalSpacing(10);
    addMetric(grid, 0, 0, QStringLiteral("cpu"), QStringLiteral("CPU 占用"));
    addMetric(grid, 0, 1, QStringLiteral("mem"), QStringLiteral("内存"));
    addMetric(grid, 0, 2, QStringLiteral("npu"), QStringLiteral("NPU 负载"));
    addMetric(grid, 1, 0, QStringLiteral("freq_npu"), QStringLiteral("NPU 频率"));
    addMetric(grid, 1, 1, QStringLiteral("freq_dmc"), QStringLiteral("DMC 频率"));
    addMetric(grid, 1, 2, QStringLiteral("freq_gpu"), QStringLiteral("GPU 频率"));
    addMetric(grid, 2, 0, QStringLiteral("temp_soc"), QStringLiteral("SOC 温度"));
    addMetric(grid, 2, 1, QStringLiteral("temp_gpu"), QStringLiteral("GPU 温度"));
    addMetric(grid, 2, 2, QStringLiteral("freq_rkvdec"), QStringLiteral("解码器频率"));
    leftBox->addLayout(grid);
    leftBox->addStretch(1);

    // T15-16 遗留②：hint 换成 StateBanner ✓ —— 这是**关键故障**（读不到 /proc ✓），
    //   一行灰字太不显眼 ✓；空文本时它**自己整条隐藏** ✓（比手工判断省心 ✓）。
    hint_ = new StateBanner(left);
    leftBox->addWidget(hint_);
    root->addWidget(left, 3);

    // ---------------- 右：网络 + 看门狗 ----------------
    auto* right = new QFrame(this);
    right->setObjectName(QStringLiteral("AreaFrame"));
    auto* rightBox = new QVBoxLayout(right);
    rightBox->setContentsMargins(16, 14, 16, 14);
    rightBox->setSpacing(10);

    auto* netTitle = new QLabel(QStringLiteral("网络"), right);
    netTitle->setObjectName(QStringLiteral("AreaTitle"));
    rightBox->addWidget(netTitle);

    auto* netBox = new QVBoxLayout();
    netBox->setSpacing(6);
    for (const QString& iface : {QStringLiteral("wlan0"), QStringLiteral("eth0"),
                                 QStringLiteral("lo")}) {
        auto* row = new QWidget(right);
        auto* rowBox = new QHBoxLayout(row);
        rowBox->setContentsMargins(0, 0, 0, 0);
        rowBox->setSpacing(8);
        auto* name = new QLabel(iface.toUpper(), row);
        name->setObjectName(QStringLiteral("SysLabel"));
        name->setFixedWidth(70);
        auto* value = new QLabel(QStringLiteral("不可读"), row);
        value->setObjectName(QStringLiteral("SysValueSmall"));
        value->setTextInteractionFlags(Qt::TextSelectableByMouse);
        values_.insert(QStringLiteral("net_") + iface, value);
        rowBox->addWidget(name);
        rowBox->addWidget(value, 1);
        netBox->addWidget(row);
    }
    rightBox->addLayout(netBox);

    auto* hostTitle = new QLabel(QStringLiteral("串流主机"), right);
    hostTitle->setObjectName(QStringLiteral("AreaTitle"));
    rightBox->addWidget(hostTitle);
    streamHost_ = new QLabel(QStringLiteral("未知"), right);
    streamHost_->setObjectName(QStringLiteral("SysValueSmall"));
    rightBox->addWidget(streamHost_);

    rightBox->addStretch(1);

    watchdog_ = new QPushButton(QStringLiteral("看门狗：未启用"), right);
    // ⚠ T7-3：这个 objectName 原来误用了 "NextWallpaper"（借那条 QSS 规则的外形）。
    //    主区「下一张」按钮删掉、QSS 规则也跟着删了，所以这里改成自己的名字 +
    //    自己的样式规则 —— 否则看门狗会掉回系统默认按钮外观。
    watchdog_->setObjectName(QStringLiteral("WatchdogButton"));
    watchdog_->setCursor(Qt::PointingHandCursor);
    connect(watchdog_, &QPushButton::clicked, this, [this]() { triggerWatchdog(); });
    rightBox->addWidget(watchdog_);

    root->addWidget(right, 2);
}

QLabel* SysPage::addMetric(QGridLayout* grid, int row, int col, const QString& key,
                           const QString& title)
{
    auto* tile = new QFrame(this);
    tile->setObjectName(QStringLiteral("SysTile"));
    auto* box = new QVBoxLayout(tile);
    box->setContentsMargins(12, 10, 12, 10);
    box->setSpacing(2);

    auto* value = new QLabel(QStringLiteral("不可读"), tile);
    value->setObjectName(QStringLiteral("SysValue"));
    auto* name = new QLabel(title, tile);
    name->setObjectName(QStringLiteral("SysLabel"));

    box->addWidget(value);
    box->addWidget(name);
    if (grid != nullptr) {
        grid->addWidget(tile, row, col);
    }
    values_.insert(key, value);
    return value;
}

void SysPage::setMetric(const QString& key, const QString& text)
{
    QLabel* label = values_.value(key, nullptr);
    if (label != nullptr) {
        label->setText(text);
    }
}

QString SysPage::metricText(const QString& key) const
{
    QLabel* label = values_.value(key, nullptr);
    return label != nullptr ? label->text() : QString();
}

bool SysPage::refresh()
{
    // ---- CPU：需要两次采样做差 ----
    core::CpuTimes now;
    const bool cpuOk = stats_.readCpuTimes(&now);
    if (!cpuOk) {
        setMetric(QStringLiteral("cpu"), QStringLiteral("不可读"));
    } else if (!hasLastCpu_) {
        // ⚠ 首次采样成功时曾经什么都不设，标签就停在初值"不可读"（单测抓到）
        setMetric(QStringLiteral("cpu"), QStringLiteral("采集中…"));
    } else {
        const double usage = core::SystemStats::cpuUsagePercent(lastCpu_, now);
        setMetric(QStringLiteral("cpu"),
                  usage < 0 ? QStringLiteral("采集中…")
                            : QStringLiteral("%1 %").arg(usage, 0, 'f', 1));
    }
    if (cpuOk) {
        lastCpu_ = now;
        hasLastCpu_ = true;
    }

    // ---- 内存 ----
    const core::MemInfo mem = stats_.readMemInfo();
    if (mem.totalKb > 0) {
        setMetric(QStringLiteral("mem"),
                  QStringLiteral("%1 / %2").arg(formatGb(mem.usedKb()), formatGb(mem.totalKb)));
    } else {
        setMetric(QStringLiteral("mem"), QStringLiteral("不可读"));
    }

    // ---- NPU ----
    double npu = 0;
    setMetric(QStringLiteral("npu"),
              stats_.readNpuLoad(&npu) ? QStringLiteral("%1 %").arg(npu, 0, 'f', 0)
                                       : QStringLiteral("不可读"));

    // ---- 频率 ----
    const QVector<FreqEntry> freqs = stats_.readFreqs();
    const auto setFreq = [this, &freqs](const QString& key, const QString& needle) {
        for (const FreqEntry& entry : freqs) {
            if (entry.name.contains(needle, Qt::CaseInsensitive)) {
                setMetric(key, formatHz(entry.hz));
                return;
            }
        }
        setMetric(key, QStringLiteral("不可读"));
    };
    setFreq(QStringLiteral("freq_npu"), QStringLiteral("npu"));
    setFreq(QStringLiteral("freq_dmc"), QStringLiteral("dmc"));
    setFreq(QStringLiteral("freq_gpu"), QStringLiteral("gpu"));
    setFreq(QStringLiteral("freq_rkvdec"), QStringLiteral("rkvdec"));

    // ---- 温度 ----
    const QVector<ThermalEntry> thermals = stats_.readThermals();
    const auto tempOf = [&thermals](const QString& needle) -> double {
        for (const ThermalEntry& entry : thermals) {
            if (entry.type.contains(needle, Qt::CaseInsensitive)) {
                return entry.celsius;
            }
        }
        return -1000.0;
    };
    const double socTemp = tempOf(QStringLiteral("soc"));
    setMetric(QStringLiteral("temp_soc"),
              socTemp > -100 ? QStringLiteral("%1 ℃").arg(socTemp, 0, 'f', 1)
                             : QStringLiteral("不可读"));
    const double gpuTemp = tempOf(QStringLiteral("gpu"));
    setMetric(QStringLiteral("temp_gpu"),
              gpuTemp > -100 ? QStringLiteral("%1 ℃").arg(gpuTemp, 0, 'f', 1)
                             : QStringLiteral("不可读"));

    // ---- 网络 ----
    const QVector<NetAddr> nets = stats_.readNetAddrs();
    const QStringList knownIfaces = stats_.readInterfaceNames();
    const auto netText = [&nets, &knownIfaces](const QString& want) -> QString {
        for (const NetAddr& entry : nets) {
            if (entry.iface == want) {
                if (!entry.up) {
                    return QStringLiteral("未连接");
                }
                return entry.ipv4.isEmpty() ? QStringLiteral("已连接（无 IPv4）") : entry.ipv4;
            }
        }
        // 区分"接口不存在"（不可读）与"接口在但没地址"（未连接）—— 板端 eth0 就是后者
        return knownIfaces.contains(want) ? QStringLiteral("未连接（无 IPv4）")
                                          : QStringLiteral("不可读");
    };
    for (const QString& iface : {QStringLiteral("wlan0"), QStringLiteral("eth0"),
                                 QStringLiteral("lo")}) {
        setMetric(QStringLiteral("net_") + iface, netText(iface));
    }

    const bool criticalOk = cpuOk || mem.totalKb > 0;
    if (hint_ != nullptr) {
        if (!criticalOk) {
            hint_->setState(StateBanner::Error,
                            QStringLiteral("读不到 /proc（路径：%1）——请检查权限或容器环境")
                                .arg(stats_.rootPrefix()));
        } else {
            // ⚠ **恢复时一定要收掉** ✗ —— 否则上一次"读不到"的横幅会留在屏幕上 ✓（我会为这条写牙齿 ✓）
            hint_->clear();
        }
    }
    return criticalOk;
}

void SysPage::setStreamHostState(const QString& text)
{
    if (streamHost_ != nullptr) {
        streamHost_->setText(text);
    }
}

void SysPage::triggerWatchdog()
{
    // 协议里没有看门狗命令 → 占位：只切按钮文案 + 给说明（不假装已启用）
    const bool nowOn = watchdog_->text().contains(QStringLiteral("未启用"));
    watchdog_->setText(nowOn ? QStringLiteral("看门狗：已请求启用（未接入）")
                             : QStringLiteral("看门狗：未启用"));
    noteText_ = nowOn ? QStringLiteral("看门狗启停未接入（协议暂无对应命令，先占位）")
                      : QStringLiteral("看门狗已回到未启用（占位）");
    if (hint_ != nullptr) {
        // T15-16 ②：另一处写 hint 的地方 ✓（差点漏掉 ✗）—— 空文本 ⇒ 它自己隐藏 ✓ 与原行为等价 ✓
        hint_->setState(StateBanner::Info, noteText_);
    }
    qInfo().noquote() << "[sys] 看门狗占位:" << noteText_;
}
