// ============================================================================
//  gui/src/services/onboard_ctl.cpp — onboard 控制实现
// ============================================================================
#include "services/onboard_ctl.h"

#include <QByteArray>
#include <QDebug>
#include <QDir>
#include <QFile>
#include <QFileInfo>
#include <QProcess>
#include <QProcessEnvironment>
#include <QRegularExpression>
#include <QStringList>

#include <pwd.h>
#include <unistd.h>

namespace {

const char* const kService = "org.onboard.Onboard";
const char* const kPath = "/org/onboard/Onboard/Keyboard";
const char* const kIface = "org.onboard.Onboard.Keyboard";

/// 从 /proc/<pid>/environ 里取一个变量（文件是 NUL 分隔的）
QString procEnv(int pid, const QString& name)
{
    QFile file(QStringLiteral("/proc/%1/environ").arg(pid));
    if (!file.open(QIODevice::ReadOnly)) {
        return QString();
    }
    const QByteArray raw = file.readAll();
    const QList<QByteArray> items = raw.split('\0');
    for (const QByteArray& item : items) {
        const int eq = item.indexOf('=');
        if (eq <= 0) {
            continue;
        }
        if (item.left(eq) == name.toUtf8()) {
            return QString::fromUtf8(item.mid(eq + 1));
        }
    }
    return QString();
}

int procUid(int pid)
{
    QFile file(QStringLiteral("/proc/%1/status").arg(pid));
    if (!file.open(QIODevice::ReadOnly)) {
        return -1;
    }
    // ⚠ 不能用 file.atEnd() 逐行读：procfs 的 status 是 st_size==0 的伪文件，
    //   atEnd() 一开始就返回 true，循环一次都不进（T6 实测 uid 恒为 -1）。
    //   先 readAll() 再按行切。
    const QStringList lines = QString::fromUtf8(file.readAll()).split(QLatin1Char('\n'));
    for (const QString& line : lines) {
        if (!line.startsWith(QLatin1String("Uid:"))) {
            continue;
        }
        const QStringList parts = line.split(QRegularExpression(QStringLiteral("\\s+")),
                                             QString::SkipEmptyParts);
        if (parts.size() >= 2) {
            bool ok = false;
            const int uid = parts.at(1).toInt(&ok);
            return ok ? uid : -1;
        }
    }
    return -1;
}

/// 从 /proc/<pid>/cmdline 读出 argv（NUL 分隔）
QStringList readArgv(int pid)
{
    QFile file(QStringLiteral("/proc/%1/cmdline").arg(pid));
    if (!file.open(QIODevice::ReadOnly)) {
        return {};
    }
    const QByteArray raw = file.readAll();
    QStringList out;
    const QList<QByteArray> parts = raw.split('\0');
    for (const QByteArray& part : parts) {
        if (!part.isEmpty()) {
            out << QString::fromUtf8(part);
        }
    }
    return out;
}

/// 这个进程像不像 onboard：**某个 argv 恰好等于** /usr/bin/onboard。
///
/// ⚠ 不能用 `pgrep -f /usr/bin/onboard`：`-f` 是"整条命令行做子串匹配"，
///   实测会把**跑证据脚本的那个 shell 自己**也匹上（它的命令文本里含这个字符串），
///   于是 first() 拿到 root 的 pid → uid 取错 → 判定"拿不到会话总线"（T6 出图时踩到）。
///   按 argv **精确相等**匹配就不会有这种自匹配。
bool looksLikeOnboard(int pid)
{
    const QStringList argv = readArgv(pid);
    for (int i = 1; i < argv.size(); ++i) {   // 从 argv[1] 起：argv[0] 是可执行文件
        if (argv.at(i) == QLatin1String("/usr/bin/onboard")) {
            return true;
        }
    }
    return false;
}

} // namespace

OnboardCtl::OnboardCtl(QObject* parent)
    : QObject(parent)
{
}

bool OnboardCtl::wantsOnboard(const QString& inputSource)
{
    return inputSource == QLatin1String("keyboard");
}

bool OnboardCtl::probe(QString* detail)
{
    busAddress_.clear();
    ownerUser_.clear();
    ownerUid_.clear();
    ownerPid_ = -1;

    // 自己扫 /proc 找 onboard（不能用 `pgrep -f`，原因见 looksLikeOnboard 的注释）
    QDir proc(QStringLiteral("/proc"));
    const QStringList entries = proc.entryList(QDir::Dirs | QDir::NoDotAndDotDot, QDir::Name);
    int candidates = 0;
    QString seenPid;
    QString seenAddr;
    QString seenUid;
    QString reason;
    for (const QString& entry : entries) {
        bool isPid = false;
        const int pid = entry.toInt(&isPid);
        if (!isPid || !looksLikeOnboard(pid)) {
            continue;
        }
        ++candidates;
        const QString address = procEnv(pid, QStringLiteral("DBUS_SESSION_BUS_ADDRESS"));
        const int uid = procUid(pid);
        seenPid = QString::number(pid);
        seenAddr = address;
        seenUid = QString::number(uid);

        if (address.isEmpty()) {
            reason = QStringLiteral("environ 里没有 DBUS_SESSION_BUS_ADDRESS");
            continue;
        }
        if (uid < 0) {
            reason = QStringLiteral("读不到 /proc/<pid>/status 的 Uid");
            continue;
        }
        struct passwd* pw = ::getpwuid(static_cast<uid_t>(uid));
        if (pw == nullptr) {
            reason = QStringLiteral("getpwuid(%1) 失败").arg(uid);
            continue;
        }
        ownerPid_ = pid;
        busAddress_ = address;
        ownerUid_ = QString::number(uid);
        ownerUser_ = QString::fromUtf8(pw->pw_name);
        break;
    }

    const bool ok = available();
    if (detail) {
        if (ok) {
            *detail = QStringLiteral("pid=%1 user=%2 bus=%3")
                          .arg(ownerPid_)
                          .arg(ownerUser_, busAddress_);
        } else if (candidates == 0) {
            *detail = QStringLiteral("没找到 onboard 进程");
        } else {
            // 带上每一步的实况，省得下次还得猜（第一次实现就是在这里卡住的）
            *detail = QStringLiteral("找到 %1 个 onboard：pid=%2 uid=%3 bus=%4 —— %5")
                          .arg(candidates)
                          .arg(seenPid, seenUid, seenAddr, reason);
        }
    }
    return ok;
}

QString OnboardCtl::runGdbus(const QStringList& args, bool* ok, QString* error) const
{
    QStringList program;
    QStringList fullArgs;
    const bool sameUser = (::getuid() == ownerUid_.toUInt());

    if (sameUser) {
        program << QStringLiteral("gdbus");
        fullArgs = args;
    } else {
        // 跨用户：必须借 runuser 换成 onboard 的属主，并在子进程里显式带上总线地址
        program << QStringLiteral("runuser") << QStringLiteral("-u") << ownerUser_
                << QStringLiteral("--") << QStringLiteral("env")
                << (QStringLiteral("DBUS_SESSION_BUS_ADDRESS=") + busAddress_)
                << QStringLiteral("gdbus");
        fullArgs = args;
    }

    QProcess proc;
    QProcessEnvironment env = QProcessEnvironment::systemEnvironment();
    env.insert(QStringLiteral("DBUS_SESSION_BUS_ADDRESS"), busAddress_);
    proc.setProcessEnvironment(env);
    proc.start(program.takeFirst(), program + fullArgs);
    if (!proc.waitForFinished(4000)) {
        if (error) {
            *error = QStringLiteral("gdbus 超时");
        }
        if (ok) {
            *ok = false;
        }
        return QString();
    }
    const QString out = QString::fromUtf8(proc.readAllStandardOutput()).trimmed();
    const QString err = QString::fromUtf8(proc.readAllStandardError()).trimmed();
    if (proc.exitCode() != 0) {
        if (error) {
            *error = err.isEmpty() ? QStringLiteral("gdbus 退出码 %1").arg(proc.exitCode()) : err;
        }
        if (ok) {
            *ok = false;
        }
        return QString();
    }
    if (ok) {
        *ok = true;
    }
    return out;
}

bool OnboardCtl::callMethod(const QString& method, QString* error)
{
    if (!available()) {
        if (error) {
            *error = QStringLiteral("onboard 不可用（没探测到会话总线）");
        }
        return false;
    }
    bool ok = false;
    runGdbus({QStringLiteral("call"), QStringLiteral("--session"), QStringLiteral("--dest"),
              QString::fromLatin1(kService), QStringLiteral("--object-path"),
              QString::fromLatin1(kPath), QStringLiteral("--method"),
              QStringLiteral("%1.%2").arg(QString::fromLatin1(kIface), method)},
             &ok, error);
    if (!ok) {
        qWarning().noquote() << "[onboard]" << method << "失败:" << (error ? *error : QString());
        return false;
    }
    qInfo().noquote() << "[onboard]" << method << "成功";
    return true;
}

bool OnboardCtl::show(QString* error)
{
    return callMethod(QStringLiteral("Show"), error);
}

bool OnboardCtl::hide(QString* error)
{
    return callMethod(QStringLiteral("Hide"), error);
}

bool OnboardCtl::isVisible(bool* ok)
{
    bool localOk = false;
    QString error;
    const QString out = runGdbus(
        {QStringLiteral("call"), QStringLiteral("--session"), QStringLiteral("--dest"),
         QString::fromLatin1(kService), QStringLiteral("--object-path"),
         QString::fromLatin1(kPath), QStringLiteral("--method"),
         QStringLiteral("org.freedesktop.DBus.Properties.Get"),
         QString::fromLatin1(kIface), QStringLiteral("Visible")},
        &localOk, &error);
    if (ok) {
        *ok = localOk;
    }
    if (!localOk) {
        return false;
    }
    return out.contains(QLatin1String("true"));
}
