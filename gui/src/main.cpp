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
#include <QMouseEvent>
#include <QObject>
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
#include "services/local_client.h"
#include "ui/chat_panel.h"
#include "ui/bottom_bar.h"
#include "ui/model_page.h"
#include "ui/settings_page.h"
#include "ui/music_bar.h"
#include "ui/sys_page.h"

namespace {

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
    bool nextWallpaperDemo = false; ///< 启动后点一下"下一张"（验收用）
    bool nextBilibiliDemo = false;  ///< 启动后点一下视频区"下一集"（验收用）
    bool videoPlayDemo = false;     ///< 启动后切一次播放/暂停（验收用）
    bool videoFullscreenDemo = false; ///< 启动后切一次全屏（验收用）
    bool watchdogDemo = false;      ///< 启动后点一下看门狗（验收用）
    QString modelModeDemo;         ///< 非空 = 切到该推理位置（验收用）
    bool modelSaveDemo = false;     ///< 启动后点"保存并同步"（验收用）
    bool modelStartDemo = false;    ///< 启动后点"启动服务"（验收用）
    QString benchDemo;             ///< 非空 = 启动后跑该基准（验收用）
    bool benchStopDemo = false;     ///< 启动后点"停止测试"（验收用）
    bool reportDemo = false;        ///< 启动后点"查看最新报告"（验收用）
    bool settingsSaveDemo = false;  ///< 启动后改两个时间并保存（验收用）
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
        "  --next-wallpaper-demo  启动后点一下主区的「下一张」（验收用）\n"
        "  --video <文件>       主区视频源（本地文件；验收用）\n"
        "  --next-bilibili-demo 启动后点一下视频区「下一集」（验收用）\n"
        "  --video-play-demo    启动后切一次播放/暂停（验收用）\n"
        "  --video-fullscreen-demo 启动后切一次全屏（覆盖整屏，验收用）\n"
        "  --watchdog-demo     启动后点一下看门狗（验收用）\n"
        "  --model-mode-demo <local|cloud|disabled>  切到该推理位置（验收用）\n"
        "  --model-save-demo   启动后点「保存并同步」（验收用）\n"
        "  --model-start-demo  启动后点「启动服务」（验收用）\n"
        "  --bench-demo <qwen_precheck|qwen_full|multimodal>  启动后跑该基准（验收用）\n"
        "  --bench-stop-demo   启动后点「停止测试」（验收用）\n"
        "  --report-demo       启动后点「查看最新报告」（验收用）\n"
        "  --settings-save-demo 启动后改两个休眠时间并保存（验收用）\n"
        "  --config <path>      指定 config/config.yaml（默认自动在仓库里找）\n"
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
        } else if (arg == QLatin1String("--next-wallpaper-demo")) {
            opt.nextWallpaperDemo = true;
        } else if (arg == QLatin1String("--video") || arg.startsWith(QLatin1String("--video="))) {
            opt.videoFile = optionValue(arg, QStringLiteral("--video"), i, argc, argv, opt);
        } else if (arg == QLatin1String("--next-bilibili-demo")) {
            opt.nextBilibiliDemo = true;
        } else if (arg == QLatin1String("--video-play-demo")) {
            opt.videoPlayDemo = true;
        } else if (arg == QLatin1String("--video-fullscreen-demo")) {
            opt.videoFullscreenDemo = true;
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
    if (opt.inputMenuDemo) {
        QTimer::singleShot(1500, &window, [&window]() {
            if (window.chatPanel() != nullptr && window.chatPanel()->inputTypeButton() != nullptr) {
                window.chatPanel()->inputTypeButton()->showMenu();
            }
        });
    }
    if (!opt.musicNoteDemo.isEmpty()) {
        const QString what = opt.musicNoteDemo;
        QTimer::singleShot(1600, &window, [&window, what]() { window.demoPlaceholderNote(what); });
    }
    if (opt.nextWallpaperDemo) {
        QTimer::singleShot(1600, &window, [&window]() { window.demoNextWallpaper(); });
    }
    if (!opt.videoFile.isEmpty()) {
        window.setVideoSource(opt.videoFile);
    }
    if (opt.nextBilibiliDemo) {
        QTimer::singleShot(2500, &window, [&window]() { window.demoNextBilibili(); });
    }
    if (!opt.videoNoteDemo.isEmpty()) {
        const QString what = opt.videoNoteDemo;
        QTimer::singleShot(2500, &window, [&window, what]() { window.demoVideoNote(what); });
    }
    if (opt.videoPlayDemo) {
        QTimer::singleShot(3500, &window, [&window]() { window.demoPlayPause(); });
    }
    if (opt.videoFullscreenDemo) {
        QTimer::singleShot(2500, &window, [&window]() { window.demoFullscreen(); });
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

} // namespace

int main(int argc, char** argv)
{
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
