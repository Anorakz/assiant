// ============================================================================
//  gui/src/services/local_client.h — GUI 侧 IPC 客户端 (Agent ⇄ GUI)
//
//  职责
//  ---------------------------------------------------------------------------
//  用 Qt 自带的 QLocalSocket 连上 Agent 监听的 Unix domain socket
//  (/tmp/agent.sock), 把字节流按 '\n' 切成一行行 NDJSON:
//
//      · 收: 解析信封 {"topic": str, "data": object, "timestamp": number},
//            按 topic 发出对应 Qt 信号 (status / llm / wallpaper / music / bilibili)
//      · 发: sendCommand() 写一行 {"action": str, "payload": object}\n
//      · 断线后固定 1 秒自动重连 (stop() 之后不再重连)
//
//  事实来源
//  ---------------------------------------------------------------------------
//      · 接收方向: docs/ipc-protocol.md   (唯一事实来源) / agent/ipc/protocol.py
//      · 发送方向: 按阶段 2 任务约定的 {"action","payload"} 线格式
//        (⚠ 与 docs/ipc-protocol.md §4 的 {topic,data,timestamp} 信封不同,
//          细节见 local_client.cpp 里 sendCommand() 的注释)
//
//  错误处理 (协议 §6: 一条坏消息只影响它自己)
//  ---------------------------------------------------------------------------
//  解析失败 / 信封字段非法 / 超长行 -> **丢弃该行 + qWarning**, 不断开连接,
//  不崩溃, 也不做任何"猜测性修复"。
//
//  断线期间发出的命令**不缓存**, 直接丢弃 + qWarning (按任务约定)。
//
//  本阶段不做 (按任务约定)
//  ---------------------------------------------------------------------------
//      · 消息队列        —— 断线期间的消息不缓存, 丢了就是丢了
//      · 指数退避        —— 固定 1 秒重连
//      · 心跳
//
//  T4 起补上「连接状态信号」
//  ---------------------------------------------------------------------------
//      connectionChanged(bool) —— 只报**状态变化**: 连上报 true; 断开/连接失败报 false;
//      同状态不重复报 (否则 1 秒一次的重连会让界面一直闪)。
//      界面据此显示 绿"已连接" / 黄"重连中"。
// ============================================================================
#pragma once

#include <QByteArray>
#include <QJsonObject>
#include <QLocalSocket>   // 需要嵌套枚举 LocalSocketError, 不能前置声明
#include <QObject>
#include <QString>

class QTimer;

class LocalClient : public QObject {
    Q_OBJECT

public:
    explicit LocalClient(QObject* parent = nullptr);
    ~LocalClient() override;

    /// 连上 path (通常是 /tmp/agent.sock), 并打开断线自动重连。
    ///
    /// @note 非阻塞: 调用后立即返回, 连接结果由 socket 信号体现。
    ///       可重复调用 —— 会先把上一次的连接收干净再重连。
    ///       调用后 stopped_ 复位为 false, 因此 stop() 之后可以再次 start()。
    void start(const QString& path);

    /// 发一条命令, 线格式 {"action": action, "payload": payload}\n。
    ///
    /// @param action  命令名 (空/全空白会被丢弃)
    /// @param payload 参数; 无参数传 {} (默认值)
    ///
    /// @note 未连接时**直接丢弃并打日志**, 不缓存、不排队 (按任务约定)。
    ///       写入后立即 flush(), 不等事件循环。
    void sendCommand(const QString& action, const QJsonObject& payload = {});

    /// 让 **Agent** 改配置真源（T14-3；线格式见 docs/ipc-protocol.md §4 的 `set_config`）。
    ///
    /// GUI **不写** config.yaml —— 唯一的写入者是 Agent（docs/adr/0005）。
    /// 回执走 `configResultReceived`（带同一个 requestId）。
    ///
    /// @param requestId    调用方给的回执 id（非空；界面靠它把回执对回这次请求）
    /// @param keys         `{点号路径: 新值}`（就是 `ConfigStore::pendingValues()`）
    /// @param credentials  可选：B 站凭据 `{SESSDATA/…}`（写进 `bilibili.cookie_file`）
    /// @return false = **没连上**（命令被丢弃），调用方要如实报"Agent 没在跑"
    bool sendSetConfig(const QString& requestId, const QJsonObject& keys,
                       const QJsonObject& credentials = {});

    /// 让 **Agent** 操作本机 WiFi（T14-9；线格式见 docs/ipc-protocol.md §4 的 `wifi_control`）。
    ///
    /// GUI 不自己调 nmcli（同 docs/adr/0005：系统动作只由板端 root 服务做）。
    /// 应答走 `wifiReceived`（`{kind: "status"|"scan"|"ack", …}`，带同一个 requestId）。
    ///
    /// @param requestId 回执 id（动作类必给；界面靠它对回自己那一次请求）
    /// @param action    status / scan / connect / forget / autoconnect / reconnect
    /// @param payload   其余字段（ssid / password / autoconnect …）
    /// @return false = **没连上**（命令被丢弃），调用方要如实报"Agent 没在跑"
    /// @note `password` 只在这里经过一次，**不会被写进任何配置文件**（Agent 侧处理）。
    bool sendWifiRequest(const QString& requestId, const QString& action,
                         const QJsonObject& payload = {});

    /// 与 Agent 的连接状态（界面在"保存"前可以先问一句）—— T14-3 起给设置页用。
    bool connected() const { return isConnected(); }

    /// 停止: 不再重连, 并断开/释放 socket。可被再次 start() 唤醒。
    void stop();

signals:
    void statusReceived(QJsonObject data);
    void llmReceived(QJsonObject data);
    void wallpaperReceived(QJsonObject data);
    void musicReceived(QJsonObject data);
    /// T15-14-a: OTA/槽状态（`{ok, current_slot, slots[], last_boot, misc_ok, misc_reason,
    /// last_ota, confirm}`，字段表见 docs/ipc-protocol.md §3）。
    /// ⚠ **只展示**：界面上不提供"开始升级"按钮 —— 升级是 root 级命令行动作。
    void otaStateReceived(QJsonObject data);
    /// T11-7: B 站队列（载荷是 `{queue[], index, current, stream, …}`，
    /// 字段表见 docs/ipc-protocol.md §3）。负载里带数组，所以不像其它 topic
    /// 那样进 ViewState，而由视频区/封面区直接消化。
    void bilibiliReceived(QJsonObject data);

    /// `set_config` 的回执（T14-3）：`{id, ok, changed, backup, path, error, llm_env{…}}`。
    /// 界面按 `id` 认领自己那一次请求。
    void configResultReceived(QJsonObject data);

    /// `llm_service` 的回执（T14-3）：`{id, ok, action, message}` —— 模型页的启停日志。
    void serviceResultReceived(QJsonObject data);

    /// `wifi` 推送（T14-9）：`{kind: "status"|"scan"|"ack", …}` —— 设置页「网络」卡片。
    void wifiReceived(QJsonObject data);

    /// 与 Agent 的连接状态发生变化（只报变化，不重复报）。T4 起给顶栏用。
    void connectionChanged(bool connected);

private slots:
    void onReadyRead();
    void onDisconnected();      ///< 触发重连
    void onErrorOccurred(QLocalSocket::LocalSocketError error);
    void tryReconnect();

private:
    /// 解析一行 (不含结尾 '\n')。合法则按 topic emit 对应信号;
    /// 否则丢弃并 qWarning, 不抛异常、不断连接。
    void processLine(const QByteArray& line);

    /// 把 sock_ 的信号接到本对象上 (start()/重连会重复使用同一个 socket 对象)
    void wireSocket();
    /// 断开信号 -> abort -> 释放 socket。先断信号是为了让 abort() 不触发重连。
    void teardownSocket();

    /// 排一次"1 秒后重连"; 已排好就不重新计时 (error 与 disconnected 常一起到)
    void scheduleReconnect();

    /// 是否处于"可发送"状态 (已连接且没被 stop())
    bool isConnected() const;

    /// 连接状态变化时才 emit connectionChanged（避免重连期刷屏）
    void notifyConnection(bool up);

    QLocalSocket* sock_ = nullptr;
    QByteArray buffer_;

    QString path_;                        ///< 保存路径供重连用
    QTimer* reconnect_timer_ = nullptr;
    bool stopped_ = false;                ///< stop() 后不重连
    bool connectedNotified_ = false;      ///< 已经把当前连接状态报给外面了吗
};
