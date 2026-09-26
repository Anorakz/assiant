// ============================================================================
//  gui/src/services/local_client.cpp — LocalClient 实现
//
//  见 local_client.h 的说明。这里只强调三件事:
//
//  1) 按行切分, 不按"读到多少字节"切分
//     readyRead 一次可能给半条、一条或三条半消息。buffer_ 累积字节, 只在
//     找到 '\n' 时才切一行出来 (docs/ipc-protocol.md §1)。
//
//  2) 接收方向校验到信封为止
//     接收 (Agent -> GUI) 的信封是 {topic, data, timestamp} (协议 §2/§6);
//     发送 (GUI -> Agent) 的是 {action, payload}, 见 sendCommand()。
//     两个方向字段名不同是协议约定, 不是笔误。
//     负载里的字段含义 (mode / text / path / title / playing …) 属于各个
//     信号接收方 (面板) 的应用层语义, 不在这一层丢消息 —— 否则同一份 schema
//     要在 client 和 panel 里各写一遍。
//
//  3) 断线重连是"固定 1 秒 + 不缓存"
//     断开或连接失败 -> 1 秒后重试, 失败就再排 1 秒 (不指数退避)。
//     重连成功之前 sendCommand() 一律丢弃, 只打日志 —— 不排队、不补发。
// ============================================================================
#include "services/local_client.h"

#include <QDebug>
#include <QJsonDocument>
#include <QJsonParseError>
#include <QJsonValue>
#include <QTimer>

namespace {

// ---- 协议常量 (照抄 agent/ipc/protocol.py / docs/ipc-protocol.md §8) ----
constexpr int kMaxLineBytes = 1 << 20;   // MAX_LINE_BYTES = 1 MiB
constexpr int kLogLineBytes = 200;       // 日志回显原始行时的截断长度

const char kTopicStatus[]    = "status";
const char kTopicLlm[]       = "llm";
const char kTopicWallpaper[] = "wallpaper";
const char kTopicMusic[]     = "music";
const char kTopicBilibili[]  = "bilibili";   // T11-7: B 站队列/当前条/缓冲状态

//: 断线后固定 1 秒重连 (按任务约定: 不做指数退避)
constexpr int kReconnectDelayMs = 1000;

/// 日志里回显一行原始内容 (截断, 避免坏消息把日志刷爆)
QString preview(const QByteArray& raw)
{
    return QString::fromUtf8(raw.left(kLogLineBytes));
}

} // namespace

// ---------------------------------------------------------------------------
//  构造 / 析构
// ---------------------------------------------------------------------------
LocalClient::LocalClient(QObject* parent)
    : QObject(parent)
{
}

LocalClient::~LocalClient()
{
    // 析构时正确清理: 挡住重连 -> 停定时器 -> 断信号/abort/释放 socket。
    // (reconnect_timer_ 是本对象的 child, 由 ~QObject 兜底删除。)
    stopped_ = true;
    if (reconnect_timer_ != nullptr) {
        reconnect_timer_->stop();
    }
    teardownSocket();
}

// ---------------------------------------------------------------------------
//  连接 / 停止
// ---------------------------------------------------------------------------
void LocalClient::start(const QString& path)
{
    if (path.isEmpty()) {
        qWarning().noquote() << "[ipc] start() 收到空路径, 忽略";
        return;
    }

    path_ = path;          // 保存下来给重连用
    stopped_ = false;      // start() 意味着"重新开始工作"

    if (reconnect_timer_ == nullptr) {
        reconnect_timer_ = new QTimer(this);
        reconnect_timer_->setSingleShot(true);   // 触发一次; 再失败会重新排
        reconnect_timer_->setInterval(kReconnectDelayMs);
        connect(reconnect_timer_, &QTimer::timeout, this, &LocalClient::tryReconnect);
    } else {
        reconnect_timer_->stop();
    }

    if (sock_ == nullptr) {
        sock_ = new QLocalSocket(this);
        wireSocket();
    } else {
        // 重复 start: 先把旧连接收干净再连。
        // 顺序很重要 —— 先断信号, abort() 才不会触发 onDisconnected 去排重连。
        sock_->disconnect(this);
        sock_->abort();
        wireSocket();
    }
    buffer_.clear();

    qInfo().noquote() << "[ipc] 连接 Agent:" << path_;
    sock_->connectToServer(path_);   // 非阻塞
}

void LocalClient::stop()
{
    stopped_ = true;
    if (reconnect_timer_ != nullptr) {
        reconnect_timer_->stop();
    }
    teardownSocket();
    qInfo().noquote() << "[ipc] 已停止 (不再重连)";
}

// ---------------------------------------------------------------------------
//  发送
// ---------------------------------------------------------------------------
void LocalClient::sendCommand(const QString& action, const QJsonObject& payload)
{
    if (action.trimmed().isEmpty()) {
        qWarning().noquote() << "[ipc] sendCommand() action 为空, 丢弃";
        return;
    }

    // 线格式: {"action": "...", "payload": {...}} + '\n'  —— **没有 timestamp**。
    //
    // ⚠ 两个方向的信封不一样, 这是协议本身的约定 (docs/ipc-protocol.md §2):
    //   命令 (本方法, GUI -> Agent) = {action, payload}
    //   推送 (handleLine, Agent -> GUI) = {topic, data, timestamp}
    //   Phase 6 D1 把 **Agent 侧统一到了 GUI 这一种** (protocol.decode_command) ——
    //   在此之前 Agent 只认 topic 形态, 于是这里发出去的每条命令都被当坏行丢掉。
    //   不要再"统一成 topic/data/timestamp": 那会把已经对齐好的两侧再拆开。
    QJsonObject envelope;
    envelope.insert(QStringLiteral("action"), action);
    envelope.insert(QStringLiteral("payload"), payload);
    const QByteArray line = QJsonDocument(envelope).toJson(QJsonDocument::Compact) + '\n';

    if (!isConnected()) {
        // 不缓存、不排队: 断线(或还没连上)期间发出的命令直接丢, 只打日志
        qWarning().noquote() << "[ipc] 未连接到 Agent, 丢弃命令:" << action
                             << preview(line);
        return;
    }

    const qint64 written = sock_->write(line);
    if (written != line.size()) {
        qWarning().noquote() << "[ipc] 写入不完整:" << written << "/" << line.size()
                             << sock_->errorString();
    }
    sock_->flush();   // 立即发出, 不等事件循环

    qInfo().noquote() << "[ipc] 发送:" << preview(line);
}

// ---------------------------------------------------------------------------
//  收数据: 累积 -> 按 '\n' 切行 -> 逐行解析
// ---------------------------------------------------------------------------
void LocalClient::onReadyRead()
{
    if (sock_ == nullptr) {
        return;
    }

    buffer_ += sock_->readAll();

    int nl = -1;
    while ((nl = buffer_.indexOf('\n')) >= 0) {
        const QByteArray line = buffer_.left(nl);
        buffer_.remove(0, nl + 1);
        processLine(line);   // 坏行只丢自己, 循环继续, 连接不受影响
    }

    // 协议 §6: 对端一直不发换行时, 缓冲迟早会吃光内存。
    // 超过上限的"半行"不可能再变成合法消息, 直接丢。
    if (buffer_.size() > kMaxLineBytes) {
        qWarning().noquote() << "[ipc] 丢弃超长未结束行:" << buffer_.size()
                             << "字节 >" << kMaxLineBytes;
        buffer_.clear();
    }
}

// ---------------------------------------------------------------------------
//  解析一行
// ---------------------------------------------------------------------------
void LocalClient::processLine(const QByteArray& line)
{
    // 空行不是消息 (协议 §6: 丢弃 + 记 warning)
    if (line.isEmpty()) {
        qWarning().noquote() << "[ipc] 丢弃空行";
        return;
    }

    // ---- 1) 是不是合法 JSON ----
    QJsonParseError perr{};
    const QJsonDocument doc = QJsonDocument::fromJson(line, &perr);
    if (perr.error != QJsonParseError::NoError) {
        qWarning().noquote() << "[ipc] 丢弃非法 JSON 行:" << preview(line)
                             << "(" << perr.errorString() << ")";
        return;
    }

    // ---- 2) 是不是 JSON object (数组/标量一律非法) ----
    if (!doc.isObject()) {
        qWarning().noquote() << "[ipc] 丢弃非 object 消息:" << preview(line);
        return;
    }
    const QJsonObject envelope = doc.object();

    // ---- 3) 信封字段 (协议 §2, 类型不对就别猜) ----
    const QJsonValue topicVal = envelope.value(QLatin1String("topic"));
    if (!topicVal.isString() || topicVal.toString().trimmed().isEmpty()) {
        qWarning().noquote() << "[ipc] 丢弃缺/非法 topic 的消息:" << preview(line);
        return;
    }

    const QJsonValue dataVal = envelope.value(QLatin1String("data"));
    // 注意: 必须显式 isObject()。直接 toObject() 会把标量静默变成空对象,
    // 等于把协议错误吞掉 (协议 §7 明确提醒)。
    if (!dataVal.isObject()) {
        qWarning().noquote() << "[ipc] 丢弃 data 不是 object 的消息:" << preview(line);
        return;
    }

    const QJsonValue tsVal = envelope.value(QLatin1String("timestamp"));
    // isDouble() 对 JSON number 成立, 对 bool / string / null 不成立。
    if (!tsVal.isDouble()) {
        qWarning().noquote() << "[ipc] 丢弃缺/非法 timestamp 的消息:" << preview(line);
        return;
    }

    // ---- 4) 按 topic 分发 ----
    const QString topic = topicVal.toString();
    const QJsonObject data = dataVal.toObject();

    if (topic == QLatin1String(kTopicStatus)) {
        emit statusReceived(data);
    } else if (topic == QLatin1String(kTopicLlm)) {
        emit llmReceived(data);
    } else if (topic == QLatin1String(kTopicWallpaper)) {
        emit wallpaperReceived(data);
    } else if (topic == QLatin1String(kTopicMusic)) {
        emit musicReceived(data);
    } else if (topic == QLatin1String(kTopicBilibili)) {
        emit bilibiliReceived(data);
    } else {
        // 协议 §6: 不认识的 topic 是"忽略", 不是错误 —— 可能对端版本更新了
        qDebug().noquote() << "[ipc] 忽略未知 topic:" << topic;
    }
}

// ---------------------------------------------------------------------------
//  连接状态: 断开 -> 1 秒后重连
// ---------------------------------------------------------------------------
void LocalClient::onDisconnected()
{
    // 断线时残留的半行是垃圾, 丢掉 —— 别和新连接的数据拼在一起
    if (!buffer_.isEmpty()) {
        qWarning().noquote() << "[ipc] 连接断开, 丢弃未结束的残行:"
                             << preview(buffer_) << "(" << buffer_.size() << "字节)";
        buffer_.clear();
    }
    qWarning().noquote() << "[ipc] 与 Agent 的连接已断开";
    notifyConnection(false);
    scheduleReconnect();
}

void LocalClient::onErrorOccurred(QLocalSocket::LocalSocketError error)
{
    qWarning().noquote() << "[ipc] socket 错误:" << static_cast<int>(error)
                         << (sock_ != nullptr ? sock_->errorString() : QString());
    // 连接阶段的失败 (Agent 还没起来 / socket 文件不存在) 也走同一条重连路径,
    // 否则 GUI 比 Agent 先启动时就永远连不上了。
    notifyConnection(false);
    scheduleReconnect();
}

void LocalClient::scheduleReconnect()
{
    if (stopped_ || path_.isEmpty() || reconnect_timer_ == nullptr) {
        return;   // stop() 过 / 没 start() 过 -> 不重连
    }
    if (reconnect_timer_->isActive()) {
        return;   // 已经排好了: 不重新计时 (error 和 disconnected 常常一起到)
    }
    qInfo().noquote() << "[ipc]" << kReconnectDelayMs << "ms 后重连" << path_;
    reconnect_timer_->start();
}

void LocalClient::tryReconnect()
{
    if (stopped_ || path_.isEmpty() || sock_ == nullptr) {
        return;
    }

    if (sock_->state() != QLocalSocket::UnconnectedState) {
        // 还在 ConnectingState: 这一次不算数, 再排一轮, 否则重连就断链了。
        // 已经 ConnectedState (迟到的定时器): 什么都不用做。
        if (sock_->state() == QLocalSocket::ConnectingState) {
            scheduleReconnect();
        }
        return;
    }

    qInfo().noquote() << "[ipc] 重连" << path_;
    buffer_.clear();
    sock_->connectToServer(path_);
}

// ---------------------------------------------------------------------------
//  内部
// ---------------------------------------------------------------------------
void LocalClient::wireSocket()
{
    connect(sock_, &QLocalSocket::readyRead, this, &LocalClient::onReadyRead);
    connect(sock_, &QLocalSocket::disconnected, this, &LocalClient::onDisconnected);
#if QT_VERSION >= QT_VERSION_CHECK(5, 15, 0)
    connect(sock_, &QLocalSocket::errorOccurred, this, &LocalClient::onErrorOccurred);
#else
    // Qt < 5.15 (板端是 5.12): 信号叫 error(), 与同名 getter error() 重载,
    // 必须用 QOverload 指定要取的是信号。
    connect(sock_,
            QOverload<QLocalSocket::LocalSocketError>::of(&QLocalSocket::error),
            this, &LocalClient::onErrorOccurred);
#endif
    // 连上时记一条日志, 并把连接状态报给界面（T4 起）。
    connect(sock_, &QLocalSocket::connected, this, [this]() {
        qInfo().noquote() << "[ipc] 已连接:" << path_;
        notifyConnection(true);
    });
}

void LocalClient::notifyConnection(bool up)
{
    if (up == connectedNotified_) {
        return;   // 状态没变: 重连期每次失败都报会让界面闪
    }
    connectedNotified_ = up;
    emit connectionChanged(up);
}

void LocalClient::teardownSocket()
{
    buffer_.clear();
    if (sock_ == nullptr) {
        return;
    }
    // 先断信号: 免得 abort() 触发 onDisconnected 又排一次重连。
    // sock_ 是本对象的 child (QObject 也会删它), 这里提前 delete 也没问题:
    // QObject 析构会把它从 children 列表里摘掉, 不会二次释放。
    sock_->disconnect(this);
    sock_->abort();
    delete sock_;
    sock_ = nullptr;
}

bool LocalClient::isConnected() const
{
    return !stopped_ && sock_ != nullptr
           && sock_->state() == QLocalSocket::ConnectedState;
}
