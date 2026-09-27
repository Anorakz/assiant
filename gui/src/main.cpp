// ============================================================================
//  gui/src/main.cpp — 程序入口（T1：从终端验收程序升级为 Qt5 Widgets 应用）
//
//  两种模式
//  ---------------------------------------------------------------------------
//    GUI（默认）  QApplication + MainWindow；可选 --windowed / --page / --screenshot
//    --stdio      保持 T1 之前的纯终端行为（QCoreApplication，无窗口）
//
//  ⚠ 两种模式都保留"终端桥"（stdin 命令 + [recv] 日志）
//  ---------------------------------------------------------------------------
//  gui/tests/e2e_ipc.py 用 `agent_gui <socket>`（positional）+ QT_QPA_PLATFORM=offscreen
//  拉起本程序，等日志 "[ipc] 已连接"（LocalClient 打），然后用 stdin 发 @switch_mode，
//  并断言日志里有 "[recv] status"。所以加了窗口之后这套行为**必须原样保留**，
//  测试脚本一行都不用改（见该脚本头部注释的预期）。
//
//  终端输入两种写法（沿用 T1 之前）
//  ---------------------------------------------------------------------------
//      普通文本              -> sendCommand("chat_input", {"text": ...})
//      @<action> <json对象>  -> sendCommand(<action>, <json对象>)
//
//  用法
//  ---------------------------------------------------------------------------
//      ./agent_gui                                  # 全屏 GUI，连 /tmp/agent.sock
//      ./agent_gui --windowed --page system         # 窗口模式 + 直接打开某页
//      ./agent_gui --screenshot /tmp/x.png          # 渲染完成后自截图并退出（验收用）
//      ./agent_gui --stdio /tmp/agent.sock          # 纯终端模式（回归 / 联调）
// ============================================================================
#include <QApplication>
#include <QByteArray>
#include <QCoreApplication>
#include <QDebug>
#include <QDir>
#include <QFile>
#include <QJsonDocument>
#include <QJsonObject>
#include <QLineEdit>
#include <cstdio>
#include <QCheckBox>          // T13-10: --settings-*-demo / --settings-dump-cards 要读勾选框
#include <QDoubleSpinBox>
#include <QLabel>
#include <QLineEdit>
#include <QMouseEvent>
#include <QObject>
#include <QScrollArea>
#include <QScrollBar>
#include <QSocketNotifier>
#include <QString>
#include <QDir>
#include <QFileInfo>
#include <QPushButton>
#include <QRadioButton>
#include <QSpinBox>
#include <QTimer>
#include <QToolButton>

#include <unistd.h>   // STDIN_FILENO / read

#include "core/config_store.h"
#include "core/idle_watcher.h"
#include "main_window.h"
#include "ui/schedule_panel.h"
#include "services/local_client.h"
#include "ui/chat_panel.h"
#include "ui/bottom_bar.h"
#include "ui/model_page.h"
#include "ui/settings_page.h"
#include "ui/music_bar.h"
#include "ui/sys_page.h"

namespace {

/// T14-7b 取证：递归打印控件树的真实尺寸（定义在文件末尾，这里先声明以便 GUI 分支调用）。
void dumpLayoutTree(QWidget* widget, int depth = 0);

/// 命令行解析结果。
struct Options {
    bool stdio = false;            ///< 纯终端模式
    bool windowed = false;         ///< GUI: 窗口而非全屏
    QString page = QStringLiteral("home");
    QString socketPath = QStringLiteral("/tmp/agent.sock");
    QString screenshot;            ///< 非空 = 渲染后自截图并退出
    int screenshotDelayMs = 1500;
    QString screenshotSeq;         ///< 非空 = 产出"可见/折叠/唤醒"三连图并退出（验收用）
    QString configPath;            ///< config/config.yaml 路径（空 = 自动找）
    int idleMs = -1;               ///< >=0 时覆盖 wake.idle_ms
    bool debug = false;            ///< 强制打开 debug（覆盖配置，只改内存）
    QString chatDemo;              ///< 非空 = 启动后走真实控件发这条消息（验收用）
    QString inputTypeDemo;         ///< 非空 = 启动后切到这个输入源（验收用）
    bool inputMenuDemo = false;    ///< 启动后展开输入源菜单（配合 scrot 抓图）
    QString musicNoteDemo;         ///< 非空 = 启动后触发音乐条某个占位说明（验收用）
    bool nextBilibiliDemo = false;  ///< 启动后点一下视频区"下一集"（验收用）
    bool prevBilibiliDemo = false;  ///< 启动后点一下视频区"上一集"（验收用）
    int bilibiliPickDemo = -1;      ///< >=0 时点一下预览栏第 N 格（验收用）
    bool videoPlayDemo = false;     ///< 启动后切一次播放/暂停（验收用）
    int videoPauseDemoMs = 0;       ///< >0 时过这么多毫秒点一次播放/暂停（T11-9: 验暂停预取）
QString modeDemo;               ///< 非空 = 点一下指向该模式的按钮（T12-3: SLEEP/IDLE/STUDY/GAME）
int modeDemoMs = 3000;          ///< 上面那一下在启动后多久点（默认 3 秒, 留够连上 Agent 的时间）
    bool videoFullscreenDemo = false; ///< 启动后切一次全屏（验收用）
    bool watchdogDemo = false;      ///< 启动后点一下看门狗（验收用）
    QString modelModeDemo;         ///< 非空 = 切到该推理位置（验收用）
    bool modelSaveDemo = false;     ///< 启动后点"保存并同步"（验收用）
    bool modelStartDemo = false;    ///< 启动后点"启动服务"（验收用）
    QString benchDemo;             ///< 非空 = 启动后跑该基准（验收用）
    bool benchStopDemo = false;     ///< 启动后点"停止测试"（验收用）
    bool reportDemo = false;        ///< 启动后点"查看最新报告"（验收用）
    bool settingsSaveDemo = false;  ///< 启动后改两个时间并保存（验收用）
    bool settingsCardsDemo = false; ///< 启动后把三张卡片设成"有区分度"的值并保存（T13-10 验收用）
    bool settingsFinalDemo = false; ///< 启动后把三张卡片设成约定值并保存（T13-10 收尾用）
    bool settingsOneKeyDemo = false; ///< 启动后改一个键并保存（T14-3 端到端验收用）
    int settingsScrollDemo = -1;    ///< >=0 = 启动后把设置页滚到该像素再截图（T13-10 取证用）
    bool settingsDumpCards = false; ///< 打印三张卡片**当前读到的值**并退出（T13-10 取证用）
    bool dumpLayout = false;        ///< 打印控件树真实尺寸/最小尺寸并退出（T14-7b 取证用）
    bool dumpSchedule = false;      ///< 打印日程区**真实渲染出来的行**并退出（S8 取证用）
    bool focusInputDemo = false;    ///< 启动后把焦点给对话输入框（S10 取证：软键盘应这时才弹）
    QString videoNoteDemo;         ///< 非空 = 触发视频区占位说明（验收用）
    QString videoFile;             ///< 非空 = 主区视频源（本地文件，验收用）
    bool help = false;
    QString error;
};

void printUsage()
{
    const char* text =
        "用法: agent_gui [选项] [socket路径]\n"
        "\n"
        "  --stdio              纯终端模式（无窗口；回归 / 联调用）\n"
        "  --windowed           GUI 用窗口显示（默认全屏 kiosk）\n"
        "  --page <key>         启动后打开指定页: home|model|system|settings\n"
        "  --screenshot <png>   渲染完成后把窗口存成 PNG 并退出（验收用）\n"
        "  --screenshot-delay <ms>  截图前等待毫秒数（默认 1500）\n"
        "  --screenshot-seq <dir>   产出 01_visible/02_idle/03_woke 三连图并退出（T3 验收用）\n"
        "  --idle-ms <ms>       覆盖 wake.idle_ms（验收时把 5s 缩短）\n"
        "  --debug              强制打开 debug（覆盖配置，只改内存）\n"
        "  --chat-demo <文本>   启动后走真实输入框+发送按钮发一条（验收用）\n"
        "  --input-type-demo <terminal|keyboard>  启动后切到该输入源（验收用）\n"
        "  --input-menu-demo    启动后展开输入源菜单（配 --scrot 抓图）\n"
        "  --music-note-demo <歌词|歌手|专辑|进度>  触发音乐条占位说明（验收用）\n"
        "  --video <文件>       主区视频源（本地文件；验收用）\n"
        "  --next-bilibili-demo 启动后点一下视频区「下一集」（验收用）\n"
        "  --prev-bilibili-demo 启动后点一下视频区「上一集」（验收用）\n"
        "  --bilibili-pick-demo <n>  启动后点一下预览栏第 n 格（0 起，验收用）\n"
        "  --video-play-demo    启动后切一次播放/暂停（验收用）\n"
        "  --video-pause-demo <ms>  过这么多毫秒点一次播放/暂停（验收「暂停预取」用）\n"
        "  --mode-demo <模式>       启动后点一下模式区里指向该模式的按钮（SLEEP/IDLE/STUDY/GAME，验收用）\n"
        "  --mode-demo-ms <ms>      上面那一下在启动后多久点（默认 3000）\n"
        "  --video-fullscreen-demo 启动后切一次全屏（覆盖整屏，验收用）\n"
        "  --watchdog-demo     启动后点一下看门狗（验收用）\n"
        "  --model-mode-demo <local|cloud|disabled>  切到该推理位置（验收用）\n"
        "  --model-save-demo   启动后点「保存并同步」（验收用）\n"
        "  --model-start-demo  启动后点「启动服务」（验收用）\n"
        "  --bench-demo <qwen_precheck|qwen_full|multimodal>  启动后跑该基准（验收用）\n"
        "  --bench-stop-demo   启动后点「停止测试」（验收用）\n"
        "  --report-demo       启动后点「查看最新报告」（验收用）\n"
        "  --settings-save-demo 启动后改两个休眠时间并保存（验收用）\n"
        "  --settings-cards-demo    启动后把三张卡片设成有区分度的值并保存（T13-10 验收用）\n"
        "  --settings-final-demo    启动后把三张卡片设成约定值并保存（T13-10 收尾用）\n"
        "  --settings-one-key-demo  启动后改一个键并保存（T14-3 端到端验收：走 IPC 让 Agent 写）\n"
        "  --settings-scroll-demo <px>  启动后把设置页滚到该像素再截图（T13-10 取证用）\n"
        "  --settings-dump-cards    打印三张卡片当前读到的值并退出（T13-10 取证用）\n"
        "  --dump-schedule      打印日程区真实渲染出来的行并退出（取证用）\n"
        "  --dump-layout        打印整棵控件树的真实尺寸/最小尺寸并退出（T14-7b 取证用，配 --screenshot-delay）\n"
        "  --focus-input-demo   启动后把焦点给对话输入框（取证：软键盘应这时才弹）\n"        "  --config <path>      指定 config/config.yaml（默认自动在仓库里找）\n"
        "  --socket <path>      Agent 的 unix socket 路径（默认 /tmp/agent.sock）\n"
        "  -h, --help           显示本帮助\n";
    qInfo().noquote() << QString::fromUtf8(text);
}

/// 取 "选项值"：支持 `--k v` 与 `--k=v` 两种写法。
QString optionValue(const QString& arg, const QString& name, int& i, int argc, char** argv,
                    Options& opt)
{
    if (arg.startsWith(name + QLatin1Char('='))) {
        return arg.mid(name.size() + 1);
    }
    if (i + 1 < argc) {
        return QString::fromLocal8Bit(argv[++i]);
    }
    opt.error = QStringLiteral("%1 缺少参数").arg(name);
    return QString();
}

Options parseArgs(int argc, char** argv)
{
    Options opt;
    bool positionalUsed = false;
    for (int i = 1; i < argc; ++i) {
        const QString arg = QString::fromLocal8Bit(argv[i]);
        if (arg == QLatin1String("--stdio")) {
            opt.stdio = true;
        } else if (arg == QLatin1String("--windowed")) {
            opt.windowed = true;
        } else if (arg == QLatin1String("-h") || arg == QLatin1String("--help")) {
            opt.help = true;
        } else if (arg == QLatin1String("--page") || arg.startsWith(QLatin1String("--page="))) {
            opt.page = optionValue(arg, QStringLiteral("--page"), i, argc, argv, opt);
        } else if (arg == QLatin1String("--socket") || arg.startsWith(QLatin1String("--socket="))) {
            opt.socketPath = optionValue(arg, QStringLiteral("--socket"), i, argc, argv, opt);
        } else if (arg == QLatin1String("--screenshot")
                   || arg.startsWith(QLatin1String("--screenshot="))) {
            opt.screenshot = optionValue(arg, QStringLiteral("--screenshot"), i, argc, argv, opt);
        } else if (arg == QLatin1String("--screenshot-seq")
                   || arg.startsWith(QLatin1String("--screenshot-seq="))) {
            opt.screenshotSeq = optionValue(arg, QStringLiteral("--screenshot-seq"), i, argc, argv,
                                            opt);
        } else if (arg == QLatin1String("--config")
                   || arg.startsWith(QLatin1String("--config="))) {
            opt.configPath = optionValue(arg, QStringLiteral("--config"), i, argc, argv, opt);
        } else if (arg == QLatin1String("--debug")) {
            opt.debug = true;
        } else if (arg == QLatin1String("--chat-demo")
                   || arg.startsWith(QLatin1String("--chat-demo="))) {
            opt.chatDemo = optionValue(arg, QStringLiteral("--chat-demo"), i, argc, argv, opt);
        } else if (arg == QLatin1String("--input-type-demo")
                   || arg.startsWith(QLatin1String("--input-type-demo="))) {
            opt.inputTypeDemo =
                optionValue(arg, QStringLiteral("--input-type-demo"), i, argc, argv, opt);
        } else if (arg == QLatin1String("--input-menu-demo")) {
            opt.inputMenuDemo = true;
        } else if (arg == QLatin1String("--music-note-demo")
                   || arg.startsWith(QLatin1String("--music-note-demo="))) {
            opt.musicNoteDemo =
                optionValue(arg, QStringLiteral("--music-note-demo"), i, argc, argv, opt);
        } else if (arg == QLatin1String("--video") || arg.startsWith(QLatin1String("--video="))) {
            opt.videoFile = optionValue(arg, QStringLiteral("--video"), i, argc, argv, opt);
        } else if (arg == QLatin1String("--next-bilibili-demo")) {
            opt.nextBilibiliDemo = true;
        } else if (arg == QLatin1String("--prev-bilibili-demo")) {
            opt.prevBilibiliDemo = true;
        } else if (arg == QLatin1String("--bilibili-pick-demo")
                   || arg.startsWith(QLatin1String("--bilibili-pick-demo="))) {
            const QString value =
                optionValue(arg, QStringLiteral("--bilibili-pick-demo"), i, argc, argv, opt);
            bool ok = false;
            const int index = value.toInt(&ok);
            if (!ok || index < 0) {
                opt.error = QStringLiteral("--bilibili-pick-demo 需要一个 >=0 的整数");
            } else {
                opt.bilibiliPickDemo = index;
            }
        } else if (arg == QLatin1String("--video-play-demo")) {
            opt.videoPlayDemo = true;
        } else if (arg == QLatin1String("--video-pause-demo")
                   || arg.startsWith(QLatin1String("--video-pause-demo="))) {
            const QString value =
                optionValue(arg, QStringLiteral("--video-pause-demo"), i, argc, argv, opt);
            bool ok = false;
            const int ms = value.toInt(&ok);
            if (!ok || ms <= 0) {
                opt.error = QStringLiteral("--video-pause-demo 需要一个 >0 的毫秒数");
            } else {
                opt.videoPauseDemoMs = ms;
            }
        } else if (arg == QLatin1String("--video-fullscreen-demo")) {
            opt.videoFullscreenDemo = true;
        } else if (arg == QLatin1String("--mode-demo")
                   || arg.startsWith(QLatin1String("--mode-demo="))) {
            const QString value =
                optionValue(arg, QStringLiteral("--mode-demo"), i, argc, argv, opt);
            const QString target = value.trimmed().toUpper();
            // 四个模式是协议 §3 的全集（与 core::ViewState::modes() 同一份口径）
            if (target != QLatin1String("SLEEP") && target != QLatin1String("IDLE")
                && target != QLatin1String("STUDY") && target != QLatin1String("GAME")) {
                opt.error = QStringLiteral("--mode-demo 只认 SLEEP/IDLE/STUDY/GAME");
            } else {
                opt.modeDemo = target;
            }
        } else if (arg == QLatin1String("--mode-demo-ms")
                   || arg.startsWith(QLatin1String("--mode-demo-ms="))) {
            const QString value =
                optionValue(arg, QStringLiteral("--mode-demo-ms"), i, argc, argv, opt);
            bool ok = false;
            const int ms = value.toInt(&ok);
            if (!ok || ms <= 0) {
                opt.error = QStringLiteral("--mode-demo-ms 需要一个 >0 的毫秒数");
            } else {
                opt.modeDemoMs = ms;
            }
        } else if (arg == QLatin1String("--watchdog-demo")) {
            opt.watchdogDemo = true;
        } else if (arg == QLatin1String("--model-mode-demo")
                   || arg.startsWith(QLatin1String("--model-mode-demo="))) {
            opt.modelModeDemo =
                optionValue(arg, QStringLiteral("--model-mode-demo"), i, argc, argv, opt);
        } else if (arg == QLatin1String("--model-save-demo")) {
            opt.modelSaveDemo = true;
        } else if (arg == QLatin1String("--model-start-demo")) {
            opt.modelStartDemo = true;
        } else if (arg == QLatin1String("--bench-demo")
                   || arg.startsWith(QLatin1String("--bench-demo="))) {
            opt.benchDemo = optionValue(arg, QStringLiteral("--bench-demo"), i, argc, argv, opt);
        } else if (arg == QLatin1String("--bench-stop-demo")) {
            opt.benchStopDemo = true;
        } else if (arg == QLatin1String("--report-demo")) {
            opt.reportDemo = true;
        } else if (arg == QLatin1String("--settings-save-demo")) {
            opt.settingsSaveDemo = true;
        } else if (arg == QLatin1String("--settings-cards-demo")) {
            opt.settingsCardsDemo = true;
        } else if (arg == QLatin1String("--settings-final-demo")) {
            opt.settingsFinalDemo = true;
        } else if (arg == QLatin1String("--settings-one-key-demo")) {
            opt.settingsOneKeyDemo = true;
        } else if (arg == QLatin1String("--settings-dump-cards")) {
            opt.settingsDumpCards = true;
        } else if (arg == QLatin1String("--settings-scroll-demo")
                   || arg.startsWith(QLatin1String("--settings-scroll-demo="))) {
            const QString value =
                optionValue(arg, QStringLiteral("--settings-scroll-demo"), i, argc, argv, opt);
            bool ok = false;
            const int px = value.toInt(&ok);
            if (!ok || px < 0) {
                opt.error = QStringLiteral("--settings-scroll-demo 需要非负整数（像素）");
            } else {
                opt.settingsScrollDemo = px;
            }
        } else if (arg == QLatin1String("--dump-schedule")) {
            opt.dumpSchedule = true;
        } else if (arg == QLatin1String("--dump-layout")) {
            opt.dumpLayout = true;
        } else if (arg == QLatin1String("--focus-input-demo")) {
            opt.focusInputDemo = true;
        } else if (arg == QLatin1String("--video-note-demo")
                   || arg.startsWith(QLatin1String("--video-note-demo="))) {
            opt.videoNoteDemo =
                optionValue(arg, QStringLiteral("--video-note-demo"), i, argc, argv, opt);
        } else if (arg == QLatin1String("--idle-ms")
                   || arg.startsWith(QLatin1String("--idle-ms="))) {
            const QString value = optionValue(arg, QStringLiteral("--idle-ms"), i, argc, argv, opt);
            bool ok = false;
            const int ms = value.toInt(&ok);
            if (!ok || ms < 0) {
                opt.error = QStringLiteral("--idle-ms 需要非负整数");
            } else {
                opt.idleMs = ms;
            }
        } else if (arg == QLatin1String("--screenshot-delay")
                   || arg.startsWith(QLatin1String("--screenshot-delay="))) {
            const QString value = optionValue(arg, QStringLiteral("--screenshot-delay"), i, argc,
                                              argv, opt);
            bool ok = false;
            const int ms = value.toInt(&ok);
            if (!ok || ms < 0) {
                opt.error = QStringLiteral("--screenshot-delay 需要非负整数");
            } else {
                opt.screenshotDelayMs = ms;
            }
        } else if (!arg.startsWith(QLatin1Char('-')) && !positionalUsed) {
            opt.socketPath = arg;      // 兼容 T1 之前的 `agent_gui <socket>` 用法
            positionalUsed = true;
        } else {
            opt.error = QStringLiteral("未知参数: %1").arg(arg);
        }
        if (!opt.error.isEmpty()) {
            return opt;
        }
    }
    return opt;
}

void printReceived(const char* topic, const QJsonObject& data)
{
    const QByteArray json = QJsonDocument(data).toJson(QJsonDocument::Compact);
    qInfo().noquote() << "[recv]" << topic << QString::fromUtf8(json);
}

/// 把 LocalClient 的四个信号接到打印上（终端桥的"收"方向）。
void wirePrinters(LocalClient* client)
{
    QObject::connect(client, &LocalClient::statusReceived, client,
                     [](const QJsonObject& d) { printReceived("status", d); });
    QObject::connect(client, &LocalClient::llmReceived, client,
                     [](const QJsonObject& d) { printReceived("llm", d); });
    QObject::connect(client, &LocalClient::wallpaperReceived, client,
                     [](const QJsonObject& d) { printReceived("wallpaper", d); });
    QObject::connect(client, &LocalClient::musicReceived, client,
                     [](const QJsonObject& d) { printReceived("music", d); });
}

/// stdin 命令通道（终端桥的"发"方向）。
///
/// 直接 ::read(0, ...) 自己按 '\n' 切，不走 iostream/stdio —— 它们的用户态缓冲
/// 可能一次把好几行吞进去，之后 fd 上没数据，QSocketNotifier 就不再触发。
///
/// 不派生 Q_OBJECT（不需要信号槽），只用 lambda + QObject 上下文做生命周期绑定。
class StdinBridge : public QObject {
public:
    StdinBridge(LocalClient* client, QObject* parent)
        : QObject(parent), client_(client)
    {
        notifier_ = new QSocketNotifier(STDIN_FILENO, QSocketNotifier::Read, this);
        connect(notifier_, &QSocketNotifier::activated, this, [this]() { handleInput(); });
    }

private:
    void handleInput()
    {
        char chunk[512];
        const ssize_t got = ::read(STDIN_FILENO, chunk, sizeof(chunk));
        if (got <= 0) {   // EOF (Ctrl-D / 管道关闭)
            notifier_->setEnabled(false);
            qInfo().noquote() << "[gui] stdin 结束 (仍继续接收)";
            return;
        }
        buffer_.append(chunk, static_cast<int>(got));

        int nl = -1;
        while ((nl = buffer_.indexOf('\n')) >= 0) {
            const QByteArray line = buffer_.left(nl).trimmed();
            buffer_.remove(0, nl + 1);
            if (line.isEmpty()) {
                continue;
            }
            if (!line.startsWith('@')) {
                client_->sendCommand(QStringLiteral("chat_input"),
                                     QJsonObject{{QStringLiteral("text"),
                                                  QString::fromUtf8(line)}});
                continue;
            }
            const int sp = line.indexOf(' ');
            const QString action = QString::fromUtf8(
                (sp < 0) ? line.mid(1) : line.mid(1, sp - 1));
            const QByteArray rawPayload = (sp < 0) ? QByteArray("{}")
                                                   : line.mid(sp + 1).trimmed();
            QJsonParseError perr{};
            const QJsonDocument pdoc = QJsonDocument::fromJson(rawPayload, &perr);
            if (perr.error != QJsonParseError::NoError || !pdoc.isObject()) {
                qWarning().noquote() << "[gui] 命令 payload 不是合法 JSON object, 丢弃:"
                                     << rawPayload;
                continue;
            }
            client_->sendCommand(action, pdoc.object());
        }
    }

    LocalClient* client_ = nullptr;
    QSocketNotifier* notifier_ = nullptr;
    QByteArray buffer_;
};

/// 纯终端模式（T1 之前的行为，原样保留）。
int runStdioMode(const Options& opt, int argc, char** argv)
{
    QCoreApplication app(argc, argv);
    QCoreApplication::setApplicationName(QStringLiteral("agent_gui"));
    QCoreApplication::setApplicationVersion(QStringLiteral("0.3.0"));

    LocalClient client;
    wirePrinters(&client);
    new StdinBridge(&client, &app);

    qInfo().noquote() << "[gui] stdio 模式, 连接:" << opt.socketPath
                      << "(敲字回车发命令, Ctrl-C 退出)";
    client.start(opt.socketPath);
    return app.exec();
}

/// 找 config/config.yaml：从可执行文件所在目录往上找。
/// 板端布局是 <repo>/gui/build/agent_gui + <repo>/config/config.yaml。
///
/// ⚠ 归一化 D 系列之前这里找的是 gui/config/gui.yaml（GUI 自己的配置）。
///    现在配置真源只有一份 config/config.yaml —— GUI 读写它的 `gui:` 段，
///    模型页读写顶层 `llm:` 段。
QString resolveConfig()
{
    QDir dir(QCoreApplication::applicationDirPath());
    for (int depth = 0; depth < 4; ++depth) {
        const QString candidate = dir.absoluteFilePath(QStringLiteral("config/config.yaml"));
        if (QFile::exists(candidate)) {
            return candidate;
        }
        if (!dir.cdUp()) {
            break;
        }
    }
    return QString();
}

/// T3 验收用：产出三连图
///   01_visible.png  全部区域可见
///   02_idle.png     空闲达 idle_ms 后"活动"区域折叠（锁定的仍在）
///   03_woke.png     往窗口塞一次真实鼠标按压（走 app 事件过滤器，与手点同一条路径）后恢复
/// 同时打印折叠状态与"当前页"以证明折叠的导航**没有拦到**那次点击。
void runScreenshotSequence(MainWindow* window, QApplication* app, const QString& dir)
{
    QDir().mkpath(dir);
    const int idleMs = window->idleWatcher()->idleMs();
    const auto shot = [window, dir](const QString& name) {
        const QString path = dir + QLatin1Char('/') + name;
        const bool ok = window->grab().save(path);
        qInfo().noquote() << "[gui] 截图" << (ok ? QStringLiteral("成功:") : QStringLiteral("失败:"))
                          << path;
    };

    QTimer::singleShot(900, window, [window, shot]() {
        qInfo().noquote() << "[gui] t=0.9s 初始:" << window->wakeStateText();
        shot(QStringLiteral("01_visible.png"));
    });

    const int idleAt = 900 + idleMs + 500;
    QTimer::singleShot(idleAt, window, [window, shot]() {
        qInfo().noquote() << QStringLiteral("[gui] 空闲 %1ms 后:").arg(window->idleWatcher()->idleMs())
                          << window->wakeStateText();
        shot(QStringLiteral("02_idle.png"));

        const QString before = window->currentPage();
        const QPointF at(10, 400);   // 原导航栏所在位置
        QMouseEvent press(QEvent::MouseButtonPress, at, Qt::LeftButton, Qt::LeftButton,
                          Qt::NoModifier);
        QCoreApplication::sendEvent(window, &press);
        QMouseEvent release(QEvent::MouseButtonRelease, at, Qt::LeftButton, Qt::NoButton,
                            Qt::NoModifier);
        QCoreApplication::sendEvent(window, &release);
        qInfo().noquote() << "[gui] 点击原导航位置(10,400) 前/后当前页:" << before << "/"
                          << window->currentPage()
                          << "(相同 => 折叠的导航没拦到这次点击)";
    });

    QTimer::singleShot(idleAt + 600, window, [window, shot, app]() {
        qInfo().noquote() << "[gui] 唤醒后:" << window->wakeStateText();
        shot(QStringLiteral("03_woke.png"));
        app->quit();
    });
}

/// GUI 模式。
int runGuiMode(const Options& opt, int argc, char** argv)
{
    QApplication app(argc, argv);
    QApplication::setApplicationName(QStringLiteral("agent_gui"));
    QApplication::setApplicationVersion(QStringLiteral("0.3.0"));

    MainWindow window;

    // 终端桥接到**主窗口持有的**客户端：GUI 与 --stdio 两种模式的 IPC 行为一致
    // （e2e_ipc 用 `agent_gui <socket>` + offscreen 拉起本程序，依赖 [recv] 日志与 stdin 命令）
    LocalClient* client = window.client();
    wirePrinters(client);
    new StdinBridge(client, &app);

    // ---- 配置：config/config.yaml 的 gui.*（四区域/统一休眠/输入源/…）+ debug ----
    const QString cfgPath = opt.configPath.isEmpty() ? resolveConfig() : opt.configPath;
    core::ConfigStore gui;
    if (!cfgPath.isEmpty()) {
        QString cfgError;
        if (gui.load(cfgPath, &cfgError)) {
            qInfo().noquote() << "[gui] 配置:" << cfgPath;
        } else {
            qWarning().noquote() << "[gui] 配置读取失败:" << cfgError << "→ 用默认值";
        }
    } else {
        qWarning().noquote() << "[gui] 没找到 config/config.yaml → 用默认配置";
    }
    if (opt.debug) {
        // 命令行强制打开（只改内存，不落盘）
        gui.setBool(QStringLiteral("gui.debug"), true);
    }
    window.setConfigPath(cfgPath);                      // 输入源切换要写回它
    // T11：仓库根 = config/config.yaml 的上一级（配置同步与 llm/scripts 相对它定位）
    if (!cfgPath.isEmpty()) {
        const QString root = QFileInfo(cfgPath).absolutePath() + QStringLiteral("/..");
        window.setRepoRoot(QDir(root).absolutePath());
    }
    window.applyConfig(gui);

    // S8 取证：打印日程区**真实渲染出来的行**（与界面同源：同一个 SchedulePanel），
    // 让"GUI 显示 vs Agent 展开"能做逐行比对，而不是靠人眼读截图。
    // 输出走 stdout（Qt 的日志在 stderr），格式：SUBTITLE/SECTION/ROW/NOTE + TAB + 文本。
    // SECTION 是两段的表头（"今天 · 3 项"）—— 让"今天 0 项 / 明天 3 项"这种结构也进得了证据。
    if (opt.dumpSchedule) {
        SchedulePanel* panel = window.schedulePanel();
        if (panel == nullptr) {
            qWarning().noquote() << "[dump] 没有日程区";
            return 2;
        }
        std::printf("SUBTITLE\t%s\n", qPrintable(panel->subtitleText()));
        for (const QString& header : panel->sectionHeaders()) {
            std::printf("SECTION\t%s\n", qPrintable(header));   // "今天 · 3 项"
        }
        const QStringList rows = panel->rowTexts();
        for (int i = 0; i < rows.size(); ++i) {
            // 尾巴里的行（已过）标一下：不然"最近 30 分钟"那条在证据里看不出来
            std::printf("ROW\t%s%s\n", panel->isRowPast(i) ? "[已过] " : "",
                        qPrintable(rows.at(i)));
        }
        if (panel->hiddenCount() > 0) {
            std::printf("HIDDEN\t%d\n", panel->hiddenCount());
        }
        if (!panel->noteText().isEmpty()) {
            std::printf("NOTE\t%s\n", qPrintable(panel->noteText()));
        }
        return 0;
    }

    // T13-10 取证：把设置页三张卡片**当前读到的值**打出来（stdout）。截图只能证明"渲染出来了"，
    // 字段值一律以这条文本为准 —— 人眼从 1280×800 的图上读小字不算证据。
    if (opt.settingsDumpCards) {
        SettingsPage* page = window.settingsPage();
        if (page == nullptr) {
            qWarning().noquote() << "[dump] 没有设置页";
            return 2;
        }
        const auto boolText = [](bool value) {
            return value ? QStringLiteral("true") : QStringLiteral("false");
        };
        const QStringList lines = {
            QStringLiteral("study.enabled=%1").arg(boolText(page->studyEnabledCheck()->isChecked())),
            QStringLiteral("study.focus_interval_min=%1").arg(page->studyFocusSpin()->value()),
            QStringLiteral("study.recheck_interval_min=%1").arg(page->studyRecheckSpin()->value()),
            QStringLiteral("study.max_failures=%1").arg(page->studyFailuresSpin()->value()),
            QStringLiteral("study.cooldown_min=%1").arg(page->studyCooldownSpin()->value()),
            QStringLiteral("study.cooldown_probe_min=%1").arg(page->studyProbeSpin()->value()),
            QStringLiteral("study.relative_band=%1")
                .arg(page->studyBandSpin()->value(), 0, 'f', 2),
            QStringLiteral("bilibili.game_watch.enabled=%1")
                .arg(boolText(page->gameEnabledCheck()->isChecked())),
            QStringLiteral("bilibili.game_watch.interval_s=%1").arg(page->gameIntervalSpin()->value()),
            QStringLiteral("bilibili.game_watch.confident_score=%1")
                .arg(page->gameScoreSpin()->value(), 0, 'f', 2),
            QStringLiteral("bilibili.cookie_file=%1").arg(page->cookiePathEdit()->text()),
            QStringLiteral("profile.enabled=%1").arg(boolText(page->profileEnabledCheck()->isChecked())),
            QStringLiteral("profile.trigger_chars=%1").arg(page->profileCharsSpin()->value()),
            QStringLiteral("profile.trigger_turns=%1").arg(page->profileTurnsSpin()->value()),
        };
        for (const QString& line : lines) {
            std::printf("CARD\t%s\n", qPrintable(line));
        }
        // 凭据单独一行（只给掩码：原值不回显）
        std::printf("CARD\tbilibili.credentials=%s\n",
                    qPrintable(QString(page->cookieStatusLabel()->text())
                                   .replace(QLatin1Char('\n'), QLatin1String(" | "))));
        return 0;
    }

    if (opt.idleMs >= 0) {
        window.idleWatcher()->setIdleMs(opt.idleMs);   // 验收时把 5s 缩短
    }

    if (!window.showPage(opt.page)) {
        qWarning().noquote() << "[gui] 未知页面:" << opt.page << "→ 使用 home";
        window.showPage(QStringLiteral("home"));
    }
    if (opt.windowed) {
        window.resize(1280, 800);
        window.show();
    } else {
        window.showFullScreen();
    }

    window.startIpc(opt.socketPath);

    // T14-7b 取证：把整棵控件树的**真实尺寸 / 最小尺寸**打出来并退出。
    //
    // 起因：1280×800 的屏上窗口被撑成 1280×883，底部 83 px 在屏幕外。
    // `xprop WM_NORMAL_HINTS` 已经证明"全屏请求是发出去的"（_NET_WM_STATE_FULLSCREEN
    // 也在），而 `program specified minimum size: 831 by 883` 说明是**应用自己的最小
    // 尺寸**比屏幕高 —— 但截图只能看出"底下被切了"，看不出是**谁**要的这 83 px。
    // 这份文本就是答案：每个控件的 size / minimumSizeHint / sizeHint / 可见性。
    // 输出走 stdout（Qt 日志在 stderr），格式：LAYOUT + TAB + 缩进 + Class#objectName …
    if (opt.dumpLayout) {
        const int delay = opt.screenshotDelayMs;      // 复用 --screenshot-delay（默认 1500）
        QTimer::singleShot(delay, &window, [&window]() {
            dumpLayoutTree(&window);
            QCoreApplication::exit(0);
        });
    }

    if (!opt.chatDemo.isEmpty()) {
        const QString text = opt.chatDemo;
        QTimer::singleShot(1200, &window, [&window, text]() { window.demoSend(text); });
    }
    if (!opt.inputTypeDemo.isEmpty()) {
        const QString type = opt.inputTypeDemo;
        QTimer::singleShot(1200, &window, [&window, type]() {
            if (window.chatPanel() != nullptr) {
                window.chatPanel()->setInputType(type);   // 与菜单项同一条路径
            }
        });
    }
    if (opt.inputMenuDemo) {        QTimer::singleShot(1500, &window, [&window]() {
            if (window.chatPanel() != nullptr && window.chatPanel()->inputTypeButton() != nullptr) {
                window.chatPanel()->inputTypeButton()->showMenu();
            }
        });
    }
    // S10 取证：把焦点给输入框（软键盘应当**这时**才弹，启动时不弹）
    if (opt.focusInputDemo) {
        QTimer::singleShot(1500, &window, [&window]() {
            if (window.chatPanel() != nullptr && window.chatPanel()->input() != nullptr) {
                window.chatPanel()->input()->setFocus(Qt::OtherFocusReason);
            }
        });
    }
    if (!opt.musicNoteDemo.isEmpty()) {
        const QString what = opt.musicNoteDemo;
        QTimer::singleShot(1600, &window, [&window, what]() { window.demoPlaceholderNote(what); });
    }
    if (!opt.videoFile.isEmpty()) {
        window.setVideoSource(opt.videoFile);
    }
    if (opt.nextBilibiliDemo) {
        QTimer::singleShot(2500, &window, [&window]() { window.demoNextBilibili(); });
    }
    if (opt.prevBilibiliDemo) {
        QTimer::singleShot(2500, &window, [&window]() { window.demoPrevBilibili(); });
    }
    if (opt.bilibiliPickDemo >= 0) {
        const int index = opt.bilibiliPickDemo;
        // 比"下一集"晚一点：预览栏要先收到队列才会有点得着的格子
        QTimer::singleShot(3000, &window, [&window, index]() { window.demoPickBilibili(index); });
    }
    if (!opt.videoNoteDemo.isEmpty()) {
        const QString what = opt.videoNoteDemo;
        QTimer::singleShot(2500, &window, [&window, what]() { window.demoVideoNote(what); });
    }
    if (opt.videoPlayDemo) {
        QTimer::singleShot(3500, &window, [&window]() { window.demoPlayPause(); });
    }
    if (opt.videoPauseDemoMs > 0) {
        // T11-9：验收"暂停 -> 缓冲继续预取到 60 s"。⚠ 必须由 **GUI 自己**暂停 ——
        // 从 Agent 那侧塞一条 playing=false 会被 GUI 每 2 秒的进度回报覆盖回去。
        const int ms = opt.videoPauseDemoMs;
        QTimer::singleShot(ms, &window, [&window]() { window.demoPlayPause(); });
    }
    if (opt.videoFullscreenDemo) {
        QTimer::singleShot(2500, &window, [&window]() { window.demoFullscreen(); });
    }
    if (!opt.modeDemo.isEmpty()) {
        // T12-3 验收：起播/连上之后点**真实控件**里指向该模式的那颗按钮
        //   （`--mode-demo SLEEP` 在 GAME 里点"睡眠" -> Agent 应当走 GAME->IDLE->SLEEP）
        const QString target = opt.modeDemo;
        const int ms = opt.modeDemoMs;
        QTimer::singleShot(ms, &window, [&window, target]() { window.demoSwitchMode(target); });
    }
    if (!opt.modelModeDemo.isEmpty()) {
        const QString mode = opt.modelModeDemo;
        QTimer::singleShot(1200, &window, [&window, mode]() {
            if (window.modelPage() != nullptr && window.modelPage()->modeButton(mode) != nullptr) {
                window.modelPage()->modeButton(mode)->setChecked(true);
            }
        });
    }
    if (opt.modelSaveDemo) {
        QTimer::singleShot(2000, &window, [&window]() {
            if (window.modelPage() != nullptr) {
                window.modelPage()->saveAndSync();
            }
        });
    }
    if (!opt.benchDemo.isEmpty()) {
        const QString which = opt.benchDemo;
        QTimer::singleShot(1500, &window, [&window, which]() {
            if (window.modelPage() != nullptr) {
                window.modelPage()->startBenchmark(which);
            }
        });
    }
    if (opt.benchStopDemo) {
        QTimer::singleShot(5000, &window, [&window]() {
            if (window.modelPage() != nullptr) {
                window.modelPage()->stopBenchmark();
            }
        });
    }
    if (opt.settingsSaveDemo) {
        QTimer::singleShot(1800, &window, [&window]() {
            if (window.settingsPage() == nullptr) {
                return;
            }
            // 两个时间**分开**改：区域 9000ms、控制条 800ms
            window.settingsPage()->regionIdleSpin()->setValue(9000);
            window.settingsPage()->overlayIdleSpin()->setValue(800);
            window.settingsPage()->saveButton()->click();
        });
    }
    // T13-10 验收：三张卡片设成**有区分度**的值（不是模板默认值）再走真实"保存"按钮 ——
    // 这样"控件 -> 文件"这条路才真的被验到，而不是把模板默认值抄一遍。
    if (opt.settingsCardsDemo) {
        QTimer::singleShot(1800, &window, [&window]() {
            SettingsPage* page = window.settingsPage();
            if (page == nullptr) {
                return;
            }
            page->studyEnabledCheck()->setChecked(true);
            page->studyFocusSpin()->setValue(25);
            page->studyRecheckSpin()->setValue(6);
            page->studyFailuresSpin()->setValue(4);
            page->studyCooldownSpin()->setValue(20);
            page->studyProbeSpin()->setValue(2);
            page->studyBandSpin()->setValue(0.06);
            page->gameEnabledCheck()->setChecked(false);
            page->gameIntervalSpin()->setValue(50);
            page->gameScoreSpin()->setValue(0.88);
            page->profileEnabledCheck()->setChecked(false);
            page->profileCharsSpin()->setValue(1500);
            page->profileTurnsSpin()->setValue(8);
            page->saveButton()->click();
        });
    }
    // T13-10 收尾：把你定的最终状态写进真配置 —— 三段都落在**模板默认值**上、
    // 学习监督**打开**（其余界面项保持配置里的原值）。
    // ⚠ 这里**不调** restoreDefaults()：那会把 gui.* 也拉回页面默认值 = 顺手改掉你的界面设置。
    if (opt.settingsFinalDemo) {
        QTimer::singleShot(1800, &window, [&window]() {
            SettingsPage* page = window.settingsPage();
            if (page == nullptr) {
                return;
            }
            page->studyEnabledCheck()->setChecked(true);
            page->studyFocusSpin()->setValue(30);
            page->studyRecheckSpin()->setValue(5);
            page->studyFailuresSpin()->setValue(3);
            page->studyCooldownSpin()->setValue(30);
            page->studyProbeSpin()->setValue(1);
            page->studyBandSpin()->setValue(0.05);
            page->gameEnabledCheck()->setChecked(true);
            page->gameIntervalSpin()->setValue(60);
            page->gameScoreSpin()->setValue(0.82);
            page->profileEnabledCheck()->setChecked(true);
            page->profileCharsSpin()->setValue(2000);
            page->profileTurnsSpin()->setValue(12);
            page->saveButton()->click();
        });
    }
    // T14-3 验收：改**一个**键再点保存 —— 走 IPC 让真 Agent 落盘（端到端取证用）
    if (opt.settingsOneKeyDemo) {
        QTimer::singleShot(1800, &window, [&window]() {
            SettingsPage* page = window.settingsPage();
            if (page == nullptr) {
                return;
            }
            page->studyBandSpin()->setValue(0.077);
            page->saveButton()->click();
        });
    }
    // T13-10 取证：滚到设置页下面几张卡片再截图（页面比窗口高，不滚拍不到）
    if (opt.settingsScrollDemo >= 0) {
        const int px = opt.settingsScrollDemo;
        QTimer::singleShot(1200, &window, [&window, px]() {
            SettingsPage* page = window.settingsPage();
            if (page == nullptr || page->scrollArea() == nullptr
                || page->scrollArea()->verticalScrollBar() == nullptr) {
                return;
            }
            page->scrollArea()->verticalScrollBar()->setValue(px);
        });
    }
    if (opt.reportDemo) {
        QTimer::singleShot(1800, &window, [&window]() {
            if (window.modelPage() != nullptr) {
                window.modelPage()->showLatestReport();
            }
        });
    }
    if (opt.modelStartDemo) {
        QTimer::singleShot(2000, &window, [&window]() {
            if (window.modelPage() != nullptr) {
                window.modelPage()->startButton()->click();
            }
        });
    }
    if (opt.watchdogDemo) {
        QTimer::singleShot(1800, &window, [&window]() {
            if (window.sysPage() != nullptr) {
                window.sysPage()->triggerWatchdog();
            }
        });
    }

    qInfo().noquote() << "[gui] 唤醒:" << window.wakeStateText()
                      << QStringLiteral("idle_ms=%1").arg(window.idleWatcher()->idleMs());

    if (!opt.screenshotSeq.isEmpty()) {
        runScreenshotSequence(&window, &app, opt.screenshotSeq);
    } else if (!opt.screenshot.isEmpty()) {
        const QString path = opt.screenshot;
        QTimer::singleShot(opt.screenshotDelayMs, &window, [&app, &window, path]() {
            const bool saved = window.grab().save(path);
            qInfo().noquote() << "[gui] 截图" << (saved ? "成功:" : "失败:") << path;
            app.quit();
        });
    }

    qInfo().noquote() << "[gui] GUI 模式, 页面:" << opt.page
                      << (opt.windowed ? "(窗口)" : "(全屏)")
                      << "连接:" << opt.socketPath;
    return app.exec();
}

/// T14-7b 取证：递归打印控件树。`min` 是 `minimumSizeHint()`（= 布局算出来的最小尺寸，
//  也就是把窗口撑过屏幕的那个数），`hint` 是 `sizeHint()`（"我想要多大"）。
/// 隐藏的控件也打（`visible=0`）：QStackedWidget 的最小尺寸是**所有页**的最大值，
/// 所以"当前没显示的那一页"完全可能是撑高窗口的元凶 —— 不列出来就看不出来。
void dumpLayoutTree(QWidget* widget, int depth)
{
    if (widget == nullptr) {
        return;
    }
    const QSize minSize = widget->minimumSizeHint();
    const QSize hint = widget->sizeHint();
    const QString name = widget->objectName().isEmpty() ? QStringLiteral("-")
                                                        : widget->objectName();
    std::printf("LAYOUT\t%*s%s#%s size=%dx%d min=%dx%d hint=%dx%d visible=%d\n",
                depth * 2, "",
                widget->metaObject()->className(), qPrintable(name),
                widget->width(), widget->height(),
                minSize.width(), minSize.height(),
                hint.width(), hint.height(),
                widget->isVisible() ? 1 : 0);
    const QObjectList children = widget->children();
    for (QObject* child : children) {
        if (auto* childWidget = qobject_cast<QWidget*>(child)) {
            dumpLayoutTree(childWidget, depth + 1);
        }
    }
}

} // namespace

int main(int argc, char** argv)
{
    // ⚑ T11-10：**板端 `souphttpsrc` 是坏的**（libgstreamer 1.18 配 plugins-good 1.16，
    //   连 `souphttpsrc ! fakesink` 都 SIGABRT），而 B 站视频那条路走的是**本机 http**
    //   （Agent 的内存窗口，边下边喂）。把坏的那个按到 rank 0，让板上的 `curlhttpsrc`
    //   顶上来 —— 实测：压下去之后 `playbin uri=http://…` static/live 两种供法都正常。
    //   ⚠ 必须在**本进程里**设：用 HTTP 的是 GUI 自己（QMediaPlayer 的 GStreamer 后端）。
    {
        const QByteArray rank = qgetenv("GST_PLUGIN_FEATURE_RANK");
        if (!rank.contains("souphttpsrc")) {
            qputenv("GST_PLUGIN_FEATURE_RANK",
                    rank.isEmpty() ? QByteArray("souphttpsrc:0")
                                   : rank + ",souphttpsrc:0");
        }
    }
    const Options opt = parseArgs(argc, argv);
    if (!opt.error.isEmpty()) {
        qWarning().noquote() << "[gui] 参数错误:" << opt.error;
        printUsage();
        return 2;
    }
    if (opt.help) {
        printUsage();
        return 0;
    }

    // 无显示环境（ctest / 冒烟）且调用方没指定平台插件时，退回 offscreen，
    // 否则 QApplication 会直接因为连不上 display 而退出。
    if (!opt.stdio && qEnvironmentVariableIsEmpty("QT_QPA_PLATFORM")
        && qEnvironmentVariableIsEmpty("DISPLAY")) {
        qputenv("QT_QPA_PLATFORM", "offscreen");
        qInfo().noquote() << "[gui] 无 DISPLAY，使用 QT_QPA_PLATFORM=offscreen";
    }

    return opt.stdio ? runStdioMode(opt, argc, argv) : runGuiMode(opt, argc, argv);
}
