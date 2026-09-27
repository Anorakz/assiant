// ============================================================================
//  gui/tests/test_crash_log.cpp — GUI 崩溃日志（T14-8）
//
//  重点验三件事（都在 QTemporaryDir 里做，绝不碰仓库真实的 logs/crash）：
//    1) **正常收尾会删掉本次会话文件**：否则 logs/crash/ 会被"什么都没发生"的空报告塞满，
//       真报告反而找不到；
//    2) **写过报告就不删**：qFatal / 信号之后的收尾（atexit 走的就是 closeCrashLoggerCleanly）
//       如果无脑删文件，就会把刚写好的现场删掉 —— 这是最容易写反的一处；
//    3) 报告里除了原因，还要有**最近若干条 Qt 消息**（崩在现场时 logs/gui.out 那一大坨没法看）。
//
//  另验：保留份数有限、启动横幅只报一次、`readReportHeadTail` 只取头尾。
//
//  跑法（板端 / 任何有 Qt5 的地方）
//  ---------------------------------------------------------------------------
//      cd gui/build && QT_QPA_PLATFORM=offscreen ctest -R test_crash_log --output-on-failure
// ============================================================================

#include <QtTest>
#include <QTemporaryDir>

#include <ctime>
#include <utime.h>        // ::utime —— 给保留份数那条用例排 mtime（见该用例注释）

#include "core/crash_log.h"

class TestCrashLog : public QObject {
    Q_OBJECT

private:
    QTemporaryDir tmp_;

    QString dir() const { return tmp_.path(); }
    QStringList logs() const
    {
        return QDir(dir()).entryList(QStringList() << QStringLiteral("*.log"), QDir::Files);
    }

private slots:
    void init()
    {
        // 每条用例一个干净目录：装/卸是**全局**的，串起来会互相看到对方的报告
        QVERIFY(tmp_.isValid());
        core::restoreCrashHandlers();
        for (const QString& name : logs()) {
            QFile::remove(QDir(dir()).filePath(name));
        }
    }

    void cleanup()
    {
        core::closeCrashLoggerCleanly();
        core::restoreCrashHandlers();
        qunsetenv("AGENT_CRASH_DIR");
        qunsetenv("AGENT_CRASH_KEEP");
        qunsetenv("AGENT_CRASH_DISABLE");
    }

    /// 正常收尾：没写过报告 → 文件被删掉
    void cleanExitRemovesTheSessionFile()
    {
        const QString path = core::installCrashLogger(dir(), QStringLiteral("probe"));
        QVERIFY(!path.isEmpty());
        QVERIFY2(QFile::exists(path), "装好之后会话文件就该在");
        qInfo() << "自检：正常路径不应该留下报告";

        core::closeCrashLoggerCleanly();
        QVERIFY2(!QFile::exists(path), "正常收尾必须删掉会话文件（否则真报告会被淹）");
    }

    /// 写过报告 → 即使收尾被调用也留着，而且里面有原因 + 最近消息
    void reportedCrashSurvivesTheCleanup()
    {
        const QString path = core::installCrashLogger(dir(), QStringLiteral("probe"));
        QVERIFY(!path.isEmpty());
        for (int i = 0; i < 5; ++i) {
            qWarning().noquote() << QStringLiteral("自检消息-%1").arg(i);
        }
        QVERIFY(core::writeCrashReport(QStringLiteral("自检：qFatal 等价路径")));
        core::closeCrashLoggerCleanly();          // atexit 那条路

        QVERIFY2(QFile::exists(path), "写过报告就不该被收尾删掉");
        QFile file(path);
        QVERIFY(file.open(QIODevice::ReadOnly));
        const QString text = QString::fromUtf8(file.readAll());
        QVERIFY2(text.contains(QStringLiteral("自检：qFatal 等价路径")), qPrintable(text.left(400)));
        QVERIFY2(text.contains(QStringLiteral("自检消息-4")), "最近消息要进报告");
        QVERIFY2(text.contains(QStringLiteral("最近")), "要有「最近 N 条消息」那一段");
        QVERIFY2(text.contains(QStringLiteral("pid=")), "头部要有 pid");
        QVERIFY2(text.contains(QStringLiteral("Qt")), "头部要有 Qt 版本");
    }

    /// 环形缓冲有界：不能无限长
    void messageRingIsBounded()
    {
        core::installCrashLogger(dir(), QStringLiteral("probe"));
        for (int i = 0; i < 400; ++i) {
            qWarning().noquote() << QStringLiteral("洪峰-%1").arg(i);
        }
        const QStringList recent = core::recentMessages();
        QVERIFY2(recent.size() <= 200, qPrintable(QStringLiteral("留了 %1 条").arg(recent.size())));
        QVERIFY2(recent.last().contains(QStringLiteral("洪峰-399")), "最新的一条必须在");
        QVERIFY2(!recent.first().contains(QStringLiteral("洪峰-0")), "最老的应该已经被挤掉");
    }

    /// 保留份数：只留最新 N 份
    void retentionKeepsTheNewest()
    {
        const qint64 now = QDateTime::currentSecsSinceEpoch();
        for (int i = 0; i < 6; ++i) {
            QFile old(QDir(dir()).filePath(QStringLiteral("old-%1.log").arg(i)));
            QVERIFY(old.open(QIODevice::WriteOnly));
            old.write("x");
            old.close();
            // 让 mtime 有序（越后面的越新）。用 utime(2) 而不是 QFile::setFileTime：
            // 后者在这个 Qt 版本上按文件名那个重载解析不出来（板端实测编译错误）。
            struct utimbuf times;
            times.actime = times.modtime = static_cast<time_t>(now - 600 + i * 10);
            QVERIFY(::utime(qPrintable(old.fileName()), &times) == 0);
        }
        qputenv("AGENT_CRASH_KEEP", "3");
        const QString session = core::installCrashLogger(dir(), QStringLiteral("probe"));
        QVERIFY(!session.isEmpty());
        // 本次会话文件不算"历史报告"（它正常退出会被删掉），所以这里只数 old-*
        QStringList old;
        for (const QString& name : logs()) {
            if (name.startsWith(QStringLiteral("old-"))) {
                old << name;
            }
        }
        QCOMPARE(old.size(), 3);
        QVERIFY2(old.contains(QStringLiteral("old-5.log")), "最新的旧报告要留");
        QVERIFY2(!old.contains(QStringLiteral("old-0.log")), "最老的该被删");
        QVERIFY2(!old.contains(QStringLiteral("old-2.log")), "第 4 老的（old-2）也该被删");
        QVERIFY2(old.contains(QStringLiteral("old-3.log")), "第 3 老的要留着");
        QVERIFY2(QFile::exists(session), "本次会话文件不该被保留策略删掉");
    }

    /// 启动横幅：带上一份的头尾；同一份只报一次
    void bannerReadsPreviousReportOnce()
    {
        const QString first = core::installCrashLogger(dir(), QStringLiteral("probe"));
        for (int i = 0; i < 60; ++i) {
            qWarning().noquote() << QStringLiteral("填充-%1").arg(i);
        }
        QVERIFY(core::writeCrashReport(QStringLiteral("上一次的现场")));
        core::closeCrashLoggerCleanly();          // 报告留着
        QVERIFY(QFile::exists(first));

        // 重新"启动一次"：主目录不变，但会话文件是新的
        qunsetenv("AGENT_CRASH_KEEP");
        core::restoreCrashHandlers();
        const QString second = core::installCrashLogger(dir(), QStringLiteral("probe2"));
        QVERIFY(second != first);
        const QString banner = core::previousReportBanner();
        QVERIFY2(banner.contains(QStringLiteral("上次没有干净退出")), qPrintable(banner.left(200)));
        QVERIFY2(banner.contains(QStringLiteral("上一次的现场")), "横幅要带上一份的现场");
        QVERIFY2(banner.contains(QStringLiteral("省略")), "长报告只取头尾（中间要省略）");
        QVERIFY2(core::previousReportBanner().isEmpty(), "同一份只该报一次");

        core::closeCrashLoggerCleanly();
        QVERIFY2(QFile::exists(first), "别人的报告不该被本次收尾删掉");
    }

    /// readReportHeadTail 的边界
    void headTailIsBounded()
    {
        const QString path = QDir(dir()).filePath(QStringLiteral("fake.log"));
        QStringList lines;
        for (int i = 0; i < 100; ++i) {
            lines << QStringLiteral("LINE-%1").arg(i);
        }
        QFile file(path);
        QVERIFY(file.open(QIODevice::WriteOnly));
        file.write(lines.join(QLatin1Char('\n')).toUtf8());
        file.close();

        const QString text = core::readReportHeadTail(path, 3, 3);
        const QStringList out = text.split(QLatin1Char('\n'));
        QCOMPARE(out.size(), 7);                              // 3 + 省略 + 3
        QCOMPARE(out.first(), QStringLiteral("LINE-0"));
        QCOMPARE(out.last(), QStringLiteral("LINE-99"));
        QVERIFY(out.at(3).contains(QStringLiteral("省略")));

        // 短文件就整份给
        QFile shortFile(QDir(dir()).filePath(QStringLiteral("short.log")));
        QVERIFY(shortFile.open(QIODevice::WriteOnly));
        shortFile.write("a\nb\nc");
        shortFile.close();
        QCOMPARE(core::readReportHeadTail(QDir(dir()).filePath(QStringLiteral("short.log")), 3, 3),
                 QStringLiteral("a\nb\nc"));
    }

    /// AGENT_CRASH_DISABLE：什么都不做（连文件都不建）
    void disableEnvMakesItANoop()
    {
        qputenv("AGENT_CRASH_DISABLE", "1");
        QCOMPARE(core::installCrashLogger(dir(), QStringLiteral("probe")), QString());
        QCOMPARE(logs().size(), 0);
    }
};

QTEST_MAIN(TestCrashLog)
#include "test_crash_log.moc"
