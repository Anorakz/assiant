// ============================================================================
//  gui/src/core/crash_log.cpp — GUI 崩溃日志实现（T14-8）
//
//  设计上的两个硬约束：
//
//  1) **信号处理器里不能分配内存、不能调 Qt**。所以除了 QString 环形缓冲（给人看的
//     报告用），还维护一份**裸字节尾巴** `g_tail`：消息进来时就 memcpy 进去，
//     SIGSEGV 处理器只做 `write(g_fd, …)` + 重置默认信号 + re-raise —— 这些都是
//     异步信号安全的。
//  2) **正常退出要能删掉本次会话文件**，但"未捕获异常/致命错误之后"的收尾（atexit /
//     closeCrashLoggerCleanly）**不能**把它删了 —— 所以有 `g_reported` 这个开关，
//     与 Agent 侧 `CrashLogger._reported` 同一个语义。
// ============================================================================
#include "core/crash_log.h"

#include <QCoreApplication>
#include <QDateTime>
#include <QDebug>
#include <QDir>
#include <QFile>
#include <QFileInfo>
#include <QSocketNotifier>
#include <QtGlobal>

#include <cstdio>
#include <cstring>
#include <exception>
#include <csignal>

#include <fcntl.h>        // open
#include <sys/types.h>    // ssize_t
#include <unistd.h>       // write / close（GUI 只在板端/Linux 编，见 docs/gui.md §1）

namespace core {
namespace {

//: 给报告用的字符串环形缓冲（最多留这么多条）
constexpr int kMaxMessages = 200;
//: 裸字节尾巴（信号处理器专用）；超出就丢最老的
constexpr int kMaxTailBytes = 64 * 1024;

QStringList g_messages;
QString g_dir;
QString g_path;
QString g_app = QStringLiteral("gui");
QMap<QString, QString> g_context;
bool g_installed = false;
bool g_reported = false;
int g_fd = -1;                       // 会话文件的裸 fd（信号处理器往它写）
QtMessageHandler g_prevHandler = nullptr;
std::terminate_handler g_prevTerminate = nullptr;
int g_keep = 20;

char g_tail[kMaxTailBytes];
size_t g_tailLen = 0;

const char* levelName(QtMsgType type)
{
    switch (type) {
    case QtDebugMsg:    return "DEBUG";
    case QtInfoMsg:     return "INFO ";
    case QtWarningMsg:  return "WARN ";
    case QtCriticalMsg: return "CRIT ";
    case QtFatalMsg:    return "FATAL";
    }
    return "?????";
}

/// 只 memcpy/memmove：可以从消息处理器里调，也可以在信号处理器里被读。
void appendTailRaw(const char* data, size_t len)
{
    if (len == 0) {
        return;
    }
    if (len >= static_cast<size_t>(kMaxTailBytes)) {          // 太长就只留尾巴
        data += (len - static_cast<size_t>(kMaxTailBytes));
        len = static_cast<size_t>(kMaxTailBytes);
    }
    if (g_tailLen + len > static_cast<size_t>(kMaxTailBytes)) {
        const size_t drop = g_tailLen + len - static_cast<size_t>(kMaxTailBytes);
        std::memmove(g_tail, g_tail + drop, g_tailLen - drop);
        g_tailLen -= drop;
    }
    std::memcpy(g_tail + g_tailLen, data, len);
    g_tailLen += len;
}

void appendLine(const QString& line)
{
    g_messages.append(line);
    while (g_messages.size() > kMaxMessages) {
        g_messages.removeFirst();
    }
    const QByteArray bytes = line.toUtf8() + '\n';
    appendTailRaw(bytes.constData(), static_cast<size_t>(bytes.size()));
}

void writeRawFd(const char* text)
{
    if (g_fd < 0 || text == nullptr) {
        return;
    }
    const size_t len = std::strlen(text);
    ssize_t ignored = ::write(g_fd, text, len);
    (void)ignored;
}

void copyToRawFd(const char* data, size_t len)
{
    if (g_fd < 0 || data == nullptr) {
        return;
    }
    ssize_t ignored = ::write(g_fd, data, len);
    (void)ignored;
}

QString makeSessionPath(const QString& dir, const QString& app)
{
    const QString stamp = QDateTime::currentDateTime().toString(QStringLiteral("yyyyMMdd-HHmmss"));
    return QDir(dir).filePath(QStringLiteral("%1-%2-%3.log")
                                  .arg(app, stamp, QString::number(QCoreApplication::applicationPid())));
}

QStringList headerLines()
{
    QStringList lines;
    lines << QString(78, QLatin1Char('='))
          << QStringLiteral("板端助手 %1 —— 会话/崩溃日志（T14-8）").arg(g_app)
          << QString(78, QLatin1Char('='))
          << QStringLiteral("开始时间 : %1").arg(
                 QDateTime::currentDateTime().toString(QStringLiteral("yyyy-MM-dd HH:mm:ss")))
          << QStringLiteral("进程     : pid=%1").arg(QCoreApplication::applicationPid())
          << QStringLiteral("命令行   : %1").arg(QCoreApplication::arguments().join(QLatin1Char(' ')))
          << QStringLiteral("工作目录 : %1").arg(QDir::currentPath())
          << QStringLiteral("Qt       : %1").arg(QString::fromLatin1(qVersion()));
    for (auto it = g_context.constBegin(); it != g_context.constEnd(); ++it) {
        lines << QStringLiteral("%1: %2").arg(it.key(), it.value());
    }
    lines << QString(78, QLatin1Char('-'))
          << QStringLiteral("⚠ 正常退出时这个文件会被删掉；它留在 logs/crash/ 里 = 上一次没干净退出。")
          << QString(78, QLatin1Char('-'))
          << QString();
    return lines;
}

void writeHeaderFile()
{
    QFile file(g_path);
    if (!file.open(QIODevice::WriteOnly | QIODevice::Truncate)) {
        return;
    }
    const QString text = headerLines().join(QLatin1Char('\n')) + QLatin1Char('\n');
    file.write(text.toUtf8());
    file.flush();
}

/// 只留最新 g_keep 份**历史报告**。
///
/// ⚠ 本次会话文件**不算**在里面（与 Agent 侧同一个理由）：它正常退出会被删掉，
///   算进去就成了"每次启动白扔一份老报告"，而且会与 GUI 的单测
///   （6 份 + KEEP=3 → 剩 3 份）对不上。
void pruneOldReports()
{
    QDir dir(g_dir);
    const QFileInfoList entries = dir.entryInfoList(QStringList() << QStringLiteral("*.log"),
                                                   QDir::Files, QDir::Time);   // 新 → 旧
    int kept = 0;
    for (const QFileInfo& info : entries) {
        if (info.absoluteFilePath() == g_path) {
            continue;                       // 本次会话（还没崩，所以不是历史报告）
        }
        if (++kept > g_keep) {
            QFile::remove(info.absoluteFilePath());
        }
    }
}

/// 报告正文：写给 QFile 的文本。
///
/// ⚠ 顺序刻意是「先最近消息、后崩溃原因」：
///   ① 时间顺序本来就该这样（消息发生在崩溃之前）；
///   ② 启动横幅只取报告**头尾**（`previousReportBanner`），原因放在最后才一定看得见 ——
///      放中间的话，消息一多就会被省略掉，横幅里只剩一坨日志。
QString reportText(const QString& reason, bool withTail)
{
    QStringList chunks;
    chunks << QString();
    if (withTail && !g_messages.isEmpty()) {
        chunks << QStringLiteral("--- 最近 %1 条消息 ---").arg(g_messages.size())
               << g_messages.join(QLatin1Char('\n'))
               << QString();
    }
    chunks << QString(78, QLatin1Char('!'))
           << QStringLiteral("崩溃/异常 : %1").arg(reason)
           << QStringLiteral("时间       : %1").arg(
                  QDateTime::currentDateTime().toString(QStringLiteral("yyyy-MM-dd HH:mm:ss")))
           << QString(78, QLatin1Char('!'))
           << QString();
    return chunks.join(QLatin1Char('\n'));
}

void messageHandler(QtMsgType type, const QMessageLogContext& context, const QString& message)
{
    const QString line = QStringLiteral("%1 %2 %3")
                             .arg(QDateTime::currentDateTime().toString(
                                      QStringLiteral("yyyy-MM-dd HH:mm:ss.zzz")),
                                  QString::fromLatin1(levelName(type)), message);
    appendLine(line);
    // 原样保留原来的去处（通常是 stderr → systemd 的 logs/gui.out）
    if (g_prevHandler != nullptr) {
        g_prevHandler(type, context, message);
    } else {
        const QByteArray bytes = line.toUtf8() + '\n';
        std::fwrite(bytes.constData(), 1, static_cast<size_t>(bytes.size()), stderr);
        std::fflush(stderr);
    }
    if (type == QtFatalMsg) {
        writeCrashReport(QStringLiteral("qFatal: %1").arg(message));
        std::abort();
    }
}

void terminateHandler()
{
    writeCrashReport(QStringLiteral("std::terminate（未捕获的 C++ 异常 / 线程里抛出）"));
    if (g_prevTerminate != nullptr && g_prevTerminate != &std::abort) {
        g_prevTerminate();
    }
    std::abort();
}

/// 致命信号：只做异步信号安全的事（写 fd + 重置默认处理 + 重新抛）
void fatalSignalHandler(int sig)
{
    const char* name = "SIGNAL";
    switch (sig) {
    case SIGSEGV: name = "SIGSEGV"; break;
    case SIGABRT: name = "SIGABRT"; break;
    case SIGBUS:  name = "SIGBUS";  break;
    case SIGILL:  name = "SIGILL";  break;
    case SIGFPE:  name = "SIGFPE";  break;
    default: break;
    }
    writeRawFd("\n");
    writeRawFd("!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!\n");
    writeRawFd("致命信号   : ");
    writeRawFd(name);
    writeRawFd("（下面是崩溃前的最近消息）\n");
    writeRawFd("!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!\n");
    copyToRawFd(g_tail, g_tailLen);        // 裸字节尾巴，不分配内存
    writeRawFd("\n（信号处理器只能写这么多：完整现场请配合 core dump / gdb）\n");
    std::signal(sig, SIG_DFL);
    std::raise(sig);
}

void installSignalHandlers()
{
    for (int sig : {SIGSEGV, SIGABRT, SIGBUS, SIGILL, SIGFPE}) {
        std::signal(sig, fatalSignalHandler);
    }
}

// ---------------------------------------------------------------------------
//  SIGTERM / SIGINT（systemd stop / Ctrl-C）要走**干净退出**这条路
//
//  为什么必须单独处理：`Restart=always` 的服务经常被 stop/restart，而进程收到 SIGTERM
//  直接死掉的话没人调 closeCrashLoggerCleanly → logs/crash/ 里每隔一会儿就多一份
//  只有表头的"假崩溃"（板端实测：跑几轮 ctest 就攒了 5 份）。
//
//  做法是经典的 self-pipe：信号处理器只 `write()` 一个字节（异步信号安全），
//  真正的收尾在事件循环里由 QSocketNotifier 触发（那里可以安全地分配内存、调 Qt）。
// ---------------------------------------------------------------------------
int g_sigPipe[2] = {-1, -1};
QSocketNotifier* g_termNotifier = nullptr;

void terminateSignalHandler(int sig)
{
    const char byte = static_cast<char>(sig);
    ssize_t ignored = ::write(g_sigPipe[1], &byte, 1);
    (void)ignored;
}

void installTerminateSignals()
{
    if (::pipe(g_sigPipe) != 0) {
        g_sigPipe[0] = g_sigPipe[1] = -1;
        return;
    }
    g_termNotifier = new QSocketNotifier(g_sigPipe[0], QSocketNotifier::Read);
    QObject::connect(g_termNotifier, &QSocketNotifier::activated, [](int) {
        char byte = 0;
        ssize_t ignored = ::read(g_sigPipe[0], &byte, 1);
        (void)ignored;
        if (qApp != nullptr) {
            qInfo().noquote() << "[gui] 收到终止信号，按干净退出处理";
            qApp->quit();          // → 主循环退出 → atexit → closeCrashLoggerCleanly
        }
    });
    std::signal(SIGTERM, terminateSignalHandler);
    std::signal(SIGINT, terminateSignalHandler);
}

} // namespace

QString defaultCrashDir()
{
    const QByteArray fromEnv = qgetenv("AGENT_CRASH_DIR");
    if (!fromEnv.isEmpty()) {
        return QString::fromLocal8Bit(fromEnv);
    }
    // 可执行文件在 <仓库根>/gui/build/agent_gui → logs/crash 与 Agent 侧同一处
    const QDir exeDir(QCoreApplication::applicationDirPath());
    const QString repoRoot = QDir(exeDir.filePath(QStringLiteral("../.."))).absolutePath();
    if (QFileInfo::exists(QDir(repoRoot).filePath(QStringLiteral("agent")))) {
        return QDir(repoRoot).filePath(QStringLiteral("logs/crash"));
    }
    return QDir(QDir::currentPath()).filePath(QStringLiteral("logs/crash"));
}

QString installCrashLogger(const QString& dir, const QString& app,
                          const QMap<QString, QString>& context)
{
    if (g_installed) {
        return g_path;
    }
    if (!qEnvironmentVariableIsEmpty("AGENT_CRASH_DISABLE")) {
        return QString();          // 故意不置 g_installed：之后环境变量撤了还能装
    }
    g_app = app;
    g_context = context;
    g_dir = dir.isEmpty() ? defaultCrashDir() : dir;
    const QByteArray keepEnv = qgetenv("AGENT_CRASH_KEEP");
    if (!keepEnv.isEmpty()) {
        bool ok = false;
        const int value = QString::fromLocal8Bit(keepEnv).toInt(&ok);
        if (ok && value > 0) {
            g_keep = value;
        }
    }
    if (!QDir().mkpath(g_dir)) {
        return QString();          // 建不出目录就当没装（同样不置 g_installed）
    }
    g_path = makeSessionPath(g_dir, g_app);
    writeHeaderFile();
    pruneOldReports();

    g_fd = ::open(g_path.toLocal8Bit().constData(), O_WRONLY | O_APPEND);
    g_tailLen = 0;
    g_reported = false;
    g_prevHandler = qInstallMessageHandler(messageHandler);
    g_prevTerminate = std::set_terminate(terminateHandler);
    installSignalHandlers();
    installTerminateSignals();
    g_installed = true;
    return g_path;
}

QString crashSessionPath()
{
    return g_path;
}

bool writeCrashReport(const QString& reason)
{
    if (g_path.isEmpty()) {
        return false;
    }
    QFile file(g_path);
    if (!file.open(QIODevice::WriteOnly | QIODevice::Append)) {
        return false;
    }
    file.write(reportText(reason, true).toUtf8());
    file.flush();
    g_reported = true;
    return true;
}

void closeCrashLoggerCleanly()
{
    restoreCrashHandlers();
    if (g_fd >= 0) {
        ::close(g_fd);
        g_fd = -1;
    }
    if (!g_reported && !g_path.isEmpty()) {
        QFile::remove(g_path);     // 没写过报告 → 删掉（见文件头第 2 条）
    }
    // 收尾之后允许再装一次（测试里每条用例都要一个干净目录；生产只在退出时调）
    g_installed = false;
    g_path.clear();
    g_dir.clear();
    g_messages.clear();
    g_tailLen = 0;
    g_reported = false;
}

void recordMessage(const QString& line)
{
    appendLine(line);
}

QStringList recentMessages()
{
    return g_messages;
}

QString readReportHeadTail(const QString& path, int head, int tail)
{
    QFile file(path);
    if (!file.open(QIODevice::ReadOnly)) {
        return QString();
    }
    const QStringList lines = QString::fromUtf8(file.readAll()).split(QLatin1Char('\n'));
    if (lines.isEmpty()) {
        return QString();
    }
    if (lines.size() <= head + tail) {
        return lines.join(QLatin1Char('\n'));
    }
    QStringList out = lines.mid(0, head);
    out << QStringLiteral("… （中间省略 %1 行）…").arg(lines.size() - head - tail);
    out += lines.mid(lines.size() - tail);
    return out.join(QLatin1Char('\n'));
}

QString previousReportBanner(int head, int tail)
{
    if (g_dir.isEmpty() || g_path.isEmpty()) {
        return QString();
    }
    const QFileInfoList entries = QDir(g_dir).entryInfoList(
        QStringList() << QStringLiteral("*.log"), QDir::Files, QDir::Time);
    QString newest;
    for (const QFileInfo& info : entries) {
        if (info.absoluteFilePath() != g_path) {
            newest = info.absoluteFilePath();
            break;
        }
    }
    if (newest.isEmpty()) {
        return QString();
    }
    const QString marker = QDir(g_dir).filePath(QStringLiteral(".last-reported"));
    QFile markerFile(marker);
    const QString name = QFileInfo(newest).fileName();
    if (markerFile.open(QIODevice::ReadOnly)) {
        if (QString::fromUtf8(markerFile.readAll()).trimmed() == name) {
            return QString();          // 同一份只报一次，免得把"这次是不是又崩了"淹掉
        }
        markerFile.close();
    }
    const QString body = readReportHeadTail(newest, head, tail);
    if (body.isEmpty()) {
        return QString();
    }
    if (markerFile.open(QIODevice::WriteOnly | QIODevice::Truncate)) {
        markerFile.write(name.toUtf8() + '\n');
        markerFile.close();
    }
    return QStringLiteral("上次没有干净退出：%1（%2）\n%3")
        .arg(name, QFileInfo(newest).lastModified().toString(QStringLiteral("yyyy-MM-dd HH:mm:ss")),
             body);
}

void restoreCrashHandlers()
{
    if (g_prevHandler != nullptr) {
        qInstallMessageHandler(g_prevHandler);
        g_prevHandler = nullptr;
    }
    if (g_prevTerminate != nullptr) {
        std::set_terminate(g_prevTerminate);
        g_prevTerminate = nullptr;
    }
    if (g_termNotifier != nullptr) {
        delete g_termNotifier;
        g_termNotifier = nullptr;
    }
    for (int fd : {g_sigPipe[0], g_sigPipe[1]}) {
        if (fd >= 0) {
            ::close(fd);
        }
    }
    g_sigPipe[0] = g_sigPipe[1] = -1;
}

} // namespace core
