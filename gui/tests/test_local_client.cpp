// ============================================================================
//  gui/tests/test_local_client.cpp — LocalClient 单测 (Qt5 QTest)
//
//  对端不是 mock, 而是 Python 的 local_server.py: 测试用 QProcess 把它拉起来,
//  真建一个 unix domain socket, 真收发 (与真 Agent 共用 agent/ipc/protocol.py
//  的编码)。
//
//  测试项
//  ---------------------------------------------------------------------------
//      1. receivesStatus                连接后收到 status, data 内容正确
//      2. receivesLlm                   收到 llm, data 内容正确
//      3. ignoresInvalidJson            非法 JSON 不崩、不触发任何信号
//      4. sendsCommand                  sendCommand 后 Python 侧收到正确内容
//      5. reconnectsAfterServerRestart  server 断开后重连成功 (QSignalSpy 等 1.5s)
//
//  约定
//  ---------------------------------------------------------------------------
//      · 异步等待全部走 QTRY_* / QSignalSpy::wait —— 不用 sleep
//      · 同步点: server 的 stdout (LISTENING / RECV <line>)
//      · 每个用例自己起一个 server 进程; cleanup() 里 kill + 删 socket 文件
//
//  跑法
//  ---------------------------------------------------------------------------
//      cmake -S gui -B gui/build && cmake --build gui/build -j4
//      cd gui/build && ctest --output-on-failure
// ============================================================================
#include <QtTest/QtTest>

#include <QByteArray>
#include <QCoreApplication>
#include <QFile>
#include <QJsonDocument>
#include <QJsonObject>
#include <QJsonParseError>
#include <QJsonValue>
#include <QProcess>
#include <QSignalSpy>
#include <QStandardPaths>
#include <QString>

#include "services/local_client.h"

namespace {

/// local_server.py 的绝对路径 (CMake 用 -DLOCAL_SERVER_SCRIPT=... 传进来)
const char kServerScript[] = LOCAL_SERVER_SCRIPT;

/// 与 local_server.py 里的 LLM_TEXT 保持一致
const char kLlmText[] = "单测用的 llm 推送";

constexpr int kStartTimeoutMs = 5000;   // 起 python + 等 listen
constexpr int kMsgTimeoutMs   = 3000;   // 等一条消息
constexpr int kReconnectMs    = 1500;   // 1s 重连 + 余量

} // namespace

class TestLocalClient : public QObject
{
    Q_OBJECT

private slots:
    void initTestCase();
    void init();
    void cleanup();

    void receivesStatus();
    void receivesLlm();
    void ignoresInvalidJson();
    void sendsCommand();
    void reconnectsAfterServerRestart();
    void connectionSignalFollowsSocketState();

private:
    void startServer(const QString& push);
    void killServer();
    void removeSocketFile();
    QString serverOutput() const;
    QString lastRecvLine() const;

    QProcess* server_ = nullptr;
    QByteArray serverOut_;
    QString path_;
};

// ---------------------------------------------------------------------------
//  夹具
// ---------------------------------------------------------------------------
void TestLocalClient::initTestCase()
{
    if (QStandardPaths::findExecutable(QStringLiteral("python3")).isEmpty()) {
        QSKIP("找不到 python3, 跳过 LocalClient 单测");
    }
    if (!QFile::exists(QString::fromUtf8(kServerScript))) {
        QSKIP("找不到 local_server.py (LOCAL_SERVER_SCRIPT), 跳过 LocalClient 单测");
    }

    // QSignalSpy 要求信号参数是已注册的 metatype。QJsonObject 在 Qt5 里是内建
    // metatype (id 46, 见 qmetatype.h 的静态核心类型表), 直接可用; 这里提前把
    // 4 个信号都验一遍, 万一将来参数类型变了, 报错也更直白。
    LocalClient probe;
    QVERIFY(QSignalSpy(&probe, &LocalClient::statusReceived).isValid());
    QVERIFY(QSignalSpy(&probe, &LocalClient::llmReceived).isValid());
    QVERIFY(QSignalSpy(&probe, &LocalClient::wallpaperReceived).isValid());
    QVERIFY(QSignalSpy(&probe, &LocalClient::musicReceived).isValid());
}

void TestLocalClient::init()
{
    // 每个用例一个独立 socket 路径 (带 pid, 免得并发跑时打架)
    path_ = QStringLiteral("/tmp/gui_ut_local_client_%1.sock")
                .arg(QCoreApplication::applicationPid());
    server_ = nullptr;
    serverOut_.clear();
    removeSocketFile();
}

void TestLocalClient::cleanup()
{
    killServer();
    removeSocketFile();
}

// ---------------------------------------------------------------------------
//  夹具里的工具
// ---------------------------------------------------------------------------
void TestLocalClient::startServer(const QString& push)
{
    serverOut_.clear();
    server_ = new QProcess(this);
    // stderr 也并进来: python 起不来时错误信息就在 serverOutput() 里
    server_->setProcessChannelMode(QProcess::MergedChannels);
    QObject::connect(server_, &QProcess::readyReadStandardOutput, this, [this]() {
        serverOut_ += server_->readAllStandardOutput();
    });

    server_->start(QStringLiteral("python3"),
                   {QString::fromUtf8(kServerScript),
                    QStringLiteral("--path"), path_,
                    QStringLiteral("--push"), push});
    QVERIFY2(server_->waitForStarted(kStartTimeoutMs), qPrintable(server_->errorString()));

    // 等 python 真的 bind + listen 之后再让 LocalClient 连
    QTRY_VERIFY_WITH_TIMEOUT(serverOutput().contains(QStringLiteral("LISTENING")),
                             kStartTimeoutMs);
}

void TestLocalClient::killServer()
{
    if (server_ == nullptr) {
        return;
    }
    server_->disconnect(this);   // 停掉 stdout 累积那根连接
    if (server_->state() != QProcess::NotRunning) {
        server_->kill();
        server_->waitForFinished(kStartTimeoutMs);
    }
    delete server_;
    server_ = nullptr;
}

void TestLocalClient::removeSocketFile()
{
    QFile::remove(path_);
}

QString TestLocalClient::serverOutput() const
{
    return QString::fromUtf8(serverOut_);
}

/// server 打的最后一行 "RECV <原始行>" 里的原始行
QString TestLocalClient::lastRecvLine() const
{
    const QStringList lines = serverOutput().split(QLatin1Char('\n'));
    QString last;
    for (const QString& line : lines) {
        if (line.startsWith(QStringLiteral("RECV "))) {
            last = line.mid(5);
        }
    }
    return last;
}

// ---------------------------------------------------------------------------
//  1) 连接后收到 status
// ---------------------------------------------------------------------------
void TestLocalClient::receivesStatus()
{
    startServer(QStringLiteral("status"));

    LocalClient client;
    QSignalSpy statusSpy(&client, &LocalClient::statusReceived);
    QSignalSpy llmSpy(&client, &LocalClient::llmReceived);
    QSignalSpy wallpaperSpy(&client, &LocalClient::wallpaperReceived);
    QSignalSpy musicSpy(&client, &LocalClient::musicReceived);

    client.start(path_);

    QTRY_COMPARE_WITH_TIMEOUT(statusSpy.count(), 1, kMsgTimeoutMs);

    // data 内容: {"mode":"STUDY","connected":true}
    const QJsonObject data = statusSpy.at(0).at(0).toJsonObject();
    QCOMPARE(data.value(QStringLiteral("mode")).toString(), QStringLiteral("STUDY"));
    QCOMPARE(data.value(QStringLiteral("connected")).toBool(), true);

    // server 只推了一条 status, 别的信号不该被触发
    QCOMPARE(llmSpy.count(), 0);
    QCOMPARE(wallpaperSpy.count(), 0);
    QCOMPARE(musicSpy.count(), 0);
}

// ---------------------------------------------------------------------------
//  2) 收到 llm
// ---------------------------------------------------------------------------
void TestLocalClient::receivesLlm()
{
    startServer(QStringLiteral("llm"));

    LocalClient client;
    QSignalSpy llmSpy(&client, &LocalClient::llmReceived);
    QSignalSpy statusSpy(&client, &LocalClient::statusReceived);

    client.start(path_);

    QTRY_COMPARE_WITH_TIMEOUT(llmSpy.count(), 1, kMsgTimeoutMs);
    const QJsonObject data = llmSpy.at(0).at(0).toJsonObject();
    QCOMPARE(data.value(QStringLiteral("text")).toString(), QString::fromUtf8(kLlmText));
    QCOMPARE(statusSpy.count(), 0);
}

// ---------------------------------------------------------------------------
//  3) 非法 JSON: 不崩, 不触发信号
// ---------------------------------------------------------------------------
void TestLocalClient::ignoresInvalidJson()
{
    // 先推一行非法 JSON, 再推一条合法 llm。
    // llm 到达 => 前面那行非法 JSON 一定已经被处理过了 —— 所以不用 sleep 也能
    // 断言"非法行没有触发任何信号, 也没把连接搞坏"。
    startServer(QStringLiteral("badjson,llm"));

    LocalClient client;
    QSignalSpy statusSpy(&client, &LocalClient::statusReceived);
    QSignalSpy llmSpy(&client, &LocalClient::llmReceived);
    QSignalSpy wallpaperSpy(&client, &LocalClient::wallpaperReceived);
    QSignalSpy musicSpy(&client, &LocalClient::musicReceived);

    client.start(path_);

    QTRY_COMPARE_WITH_TIMEOUT(llmSpy.count(), 1, kMsgTimeoutMs);   // 进程还活着, 后一条照样到
    QCOMPARE(statusSpy.count(), 0);
    QCOMPARE(wallpaperSpy.count(), 0);
    QCOMPARE(musicSpy.count(), 0);
}

// ---------------------------------------------------------------------------
//  4) sendCommand -> Python server 收到正确内容
// ---------------------------------------------------------------------------
void TestLocalClient::sendsCommand()
{
    startServer(QStringLiteral("status"));

    LocalClient client;
    QSignalSpy statusSpy(&client, &LocalClient::statusReceived);
    client.start(path_);
    QTRY_COMPARE_WITH_TIMEOUT(statusSpy.count(), 1, kMsgTimeoutMs);   // 先确认连上了

    const QString text = QStringLiteral("单测发送: 你好");
    client.sendCommand(QStringLiteral("chat_input"),
                       QJsonObject{{QStringLiteral("text"), text}});

    // server 把收到的每一行打成了 "RECV <原始行>"
    QTRY_VERIFY_WITH_TIMEOUT(!lastRecvLine().isEmpty(), kMsgTimeoutMs);

    QJsonParseError perr{};
    const QJsonDocument doc = QJsonDocument::fromJson(lastRecvLine().toUtf8(), &perr);
    QCOMPARE(perr.error, QJsonParseError::NoError);
    QVERIFY(doc.isObject());

    const QJsonObject cmd = doc.object();
    QCOMPARE(cmd.value(QStringLiteral("action")).toString(), QStringLiteral("chat_input"));
    const QJsonValue payload = cmd.value(QStringLiteral("payload"));
    QVERIFY(payload.isObject());
    QCOMPARE(payload.toObject().value(QStringLiteral("text")).toString(), text);
}

// ---------------------------------------------------------------------------
//  5) server 断开 -> 自动重连
// ---------------------------------------------------------------------------
void TestLocalClient::reconnectsAfterServerRestart()
{
    startServer(QStringLiteral("status"));

    LocalClient client;
    QSignalSpy statusSpy(&client, &LocalClient::statusReceived);
    client.start(path_);
    QTRY_COMPARE_WITH_TIMEOUT(statusSpy.count(), 1, kMsgTimeoutMs);   // 第一次连上

    // 模拟 Agent 退出: kill 掉进程, 并删掉它留下的 socket 文件 (协议 §1)
    killServer();
    removeSocketFile();

    // 同一个路径再起一个 server。客户端 1 秒后会重连, 连上就能再收到一条 status。
    startServer(QStringLiteral("status"));

    if (statusSpy.count() < 2) {
        QVERIFY2(statusSpy.wait(kReconnectMs), "server 重启后 1.5s 内没有重连成功");
    }
    QCOMPARE(statusSpy.count(), 2);
    QCOMPARE(statusSpy.at(1).at(0).toJsonObject()
                 .value(QStringLiteral("mode")).toString(),
             QStringLiteral("STUDY"));
}

// ---------------------------------------------------------------------------
//  6) 连接状态信号：只报变化（T4 顶栏依赖它）
// ---------------------------------------------------------------------------
void TestLocalClient::connectionSignalFollowsSocketState()
{
    startServer(QStringLiteral("status"));

    LocalClient client;
    QSignalSpy connSpy(&client, &LocalClient::connectionChanged);
    QVERIFY(connSpy.isValid());
    QCOMPARE(connSpy.count(), 0);          // 还没 start()，不该有任何上报

    QSignalSpy statusSpy(&client, &LocalClient::statusReceived);
    client.start(path_);
    QTRY_COMPARE_WITH_TIMEOUT(statusSpy.count(), 1, kMsgTimeoutMs);
    QCOMPARE(connSpy.count(), 1);
    QCOMPARE(connSpy.at(0).at(0).toBool(), true);

    // 断开：报一次 false；之后每 1 秒的重连失败**不重复报**（否则界面一直闪）
    killServer();
    removeSocketFile();
    QTRY_VERIFY_WITH_TIMEOUT(connSpy.count() >= 2, kMsgTimeoutMs);
    QCOMPARE(connSpy.at(1).at(0).toBool(), false);
    const int afterDisconnect = connSpy.count();
    QTest::qWait(kReconnectMs * 2);        // 跨过两轮重连尝试
    QCOMPARE(connSpy.count(), afterDisconnect);

    // 再起 server：连上后报一次 true
    startServer(QStringLiteral("status"));
    QTRY_VERIFY_WITH_TIMEOUT(connSpy.count() > afterDisconnect, kMsgTimeoutMs * 2);
    QCOMPARE(connSpy.last().at(0).toBool(), true);
    QCOMPARE(connSpy.count(), afterDisconnect + 1);
}

QTEST_GUILESS_MAIN(TestLocalClient)
#include "test_local_client.moc"
