// ============================================================================
//  gui/src/core/crash_log.h — GUI 崩溃日志（T14-8）
//
//  与 Agent 侧（`agent/core/crash_log.py`）同一套约定：
//    · 每个进程一开始就把"本次会话"的文件建好，**正常退出时删掉** ——
//      所以 logs/crash/ 里剩下的每一份都是"上一次没干净退出"；
//    · 崩溃/致命错误发生时，除了原因与时间，还把**最近若干条 Qt 消息**写进去
//      （Qt 的日志平时只进 logs/gui.out，崩在现场时那一大坨没法看）；
//    · 本次启动会把上一份报告的**头尾**打到 `logs/gui.out`（同一份只报一次）；
//    · 只保留最新 N 份（默认 20）。
//
//  谁在调它
//    `gui/src/main.cpp`：装一次（`installCrashLogger`）+ 退出时 `closeCrashLoggerCleanly`；
//    另外 Qt 的 `qFatal` / `std::terminate` / SIGSEGV 等都在本模块里兜住。
//
//  为什么要有它（板端现实）
//    GUI 会碰 QMediaPlayer / GStreamer(mppvideodec) / onboard —— 这些崩起来是**信号级**
//    的，Python 那套 excepthook 根本看不到。没有这份日志就只能看到 systemd 报
//    "dumped core"，然后什么线索都没有。
// ============================================================================
#pragma once

#include <QMap>
#include <QString>
#include <QStringList>

namespace core {

/// 默认崩溃目录：`$AGENT_CRASH_DIR` → 否则可执行文件的 <仓库根>/logs/crash → 否则 cwd/logs/crash
QString defaultCrashDir();

/// 装好崩溃日志（幂等）。必须在 QApplication 之后、任何界面代码之前调用。
/// @param dir     崩溃目录（空 = defaultCrashDir()）
/// @param app     文件名前缀（gui → `gui-<时间>-<pid>.log`）
/// @param context 额外写进报告头部的键值（例如 配置 / 版本）
/// @return 本次会话文件路径（装不上时为空）
QString installCrashLogger(const QString& dir = QString(),
                          const QString& app = QStringLiteral("gui"),
                          const QMap<QString, QString>& context = QMap<QString, QString>());

/// 本次会话文件路径（没装则空）。
QString crashSessionPath();

/// 把"为什么死"写进本次会话文件。`qFatal` / `std::terminate` / 致命信号都会走它。
/// @return 写成功返回 true
bool writeCrashReport(const QString& reason);

/// 正常退出收尾：**没写过报告**才删掉本次会话文件（写过就留着）。
/// @note 收尾之后可以再 `installCrashLogger` 一次（测试用；生产只在退出时调一次）。
void closeCrashLoggerCleanly();

/// 最近留下的消息（最新在后），给测试与报告用。
QStringList recentMessages();

/// 把目录里最新一份报告的头尾拼成启动横幅；已经报过同一份则返回空串。
QString previousReportBanner(int head = 10, int tail = 25);

/// 读一份报告的头 head 行 + 尾 tail 行（中间省略）。
QString readReportHeadTail(const QString& path, int head = 10, int tail = 25);

// ---------------------------------------------------------------------------
//  下面两个是"给测试用"的内部入口，正常代码不该直接调
// ---------------------------------------------------------------------------
/// 记一条消息进环形缓冲（消息处理器内部调它）
void recordMessage(const QString& line);

/// 恢复安装前的 Qt 消息处理器 / 终止处理器（测试收尾用）
void restoreCrashHandlers();

} // namespace core
