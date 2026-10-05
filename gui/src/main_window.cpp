// ============================================================================
//  gui/src/main_window.cpp — 主窗口框架实现（T1 骨架 + T3 唤醒接线）
//
//  1. 上区域通栏顶栏（T4 填内容）、左区域导航栏 + 页面栈
//  2. 四区域包进 RegionHost，接 IdleWatcher：空闲折叠 / 点击唤醒（T3）
//  3. 一份**最小**高级灰样式（配色见方案 §8），T15 再统一打磨
// ============================================================================
#include <QComboBox>   // T15-17 / T3：读设置页的四个区域模式 ✓
#include <QCheckBox>   // T15-17 / T3b：读 Debug 勾选框 ✓
#include <QSpinBox>    // T15-17 / T3：读休眠时间 ✓

#include "main_window.h"

#include <QInputMethod>   // T15-17 bug①后续：跟着输入法可见性摆承载层 ✓
#include <QQuickItem>     // T15-17 bug①后续：rootObject()->property("kbHeight") 需要**完整类型** ✓
#include <QQuickWidget>   // T15-17 bug①：eglfs 下承载 Qt 虚拟键盘（Application 集成）

#include "core/bilibili_format.h"
#include "core/config_store.h"
#include "core/idle_watcher.h"
#include "core/image_fit.h"
#include "core/schedule_model.h"
#include "services/local_client.h"
#include "services/onboard_ctl.h"
#include <QGuiApplication>      // T15-1 1b：platformName() 判断"有没有 X"
#include <QInputMethod>         // T15-1 1b：无 X 时把软键盘交给 Qt 输入法
#include "ui/bilibili_cover.h"
#include "ui/bilibili_preview.h"
#include "ui/bottom_bar.h"
#include "ui/chat_panel.h"
#include "ui/cover_loader.h"
#include "ui/mode_panel.h"
#include "ui/music_bar.h"
#include "ui/base_style.h"
#include "ui/theme.h"          // T15-16 G-A-1b：视觉常量（QSS 的 @token@ 由 theme::styleSheet 展开）
#include "ui/pages.h"
#include "ui/region_host.h"
#include "ui/icons.h"
#include "ui/model_page.h"
#include "ui/schedule_panel.h"
#include "ui/settings_page.h"
#include "ui/sys_page.h"
#include "ui/top_bar.h"
#include "ui/video_panel.h"

#include <QCoreApplication>
#include <QDateTime>
#include <QFrame>
#include <QHBoxLayout>
#include <QJsonDocument>
#include <QJsonObject>
#include <QLabel>
#include <QLineEdit>
#include <QPainter>
#include <QProcess>
#include <QPushButton>
#include <QShowEvent>
#include <QStackedWidget>
#include <QStringList>
#include <QTimer>
#include <QVariantAnimation>
#include <QVBoxLayout>

namespace {

/// 日程区默认最多显示几行（配置键 gui.schedule.max_rows；两段合计上限）
constexpr int kDefaultScheduleRows = 6;

/// 日程区刷新间隔（S5）：让窗口往前滑（新条目进来、已过的滑出去）+ 跨零点翻页
constexpr int kScheduleRefreshMs = 60 * 1000;

/// T15-16 G-B-5：壁纸淡入的**最小重绘间隔**（≈30 fps ✓）
/// 改前每个 valueChanged（60 Hz）都重绘 ⇒ 200 ms 要画 **12 帧**、每帧 1 次全屏填充 + 2 次全屏 blit ✗
constexpr qint64 kWallpaperFadeMinFrameMs = 33;

/// 方案 §8 配色：底 #1E1F22 / 面板 #2B2D31 / 分隔 #3A3D42 / 主文字 #E6E6E6 /
/// 次文字 #9AA0A6 / 强调 #7AA2F7。


} // namespace

MainWindow::MainWindow(QWidget* parent)
    : QMainWindow(parent)
{
    setWindowTitle(QStringLiteral("板端助手"));
    // T15-16 G-A-1b：QSS 里的 `@token@` 在这里展开成 theme.h 的常量值 ——
    // 展开结果与原字面量**逐字节相同**（零视觉变化 ✓），由 test_theme 的黄金串断言守住 ✓。
    setStyleSheet(theme::styleSheet(theme::kBaseQss));

    auto* central = new QWidget(this);
    auto* root = new QVBoxLayout(central);
    root->setContentsMargins(0, 0, 0, 0);
    root->setSpacing(0);

    // ---- 上区域：通栏（横跨导航与页面）----
    topHost_ = new RegionHost(RegionHost::Edge::Top, buildTopBar(), central);
    root->addWidget(topHost_);

    // ---- 其下：左区域（导航）+ 页面栈 ----
    auto* body = new QWidget(central);
    auto* bodyBox = new QHBoxLayout(body);
    bodyBox->setContentsMargins(0, 0, 0, 0);
    bodyBox->setSpacing(0);

    navHost_ = new RegionHost(RegionHost::Edge::Left, buildNavBar(), body);
    // 左区域做成圆角卡片：四周留 8px（右侧不留，紧贴页面区），圆角才看得出来
    navHost_->setInnerMargins(QMargins(8, 8, 0, 8));
    bodyBox->addWidget(navHost_);

    stack_ = new QStackedWidget(body);
    stack_->setObjectName(QStringLiteral("PageStack"));
    for (const PageEntry& entry : pageEntries()) {
        QWidget* page = createPage(entry.key, stack_);
        if (entry.key == QLatin1String("home")) {
            mainPage_ = static_cast<MainPage*>(page);   // createPage 保证 home 是 MainPage
        }
        if (entry.key == QLatin1String("system")) {
            sysPage_ = static_cast<SysPage*>(page);     // T10：系统与网络页
        }
        if (entry.key == QLatin1String("model")) {
            modelPage_ = static_cast<ModelPage*>(page); // T11：模型测试页
        }
        if (entry.key == QLatin1String("settings")) {
            settingsPage_ = static_cast<SettingsPage*>(page);   // T13：设置页
        }
        stack_->addWidget(page);
    }
    bodyBox->addWidget(stack_, 1);

    root->addWidget(body, 1);
    setCentralWidget(central);

    // ---- T15-1 1b：软键盘（Qt 虚拟键盘）让位 ----
    // 虚拟键盘是**独立窗口**（Qt 的 DesktopInputPanel：整屏半透明窗口 + 键盘贴底），
    // 它不会改我们的窗口大小，所以"它盖住的那半屏"必须由我们自己让开：
    // 跟着输入法的 visibleChanged / keyboardRectangleChanged，把右区域（对话输入行在那）
    // 的底部让出键盘高度 —— 否则输入行正好被键盘压住（板端实测：键盘 0,400 1280x400，
    // 输入行 y=496..544，全在键盘下面）。
    if (QGuiApplication::inputMethod() != nullptr) {
        connect(QGuiApplication::inputMethod(), &QInputMethod::visibleChanged,
                this, &MainWindow::applyKeyboardInset);
        connect(QGuiApplication::inputMethod(), &QInputMethod::keyboardRectangleChanged,
                this, &MainWindow::applyKeyboardInset);
    }

    // ---- T3：唤醒机制 ----
    watcher_ = new core::IdleWatcher(this);
    watcher_->installOn(QCoreApplication::instance());
    connect(watcher_, &core::IdleWatcher::wentIdle, this, [this]() {
        for (RegionHost* region : regions()) {
            if (region != nullptr && region->isActive()) {
                region->hideRegion(true);
            }
        }
    });
    connect(watcher_, &core::IdleWatcher::woke, this, [this]() {
        for (RegionHost* region : regions()) {
            if (region != nullptr && region->isCollapsed()) {
                region->showRegion(true);
            }
        }
    });

    showPage(QStringLiteral("home"));

    // ---- T4：IPC 客户端与显示状态都归主窗口管 ----
    // main.cpp 把"终端桥"（[recv] 打印 + stdin 命令）接到 client() 上，
    // 于是 GUI 与 --stdio 两种模式的 IPC 行为保持一致（e2e_ipc 依赖这一点）。
    client_ = new LocalClient(this);
    connect(client_, &LocalClient::connectionChanged, this, [this](bool up) {
        agentUp_ = up;
        if (up) {
            everConnected_ = true;
        }
        // T15-16：链接一通/一断，音乐条那三个按钮就跟着可用/禁用
        if (BottomBar* bar = bottomBar(); bar != nullptr && bar->musicBar() != nullptr) {
            bar->musicBar()->setConnected(up);
        }
        refreshLinkState();
    });
    connect(client_, &LocalClient::statusReceived, this,
            [this](const QJsonObject& d) { onMessage(QStringLiteral("status"), d); });
    connect(client_, &LocalClient::llmReceived, this,
            [this](const QJsonObject& d) { onMessage(QStringLiteral("llm"), d); });
    connect(client_, &LocalClient::wallpaperReceived, this,
            [this](const QJsonObject& d) { onMessage(QStringLiteral("wallpaper"), d); });
    connect(client_, &LocalClient::musicReceived, this,
            [this](const QJsonObject& d) { onMessage(QStringLiteral("music"), d); });
    // T15-14-a: OTA/槽状态 —— 只交给设置页展示（MainWindow 不留状态：它不参与绘制）
    connect(client_, &LocalClient::otaStateReceived, this,
            [this](const QJsonObject& d) { onMessage(QStringLiteral("ota_state"), d); });
    connect(client_, &LocalClient::bilibiliReceived, this,
            [this](const QJsonObject& d) { onMessage(QStringLiteral("bilibili"), d); });
    // T14-3：两条回执按请求 id 的路由（`<prefix>-<n>`）—— 页面自己不认识就忽略
    connect(client_, &LocalClient::configResultReceived, this, [this](const QJsonObject& d) {
        const QString id = d.value(QStringLiteral("id")).toString();
        // 先把回执按请求 id 的来源分发给对应页面 ✓（不认识就忽略 ✓）
        if (id.startsWith(QLatin1String("settings")) && settingsPage_ != nullptr) {
            settingsPage_->onConfigResult(d);
        } else if (id.startsWith(QLatin1String("model")) && modelPage_ != nullptr) {
            modelPage_->onConfigResult(d);
        } else if (id.startsWith(QLatin1String("input-source"))) {
            // 输入源那一次：写就写了，失败只记日志（界面已经切过去了）
            if (!d.value(QStringLiteral("ok")).toBool(false)) {
                qWarning().noquote() << "[ui] 输入源没能写进配置:"
                                     << d.value(QStringLiteral("error")).toString();
            }
        } else {
            qDebug().noquote() << "[ipc] 收到不认识来源的 config_result:" << id;
        }

        // T15-17 / T2 + T3c：**保存即生效** ✓ —— **任何**成功的配置回执都重放一次 ✓，不必重启 GUI ✓。
        // ⚠ T3c 修的缺口：先前这段只写在 `settings` 分支里 ✗ ⇒ **模型页保存**与**输入源切换**
        //   落盘成功后**不会重放** ✗（同族毛病 ✓）⇒ 现在提到分支之外 ✓，三个来源一视同仁 ✓。
        // ⚠ 回执里只有 id／ok／error ✗（不带全量 gui.* ✓）⇒ **重新读盘** ✓；
        //   而回执是 Agent **落盘之后**才发的 ✓ ⇒ 此刻盘上就是新值 ✓。
        // ⚠ `reapplyGuiConfig()` **不含** `loadFromConfig` ✓ ⇒ **不会冲掉正在编辑的内容** ✓（T1 的靶心 ✓）。
        if (d.value(QStringLiteral("ok")).toBool(false) && !configPath_.isEmpty()) {
            core::ConfigStore store;
            if (store.load(configPath_)) {
                reapplyGuiConfig(store);
                qInfo().noquote() << QStringLiteral(
                    "[ui] 配置已落盘并**立即套用** ✓（来源=%1，不必重启 ✓）").arg(id);
            } else {
                qWarning().noquote() << QStringLiteral("[ui] 配置落盘成功但重新读盘失败 ✗，本次先不套用");
            }
        }
    });
    connect(client_, &LocalClient::serviceResultReceived, this, [this](const QJsonObject& d) {
        if (modelPage_ != nullptr) {
            modelPage_->onServiceResult(d);
        }
    });
    // T14-9：WiFi（kind=status|scan|ack）—— 只有设置页那张卡片关心
    connect(client_, &LocalClient::wifiReceived, this, [this](const QJsonObject& d) {
        if (settingsPage_ != nullptr) {
            settingsPage_->onWifiResult(d);
        }
    });

    // ---- T5：主页面右区域的交互接线 ----
    if (mainPage_ != nullptr && mainPage_->modePanel() != nullptr) {
        connect(mainPage_->modePanel(), &ModePanel::modeRequested, this,
                [this](const QString& value) {
                    if (client_ == nullptr) {
                        return;
                    }
                    client_->sendCommand(QStringLiteral("switch_mode"),
                                         QJsonObject{{QStringLiteral("value"), value}});
                });
    }
    if (mainPage_ != nullptr && mainPage_->chatPanel() != nullptr) {
        connect(mainPage_->chatPanel(), &ChatPanel::sendRequested, this,
                [this](const QString& text) {
                    ChatPanel* panel = mainPage_->chatPanel();
                    if (client_ == nullptr || !agentUp_) {
                        panel->appendSystem(QStringLiteral("没发出去：与 Agent 未连接"));
                        return;
                    }
                    panel->appendUser(text);
                    panel->setThinking(true);
                    client_->sendCommand(QStringLiteral("chat_input"),
                                         QJsonObject{{QStringLiteral("text"), text}});
                });
        // T6：输入源切换（小按钮三选）→ 弹/收 onboard + 写回 config.yaml 的 gui: 段
        connect(mainPage_->chatPanel(), &ChatPanel::inputTypeChanged, this,
                [this](const QString& type) { applyInputType(type); });
        // S10：只有输入框拿到焦点才弹软键盘；失焦收起
        connect(mainPage_->chatPanel(), &ChatPanel::inputFocusChanged, this,
                [this](bool focused) { onChatInputFocused(focused); });
    }

    // ⚠ T7-3：这里原来有一条"主区右下角「下一张」→ 发协议 next_wallpaper"的连接。
    //    按钮、信号与这条连接一起删掉了 —— 换壁纸只走对话（对 Agent 说
    //    "换一张安静的深色风景"），Agent 侧那条同名 IPC 命令也一并删除。

    // T9：视频区"下一集" → 真实发协议 next_bilibili
    if (mainPage_ != nullptr && mainPage_->videoPanel() != nullptr) {
        connect(mainPage_->videoPanel(), &VideoPanel::nextBilibiliRequested, this, [this]() {
            if (client_ == nullptr || !agentUp_) {
                if (mainPage_->chatPanel() != nullptr) {
                    mainPage_->chatPanel()->appendSystem(
                        QStringLiteral("没发出去：与 Agent 未连接"));
                }
                return;
            }
            client_->sendCommand(QStringLiteral("next_bilibili"), QJsonObject());
        });
        // T9：全屏按钮 → 画面铺满整个屏幕（隐藏四区域面板）
        connect(mainPage_->videoPanel(), &VideoPanel::fullscreenToggled, this,
                [this](bool on) { setVideoFullscreen(on); });
    }

    // ---- T11-7：B 站那几条线（预览栏 / 上一集 / 进度回报 / 格数上报）----
    //  取图器一份，预览栏与下区域封面**共用**（同一张封面只下一次）。
    coverLoader_ = new CoverLoader(this);
    if (mainPage_ != nullptr) {
        if (mainPage_->videoPanel() != nullptr) {
            mainPage_->videoPanel()->setCoverLoader(coverLoader_);
        }
        if (mainPage_->bottomBar() != nullptr && mainPage_->bottomBar()->bilibiliCover() != nullptr) {
            mainPage_->bottomBar()->bilibiliCover()->setCoverLoader(coverLoader_);
        }
    }
    if (mainPage_ != nullptr && mainPage_->videoPanel() != nullptr) {
        VideoPanel* video = mainPage_->videoPanel();
        // 「上一集」→ prev_bilibili（T11-7 从占位转正）
        connect(video, &VideoPanel::prevBilibiliRequested, this, [this]() {
            if (client_ == nullptr || !agentUp_) {
                if (mainPage_->chatPanel() != nullptr) {
                    mainPage_->chatPanel()->appendSystem(
                        QStringLiteral("没发出去：与 Agent 未连接"));
                }
                return;
            }
            client_->sendCommand(QStringLiteral("prev_bilibili"), QJsonObject());
        });
        // 点预览图第 index 格 → bilibili_pick{index}（**只有用户点才会播**）
        connect(video, &VideoPanel::pickBilibiliRequested, this, [this](int index) {
            if (client_ == nullptr || !agentUp_) {
                if (mainPage_->chatPanel() != nullptr) {
                    mainPage_->chatPanel()->appendSystem(
                        QStringLiteral("没发出去：与 Agent 未连接"));
                }
                return;
            }
            client_->sendCommand(QStringLiteral("bilibili_pick"),
                                 QJsonObject{{QStringLiteral("index"), index}});
        });
        // 预览栏格数变了 → bilibili_viewport{visible}（Agent 用它定队列目标 = 3N）
        connect(video, &VideoPanel::viewportChanged, this, [this](int visible) {
            sendToAgent(QStringLiteral("bilibili_viewport"),
                        QJsonObject{{QStringLiteral("visible"), visible}});
        });

        // 真实进度回报（不进 LLM：这是 Agent 自己要看的事实）
        connect(video, &VideoPanel::videoStateReported, this,
                [this](qint64 positionMs, qint64 durationMs, bool playing, bool eof) {
                    sendToAgent(QStringLiteral("video_state"),
                                bilibili::videoStatePayload(positionMs, durationMs, playing, eof));
                });
    }

    // ---- T15-16：音乐条三个按钮 → 现成的三条命令（**不新增协议**）----
    //   ⚠ 控件只发信号、命令在这里发（与 B 站那几条同一个路子）：`sendToAgent()` 会
    //     处理"还没连上 Agent"的情况（往对话区写一句"没发出去"）。
    if (mainPage_ != nullptr && mainPage_->bottomBar() != nullptr
        && mainPage_->bottomBar()->musicBar() != nullptr) {
        MusicBar* music = mainPage_->bottomBar()->musicBar();
        connect(music, &MusicBar::prevClicked, this,
                [this]() { sendToAgent(QStringLiteral("music_prev")); });
        connect(music, &MusicBar::playPauseClicked, this,
                [this]() { sendToAgent(QStringLiteral("music_play_pause")); });
        connect(music, &MusicBar::nextClicked, this,
                [this]() { sendToAgent(QStringLiteral("music_next")); });

        // ---- T15-16：歌词与进度的那条线 ----
        //   · `musicLyrics_` 收 `lyric_*` 字段（rev 变了才整份换）；
        //   · provider 是 D5 接口的真实现 —— 音乐条只按位置问它"该显示哪两句"；
        //   · 1 s 定时器：**进度条与歌词都靠它走**。协议 3 s 才推一次位置，
        //     光等推送的话进度条一顿一顿、歌词还会晚一句；本地时钟外推就没这问题，
        //     每次收到推送再重新锚定一次（消漂移）。
        musicLyricsProvider_.setSource(&musicLyrics_);
        music->setLyricsProvider(&musicLyricsProvider_);
        musicTick_ = new QTimer(this);
        musicTick_->setInterval(1000);
        connect(musicTick_, &QTimer::timeout, this, &MainWindow::tickMusic);
        musicTick_->start();
    }

    // T9：内嵌控制条的 活动/锁定 —— **自己的 watcher、自己的 idle_ms**，
    //     不与四个区域的 wake.idle_ms 共享（验收要求）。
    overlayWatcher_ = new core::IdleWatcher(this);
    overlayWatcher_->installOn(QCoreApplication::instance());
    overlayWatcher_->setIdleMs(3000);
    connect(overlayWatcher_, &core::IdleWatcher::wentIdle, this, [this]() {
        if (overlayAutoHide_ && mainPage_ != nullptr && mainPage_->videoPanel() != nullptr) {
            mainPage_->videoPanel()->setOverlayVisible(false);
            qInfo().noquote() << "[video] 控制条空闲隐藏";
        }
    });
    connect(overlayWatcher_, &core::IdleWatcher::woke, this, [this]() {
        if (overlayAutoHide_ && mainPage_ != nullptr && mainPage_->videoPanel() != nullptr) {
            mainPage_->videoPanel()->setOverlayVisible(true);
        }
    });
    overlayWatcher_->setEnabled(true);

    // T10：系统页按配置的 gui.monitor_interval_ms 定时刷新（不看时才不刷）
    monitorTimer_ = new QTimer(this);
    connect(monitorTimer_, &QTimer::timeout, this, [this]() {
        ++monitorTicks_;             // G-B-4：先记一笔"醒了" ✓（可见性判断之后就不算醒了 ✗）
        if (sysPage_ != nullptr && stack_ != nullptr && stack_->currentWidget() == sysPage_) {
            sysPage_->refresh();
        }
    });
    monitorTimer_->setInterval(1000);   // applyConfig() 里会按配置改写

    // T15-16 G-B-4：**系统页不可见就不让定时器醒** ✓
    //   改前它每秒都触发（只是进 lambda 后什么都不干 ✗）—— 板端实测隐藏态自愿唤醒 4.13 次/s ✓。
    if (stack_ != nullptr) {
        connect(stack_, &QStackedWidget::currentChanged, this, [this](int) {
            if (monitorTimer_ == nullptr) {
                return;
            }
            if (sysPageVisible()) {
                monitorTimer_->start();
            } else {
                monitorTimer_->stop();          // 停 ⇒ 隐藏期间一次都不醒 ✓
            }
        });
    }
    if (sysPageVisible()) {
        monitorTimer_->start();                 // 启动时若就在系统页 ⇒ 立刻开 ✓
    }

    // S5：日程区每 60 秒重读一次（窗口往前滑 / 跨零点翻页）。
    // 启动时的那一次在 applyConfig() 末尾（main.cpp 会调它）。
    scheduleTimer_ = new QTimer(this);
    connect(scheduleTimer_, &QTimer::timeout, this, [this]() { reloadSchedule(); });
    scheduleTimer_->setInterval(kScheduleRefreshMs);
    scheduleTimer_->start();

    // T9：视频区点了占位控制项 → 说明挂到右侧对话区的提示行
    //（控制条里放文字会被 QVideoWidget 的原生窗口盖住，实测真机看不到）
    if (mainPage_ != nullptr && mainPage_->videoPanel() != nullptr) {
        connect(mainPage_->videoPanel(), &VideoPanel::placeholderClicked, this,
                [this](const QString& what) {
                    if (mainPage_->chatPanel() != nullptr) {
                        mainPage_->chatPanel()->setInputHint(
                            QStringLiteral("视频「%1」未接入：协议暂无对应命令")
                                .arg(what));
                    }
                });
    }

    // T8：壁纸交叉淡入（200ms）
    wallpaperAnim_ = new QVariantAnimation(this);
    wallpaperAnim_->setDuration(200);
    wallpaperAnim_->setStartValue(0.0);
    wallpaperAnim_->setEndValue(1.0);
    connect(wallpaperAnim_, &QVariantAnimation::valueChanged, this,
            [this](const QVariant& value) {
                wallpaperFade_ = value.toReal();     // 进度**每次都更新** ✓（不然淡入会跳 ✓）
                // T15-16 G-B-5：**限帧到 ~30 fps** ✓ —— 只把重复的重绘请求合并掉 ✗，
                //   动画时长仍是 200 ms ✓（不拖长 ✓）；本次淡入的第一帧一定画 ✓。
                const qint64 nowMs = QDateTime::currentMSecsSinceEpoch();
                if (wallpaperFadeLastMs_ == 0
                    || nowMs - wallpaperFadeLastMs_ >= kWallpaperFadeMinFrameMs) {
                    wallpaperFadeLastMs_ = nowMs;
                    update();
                }
            });
    connect(wallpaperAnim_, &QVariantAnimation::finished, this, [this]() {
        prevWallpaper_ = QPixmap();      // 淡入结束就把旧图丢掉
        wallpaperFade_ = 1.0;
        wallpaperFadeLastMs_ = 0;        // G-B-5：下一次淡入的首帧一定画 ✓
        update();                        // ⚠ 这里**无条件**更新 ✓（否则最后一帧可能被节流吞掉 ✗）
    });

    onboard_ = new OnboardCtl(this);
}

void MainWindow::setWallpaperFromPath(const QString& path, int index)
{
    if (path.isEmpty() || path == wallpaperPath_) {
        return;                       // 同一张不重复加载
    }
    QPixmap pixmap;
    if (!pixmap.load(path)) {
        // 按方案 §7：读不到就给兜底底图 + 灰字路径，不崩
        wallpaper_ = QPixmap();
        prevWallpaper_ = QPixmap();
        wallpaperFade_ = 1.0;
        wallpaperPath_ = path;
        qWarning().noquote() << "[ui] 壁纸读取失败:" << path;
        if (mainPage_ != nullptr) {
            mainPage_->setMainHint(QStringLiteral("壁纸读取失败：%1").arg(path), /*warn=*/true);
        }
        update();
        return;
    }

    prevWallpaper_ = wallpaper_;
    wallpaper_ = pixmap;
    wallpaperPath_ = path;
    qInfo().noquote() << QStringLiteral("[ui] 壁纸: %1 (%2x%3, index=%4)")
                             .arg(path)
                             .arg(pixmap.width())
                             .arg(pixmap.height())
                             .arg(index);
    // T3：壁纸画上去了，主区那两行开发占位文字就收起来（否则压在图上）。
    //     传空串 = 藏起来，见 MainPage::setMainHint 的注释。
    if (mainPage_ != nullptr) {
        mainPage_->setMainHint(QString());
    }
    if (prevWallpaper_.isNull()) {
        wallpaperFade_ = 1.0;
    } else {
        wallpaperFade_ = 0.0;
        wallpaperAnim_->stop();
        wallpaperAnim_->start();
    }
    update();
}

void MainWindow::paintEvent(QPaintEvent* event)
{
    Q_UNUSED(event);
    QPainter painter(this);
    const QRect target = rect();
    painter.fillRect(target, QColor(theme::kBg));   // 兜底底色（也算"占位底图"）

    // T15-16 G-B-5：淡入期间的**重绘帧数**（判据 ✓）
    //   ⚠ 放在这里而不是 `valueChanged` 回调里 ✗ —— 那数的是"动画要求重绘几次"，
    //     这里是"真的画了几帧" ✓（合并/丢弃的重绘不该算进去 ✓）。
    if (wallpaperFade_ < 1.0) {
        ++wallpaperFadePaints_;
    }

    // 交叉淡入：旧图在下、新图按 fade 叠上去
    if (!prevWallpaper_.isNull() && wallpaperFade_ < 1.0) {
        painter.setOpacity(1.0 - wallpaperFade_);
        painter.drawPixmap(target, prevWallpaper_,
                           core::centeredCropRect(prevWallpaper_.size(), target.size()));
    }
    if (!wallpaper_.isNull()) {
        painter.setOpacity(prevWallpaper_.isNull() ? 1.0 : wallpaperFade_);
        painter.drawPixmap(target, wallpaper_,
                           core::centeredCropRect(wallpaper_.size(), target.size()));
    }
    painter.setOpacity(1.0);
}

// ⚠ T7-3：demoNextWallpaper()（验收时"点一下下一张"）随按钮一起删掉了。

void MainWindow::setVideoSource(const QString& path)
{
    if (mainPage_ == nullptr || mainPage_->videoPanel() == nullptr) {
        return;
    }
    mainPage_->videoPanel()->setSource(path);
    qInfo().noquote() << "[ui] 视频源:"
                      << (path.isEmpty() ? QStringLiteral("(未接入)") : path);
}

void MainWindow::demoWallpaperFade()
{
    // T15-16 G-B-5：与真换图**同一对动作** ✓（见 setWallpaperFromPath() 里的 stop()+start() ✓）
    if (wallpaperAnim_ != nullptr) {
        wallpaperAnim_->stop();
        wallpaperAnim_->start();
    }
}

void MainWindow::demoNextBilibili()
{
    if (mainPage_ != nullptr && mainPage_->videoPanel() != nullptr
        && mainPage_->videoPanel()->nextButton() != nullptr) {
        mainPage_->videoPanel()->nextButton()->click();
    }
}

void MainWindow::demoPrevBilibili()
{
    if (mainPage_ != nullptr && mainPage_->videoPanel() != nullptr
        && mainPage_->videoPanel()->previousButton() != nullptr) {
        mainPage_->videoPanel()->previousButton()->click();
    }
}

void MainWindow::demoSwitchMode(const QString& target)
{
    // 走**真实控件**: 点那颗按钮 -> ModePanel::modeRequested -> 真发 switch_mode
    //   （T12-3 的验收就靠这个: 从 GAME 点"睡眠"应当看到 Agent 走 GAME->IDLE->SLEEP）
    ModePanel* panel = modePanel();
    if (panel == nullptr) {
        return;
    }
    QPushButton* button = panel->buttonFor(target);
    if (button == nullptr) {
        qInfo().noquote() << QStringLiteral("[mode] --mode-demo %1：当前没有这颗按钮（现有：%2）")
                                 .arg(target).arg(panel->mode());
        return;
    }
    qInfo().noquote() << QStringLiteral("[mode] 点了一下「%1」-> switch_mode")
                             .arg(button->text());
    button->click();
}

void MainWindow::demoPickBilibili(int index)
{
    // 走**真实控件**那条路（`activateItem` 就是真点击调用的同一个入口）
    if (mainPage_ == nullptr || mainPage_->videoPanel() == nullptr) {
        return;
    }
    BilibiliPreview* preview = mainPage_->videoPanel()->preview();
    if (preview != nullptr) {
        preview->activateItem(index);
    }
}

void MainWindow::demoVideoNote(const QString& what)
{
    if (mainPage_ != nullptr && mainPage_->videoPanel() != nullptr) {
        mainPage_->videoPanel()->triggerPlaceholder(what);
    }
}

void MainWindow::setVideoFullscreen(bool on)
{
    // 全屏 = 覆盖整个屏幕：把四区域面板全藏起来，画面铺满
    if (mainPage_ == nullptr || mainPage_->videoPanel() == nullptr) {
        return;
    }
    videoFullscreen_ = on;
    const QVector<RegionHost*> hosts = regions();
    for (RegionHost* host : hosts) {
        if (host == nullptr) {
            continue;
        }
        if (on) {
            host->hideRegion(false);     // 不动画，直接藏
        } else {
            host->showRegion(false);
        }
    }
    if (!on) {
        // 退出全屏时让四区域的可见性回到"活动/锁定"该有的样子
        for (RegionHost* host : hosts) {
            if (host != nullptr && host->isActive()) {
                host->showRegion(false);
            }
        }
    }
    mainPage_->setGameMode(true);        // 保证主区在视频页
    qInfo().noquote() << (on ? "[ui] 视频全屏：隐藏四区域面板" : "[ui] 退出视频全屏：恢复面板");
}

void MainWindow::demoPlayPause()
{
    if (mainPage_ != nullptr && mainPage_->videoPanel() != nullptr) {
        mainPage_->videoPanel()->togglePlayPause();
    }
}

void MainWindow::demoFullscreen()
{
    if (mainPage_ != nullptr && mainPage_->videoPanel() != nullptr) {
        mainPage_->videoPanel()->setFullscreen(!mainPage_->videoPanel()->isFullscreen());
    }
}

void MainWindow::setConfigPath(const QString& path)
{
    configPath_ = path;
    if (modelPage_ != nullptr && !repoRoot_.isEmpty()) {
        modelPage_->setPaths(configPath_, repoRoot_);
    }
}

void MainWindow::setRepoRoot(const QString& root)
{
    repoRoot_ = root;
    if (modelPage_ != nullptr && !configPath_.isEmpty()) {
        modelPage_->setPaths(configPath_, repoRoot_);
    }
}

void MainWindow::showOnboard(const QString& why)
{
    if (onboard_ == nullptr) {
        return;
    }
    // ---- T15-1 1b：无 X 的形态（EGLFS / linuxfb）下，onboard 根本不存在 ----
    // onboard 是 GTK/X11 时代的软键盘；极小镜像（路线 B）里没有 X，所以这里**不再去找它**，
    // 直接把面板交给 Qt 的输入法（部署侧设 QT_IM_MODULE=qtvirtualkeyboard，由它接管弹出）。
    // ⚠ 别再走下面那条"探不到 onboard 就报错"的路：那在无 X 下是**正常的**，不是错误。
    if (QGuiApplication::platformName() != QLatin1String("xcb")) {
        // T15-17 bug①：eglfs 下 VK 的 Desktop 集成开不出第二个顶层窗口 ⇒ 用 Application 集成 ✓
        //   · 懒建一次 ✓；全窗 + **鼠标穿透** ⇒ 不挡任何输入 ✓
        //   · 键盘的显隐交给 QML 自己（不可见时面板停在屏外）✓ ⇒ 无需改 hideOnboard ✓
        if (vkPanel_ == nullptr) {
            vkPanel_ = new QQuickWidget(this);
            // ⚠ 这里**不能**设 WA_TransparentForMouseEvents ✗ ——
            //   板上实测：设了它 ⇒ 触摸**穿透**键盘落到下面的控件 ✓
            //   ⇒ 表现为"点键盘没反应，整个画面却在移动" ✗（底下某处收到点击/拖拽 ✓）
            //   ⇒ 键盘必须**收得到点击**才可交互 ✓，所以**不设穿透** ✓；
            //     遮挡问题改由"只盖键盘高度的底部条 + 不可见时隐藏"解决 ✓（见下 ✓）。
            // ⚠ QQuickWidget 默认不透明（白底 ✗）⇒ 必须设透明 ✓
            //   （否则会把它覆盖到的区域**整块盖白** ✗ —— 板上实测过 ✓）。
            vkPanel_->setClearColor(Qt::transparent);
            vkPanel_->setAttribute(Qt::WA_AlwaysStackOnTop, true);
            vkPanel_->setResizeMode(QQuickWidget::SizeRootObjectToView);
            vkPanel_->setSource(QUrl(QStringLiteral("qrc:/virtualkeyboard.qml")));
            vkPanel_->setVisible(false);                  // 先在隐藏态，等 visibleChanged 摆好 ✓

            // 键盘显隐 / 几何：跟着输入法可见性走 ✓（⚠ 只在这里连一次 ✓）
            // ⚠ 关键教训（本轮板上实测 ✗）：**`visibleChanged` 触发时 QML 还没布局** ✗
            //   ⇒ 当场读 `kbHeight` 会得到 0 ✗ ⇒ 几何算成 0×0 且被隐藏 ✓
            //   ⇒ 所以：**先按输入法可见性显/隐** ✓，几何则**延迟数拍重试** ✓ 读到高度再摆底部条 ✓
            if (QGuiApplication::inputMethod() != nullptr) {
                // ⚠ 为什么先给"整窗"再收成"底部条" ✗→✓：
                //   根 `Item` 若没有尺寸，QQuickWidget 里 `InputPanel` 的高度可能一直是 0 ✗
                //   （能工作的最小验证 QML 根 Item 是**有明确宽高**的 ✓，见截图 vk-min.png ✓）
                //   ⇒ 先摆满整窗让 QML 完成布局 ✓ ⇒ 再把几何收成底部一条 ✓
                auto layoutVk = [this](int attempt) {
                    if (vkPanel_ == nullptr) {
                        return;
                    }
                    QInputMethod* im2 = QGuiApplication::inputMethod();
                    const bool imVisible = (im2 != nullptr) && im2->isVisible();
                    if (!imVisible) {
                        vkPanel_->hide();     // 收起来 ⇒ 不遮挡、不拦截输入 ✓
                        return;
                    }
                    const bool haveRoot = (vkPanel_->rootObject() != nullptr);
                    const int kbH = haveRoot ? vkPanel_->rootObject()->property("kbHeight").toInt() : -1;
                    // ★ 诊断：每一次尝试都记下来 ✓（含 kbH<=0 ✗）⇒ 能分出"取不到属性"还是"高度真是 0" ✓
                    qInfo().noquote() << QStringLiteral("[ui] 键盘承载尝试 #%1：root=%2 kbHeight=%3 现几何=%4,%5 %6x%7")
                                             .arg(attempt).arg(haveRoot ? 1 : 0).arg(kbH)
                                             .arg(vkPanel_->x()).arg(vkPanel_->y())
                                             .arg(vkPanel_->width()).arg(vkPanel_->height());
                    if (kbH <= 0) {
                        // 还没布局好：**先保持整窗尺寸**（这样 QML 才有空间算高度 ✓）
                        vkPanel_->setGeometry(rect());
                        vkPanel_->show();
                        return;
                    }
                    // 只盖**底部一条** ✓ —— 用户要的"覆盖在底层画面之上、GUI 不动" ✓
                    vkPanel_->setGeometry(0, height() - kbH, width(), kbH);
                    vkPanel_->show();
                    qInfo().noquote() << QStringLiteral("[ui] 键盘承载已就位：kbHeight=%1 几何=%2,%3 %4x%5")
                                             .arg(kbH).arg(vkPanel_->x()).arg(vkPanel_->y())
                                             .arg(vkPanel_->width()).arg(vkPanel_->height());
                };
                connect(QGuiApplication::inputMethod(), &QInputMethod::visibleChanged, this,
                        [this, layoutVk]() {
                            QInputMethod* im = QGuiApplication::inputMethod();
                            if (im != nullptr && im->isVisible()) {
                                vkPanel_->setGeometry(rect());   // 先整窗 ✓ 让 QML 布局 ✓
                                vkPanel_->show();
                            } else {
                                vkPanel_->hide();                // 不遮挡、不拦截输入 ✓
                            }
                            // 延迟数拍重试：QML 布局完成后再收成"底部一条" ✓
                            int n = 0;
                            for (int d : {50, 150, 400, 900, 1800}) {
                                ++n;
                                const int k = n;
                                QTimer::singleShot(d, this, [layoutVk, k]() { layoutVk(k); });
                            }
                        });
            }
            qInfo().noquote() << QStringLiteral("[ui] 已建虚拟键盘承载（Application 集成，qrc:/virtualkeyboard.qml，覆盖式 ✓）");
        }
        if (QGuiApplication::inputMethod() != nullptr) {
            QGuiApplication::inputMethod()->show();
        }
        qInfo().noquote() << QStringLiteral("[ui] 无 X（%1）：软键盘交给 Qt 输入法（QT_IM_MODULE=%2，%3）")
                                 .arg(QGuiApplication::platformName(),
                                      qEnvironmentVariable("QT_IM_MODULE", "(未设置)"), why);
        return;
    }
    QString detail;
    if (!onboard_->available() && !onboard_->probe(&detail)) {
        qWarning().noquote() << "[ui] 软键盘不可用（" << why << "）:" << detail;
        if (mainPage_ != nullptr && mainPage_->chatPanel() != nullptr) {
            mainPage_->chatPanel()->appendSystem(
                QStringLiteral("软键盘不可用（%1）——可改用鼠标/终端输入").arg(detail));
        }
        return;
    }
    QString error;
    if (!onboard_->show(&error)) {
        qWarning().noquote() << "[ui] 软键盘弹出失败（" << why << "）:" << error;
        if (mainPage_ != nullptr && mainPage_->chatPanel() != nullptr) {
            mainPage_->chatPanel()->appendSystem(
                QStringLiteral("软键盘不可用（%1）——可改用鼠标/终端输入").arg(error));
        }
        return;
    }
    bool ok = false;
    const bool visible = onboard_->isVisible(&ok);
    qInfo().noquote() << QStringLiteral("[ui] 软键盘弹出（%1，Visible=%2）")
                             .arg(why, ok ? (visible ? QStringLiteral("true")
                                                     : QStringLiteral("false"))
                                          : QStringLiteral("未知"));
}

void MainWindow::hideOnboard(const QString& why)
{
    if (onboard_ == nullptr) {
        return;
    }
    // T15-1 1b：无 X 时同样交给 Qt 输入法（onboard 没有可收的东西）
    if (QGuiApplication::platformName() != QLatin1String("xcb")) {
        if (QGuiApplication::inputMethod() != nullptr) {
            QGuiApplication::inputMethod()->hide();
        }
        qInfo().noquote() << QStringLiteral("[ui] 无 X（%1）：收起 Qt 输入法（%2）")
                                 .arg(QGuiApplication::platformName(), why);
        return;
    }
    if (!onboard_->available() && !onboard_->probe(nullptr)) {
        return;                     // 这台机器没有 onboard：安静跳过（不是错误）
    }
    QString error;
    if (!onboard_->hide(&error)) {
        qWarning().noquote() << "[ui] 软键盘收起失败（" << why << "）:" << error;
        return;
    }
    qInfo().noquote() << QStringLiteral("[ui] 软键盘收起（%1）").arg(why);
}

void MainWindow::applyKeyboardInset()
{
    ++insetRequests_;               // G-B-6：进来的每一次都记（含重复 ✓，与"真改布局"分开数 ✓）
    // T15-17 bug①后续（用户明确要求 ✓）：eglfs 下我们的键盘是**覆盖式** ✓
    //   —— 承载用的 QQuickWidget 浮在窗口**底部一条**上 ✓ ⇒ **GUI 不移动** ✓
    //   ⇒ 这里不再给内容"让开" ✓（仍然保留上面的计数，观测语义不变 ✓）
    if (vkPanel_ != nullptr) {
        return;
    }
    QInputMethod* im = QGuiApplication::inputMethod();
    QRect kb;
    int covered = 0;
    if (im != nullptr && im->isVisible()) {
        kb = im->keyboardRectangle().toRect();          // 窗口坐标
        if (mainPage_ != nullptr && kb.height() > 0) {
            // 换成"本页被盖住多少"：键盘上沿在本页里的 y 一减就是
            const int topInPage = mainPage_->mapFrom(this, kb.topLeft()).y();
            covered = qBound(0, mainPage_->height() - topInPage, mainPage_->height());
        }
    }
    if (mainPage_ != nullptr) {
    }
    if (covered != keyboardInset_) {
        keyboardInset_ = covered;
        // T15-16 G-B-6：**只在值真的变了才往下传** ✓
        //   改前这行在判断**之前**（先调用、后判断 ✗）—— 页面那层虽有防护 ✓，
        //   但"请求"会一路走到页面函数里再被弹回来 ✓，多花几何计算 ✗。
        if (mainPage_ != nullptr) {
            mainPage_->setKeyboardInset(covered);
        }
        qInfo().noquote() << QStringLiteral("[ui] %1：虚拟键盘盖住 %2 px -> 对话区高度上限 "
                                           "让开（键盘矩形 %3,%4 %5x%6，窗口 %7x%8）")
                                 .arg(QGuiApplication::platformName())
                                 .arg(covered).arg(kb.x()).arg(kb.y()).arg(kb.width())
                                 .arg(kb.height()).arg(width()).arg(height());
    }
}

void MainWindow::onChatInputFocused(bool focused)
{
    if (OnboardCtl::shouldShow(onboardAuto_, inputSource_, focused)) {
        showOnboard(QStringLiteral("输入框获得焦点"));
        return;
    }
    if (!focused) {
        hideOnboard(QStringLiteral("输入框失焦"));
    }
}

QString MainWindow::nextConfigRequestId(const QString& prefix)
{
    ++configRequestSeq_;
    return QStringLiteral("%1-%2").arg(prefix).arg(configRequestSeq_);
}

void MainWindow::sendConfigRequest(const QString& prefix, const QJsonObject& keys,
                                   const QJsonObject& credentials)
{
    const QString requestId = nextConfigRequestId(prefix);
    const bool sent = client_ != nullptr && client_->sendSetConfig(requestId, keys, credentials);
    if (sent) {
        qInfo().noquote() << QStringLiteral("[ui] 已请 Agent 写配置（%1 个键，id=%2）")
                                 .arg(keys.size())
                                 .arg(requestId);
        return;
    }
    // ⚠ **不退回"GUI 自己写"**（你定的：唯一写入者是 Agent）——如实报"没连上"。
    QJsonObject failure;
    failure.insert(QStringLiteral("id"), requestId);
    failure.insert(QStringLiteral("ok"), false);
    failure.insert(QStringLiteral("error"),
                   QStringLiteral("Agent 没连上（命令没发出去）：设置改不了，"
                                  "先把 Agent 起起来再保存。"));
    if (prefix == QLatin1String("settings") && settingsPage_ != nullptr) {
        settingsPage_->onConfigResult(failure);
    } else if (prefix == QLatin1String("model") && modelPage_ != nullptr) {
        modelPage_->onConfigResult(failure);
    }
}

void MainWindow::sendWifiRequest(const QString& action, const QJsonObject& payload)
{
    // T14-9：与 set_config / llm_service 同一条规矩 —— GUI 不做系统动作（不自己调 nmcli），
    // 只把请求发给 Agent；没连上就**如实报**（界面显示"Agent 没在跑"），不静默失败。
    const QString requestId = nextConfigRequestId(QStringLiteral("wifi"));
    QJsonObject body = payload;
    const bool sent = client_ != nullptr && client_->sendWifiRequest(requestId, action, body);
    if (!sent) {
        QJsonObject failure;
        failure.insert(QStringLiteral("kind"), QStringLiteral("ack"));
        failure.insert(QStringLiteral("id"), requestId);
        failure.insert(QStringLiteral("action"), action);
        failure.insert(QStringLiteral("ok"), false);
        failure.insert(QStringLiteral("message"),
                       QStringLiteral("Agent 没连上（命令没发出去）：WiFi 操作要 Agent 在跑。"));
        if (settingsPage_ != nullptr) {
            settingsPage_->onWifiResult(failure);
        }
        return;
    }
    qInfo().noquote() << QStringLiteral("[ui] 已请 Agent 处理 WiFi（%1, id=%2）")
                             .arg(action, requestId);
}

void MainWindow::sendLlmServiceRequest(const QString& action)
{
    if (client_ == nullptr || !client_->connected()) {
        QJsonObject failure;
        failure.insert(QStringLiteral("action"), action);
        failure.insert(QStringLiteral("ok"), false);
        failure.insert(QStringLiteral("message"),
                       QStringLiteral("Agent 没连上（命令没发出去）：起停服务要 Agent 在跑。"));
        if (modelPage_ != nullptr) {
            modelPage_->onServiceResult(failure);
        }
        return;
    }
    QJsonObject payload;
    payload.insert(QStringLiteral("id"), nextConfigRequestId(QStringLiteral("service")));
    payload.insert(QStringLiteral("action"), action);
    client_->sendCommand(QStringLiteral("llm_service"), payload);
}

void MainWindow::startAgentService()
{
    // T14-7 的 `systemd/agent.service` 就位后这条就真能把它拉起来。
    // 现在（还没有单元）会失败 —— 如实把 systemctl 的原话显示出来，不假装起了。
    QProcess process(this);
    process.start(QStringLiteral("systemctl"),
                  {QStringLiteral("start"), QStringLiteral("agent.service")});
    if (!process.waitForFinished(10000)) {
        qWarning().noquote() << "[ui] 启动 Agent 超时（systemctl start agent.service）";
        return;
    }
    const QString output = QString::fromLocal8Bit(process.readAllStandardError()).trimmed()
                           + QString::fromLocal8Bit(process.readAllStandardOutput()).trimmed();
    if (process.exitCode() == 0) {
        qInfo().noquote() << "[ui] 已请求启动 Agent（等它自己连上来）";
    } else {
        qWarning().noquote() << "[ui] 启动 Agent 失败:" << output
                             << "(T14-7 的 systemd 单元就位前，这一步注定失败)";
    }
}

void MainWindow::applyInputType(const QString& type)
{
    inputSource_ = type;

    // 1) S10：这里**只**负责"切到命令行时把已经弹出来的键盘收掉"。
    //    弹的时机只有一个 —— 输入框拿到焦点（见 onChatInputFocused）。
    //    以前启动时就弹，一开机键盘盖住半个主区，而用户根本没打算打字。
    if (!OnboardCtl::wantsOnboard(type)) {
        hideOnboard(QStringLiteral("输入源切到命令行"));
    }

    // 命令行：只是记下选择 —— 真正的输入源由 Agent 侧合并（协议还没有切换命令）
    // (原来的 "PC"（宿主机键盘）选项已随主机输入方向一起移除, Phase 6 C4)
    if (mainPage_ != nullptr && mainPage_->chatPanel() != nullptr) {
        ChatPanel* panel = mainPage_->chatPanel();
        if (OnboardCtl::wantsOnboard(type)) {
            panel->setInputHint(QString());
        } else {
            panel->setInputHint(
                QStringLiteral("输入源「命令行」：实际输入由 Agent 侧决定（协议暂未支持切换）"));
        }
    }

    // 2) 让 **Agent** 把它写进 config.yaml 的 gui: 段（T14-3：GUI 不写文件）
    if (configPath_.isEmpty() || client_ == nullptr) {
        return;
    }
    QJsonObject keys;
    keys.insert(QStringLiteral("gui.input_source"), type);
    const QString requestId = nextConfigRequestId(QStringLiteral("input-source"));
    if (client_->sendSetConfig(requestId, keys)) {
        qInfo().noquote() << "[ui] 已请 Agent 保存输入源:" << type;
    } else {
        qWarning().noquote() << "[ui] 输入源没能保存：Agent 没连上（界面已切，配置未动）";
    }
}

bool MainWindow::sysPageVisible() const
{
    return stack_ != nullptr && sysPage_ != nullptr && stack_->currentWidget() == sysPage_;
}

ChatPanel* MainWindow::chatPanel() const
{
    return (mainPage_ != nullptr) ? mainPage_->chatPanel() : nullptr;
}

ModePanel* MainWindow::modePanel() const
{
    return (mainPage_ != nullptr) ? mainPage_->modePanel() : nullptr;
}

SchedulePanel* MainWindow::schedulePanel() const
{
    return (mainPage_ != nullptr) ? mainPage_->schedulePanel() : nullptr;
}

BottomBar* MainWindow::bottomBar() const
{
    return (mainPage_ != nullptr) ? mainPage_->bottomBar() : nullptr;
}

void MainWindow::applyMusic(const QJsonObject& data)
{
    BottomBar* bar = bottomBar();
    MusicBar* music = (bar != nullptr) ? bar->musicBar() : nullptr;
    if (music == nullptr) {
        return;
    }
    music->setMusic(data);                       // 曲目/歌手/专辑/播放状态（见 music_bar.h）
    music->setConnected(agentUp_);

    const QJsonValue duration = data.value(QStringLiteral("duration_s"));
    if (duration.isDouble()) {
        musicDuration_ = duration.toDouble();
    }
    const QJsonValue playing = data.value(QStringLiteral("playing"));
    if (playing.isBool()) {
        musicPlaying_ = playing.toBool();
    }
    const QJsonValue position = data.value(QStringLiteral("position_s"));
    if (position.isDouble()) {
        // 收到的是**真值**：重新锚定，之后靠本地时钟外推（见 musicPosition()）
        musicAnchorPosition_ = position.toDouble();
        musicAnchorMs_ = QDateTime::currentMSecsSinceEpoch();
    }
    musicLyrics_.applyPayload(data);             // 曲目/歌词没变 -> 它自己会什么都不做
    pushMusicClock();                            // 立刻画一次，别等下一秒
}

void MainWindow::tickMusic()
{
    pushMusicClock();
}

void MainWindow::pushMusicClock()
{
    double position = musicPosition();
    if (musicDuration_ > 0.0) {
        position = qMin(position, musicDuration_);
    }
    BottomBar* bar = bottomBar();
    MusicBar* music = (bar != nullptr) ? bar->musicBar() : nullptr;
    if (music == nullptr) {
        return;
    }
    music->setProgress(position, musicDuration_);
    music->setPosition(position);                // 顺带让它去问歌词"该显示哪句"
}

double MainWindow::musicPosition() const
{
    if (!musicPlaying_ || musicAnchorMs_ <= 0) {
        return musicAnchorPosition_;             // 暂停/不知道：停在锚点上不动
    }
    const qint64 elapsed = QDateTime::currentMSecsSinceEpoch() - musicAnchorMs_;
    return musicAnchorPosition_ + static_cast<double>(qMax<qint64>(0, elapsed)) / 1000.0;
}

void MainWindow::demoLyrics(const QString& which)
{
    const auto row = [](double t, const QString& text, const QString& tr) {
        QJsonObject out;
        out.insert(QStringLiteral("t"), t);
        out.insert(QStringLiteral("text"), text);
        if (!tr.isEmpty()) {
            out.insert(QStringLiteral("tr"), tr);
        }
        return out;
    };
    QJsonObject payload;
    payload.insert(QStringLiteral("track_id"), QStringLiteral("186016"));
    payload.insert(QStringLiteral("title"), QStringLiteral("晴天"));
    payload.insert(QStringLiteral("artist"), QStringLiteral("周杰伦"));
    payload.insert(QStringLiteral("album"), QStringLiteral("叶惠美"));
    payload.insert(QStringLiteral("playing"), true);
    payload.insert(QStringLiteral("position_s"), 30.0);
    payload.insert(QStringLiteral("duration_s"), 269.0);
    // ⚠ 值要同时认 ASCII 别名：板端 locale 不是 UTF-8 时，Qt 会把 argv 里的中文按
    //   8-bit 解成另一个串（实测：板上传 "有词" 匹配不上，宿主上却能）—— 验收脚本用
    //   `has|inst|none` 就绕开这件事。
    const QString key = which.trimmed().toLower();
    const bool wantedLyrics = which == QStringLiteral("有词") || key == QStringLiteral("has");
    const bool instrumental = which == QStringLiteral("纯音乐") || key == QStringLiteral("inst");
    if (wantedLyrics) {
        QJsonArray rows;
        rows.append(row(0.0, QStringLiteral("作词 : 周杰伦"), QString()));
        rows.append(row(28.95, QStringLiteral("故事的小黄花"), QStringLiteral("The little yellow flower")));
        rows.append(row(32.38, QStringLiteral("从出生那年就飘着"), QString()));
        rows.append(row(35.87, QStringLiteral("童年的荡秋千"), QString()));
        payload.insert(QStringLiteral("lyric_ok"), true);
        payload.insert(QStringLiteral("lyric_rev"), 1);
        payload.insert(QStringLiteral("lyric_lines"), rows);
    } else if (instrumental) {
        payload.insert(QStringLiteral("lyric_ok"), false);
        payload.insert(QStringLiteral("lyric_rev"), 2);
        payload.insert(QStringLiteral("lyric_reason"), QStringLiteral("没有歌词"));
    } else {
        payload.insert(QStringLiteral("lyric_ok"), false);
        payload.insert(QStringLiteral("lyric_rev"), 3);
        payload.insert(QStringLiteral("lyric_reason"), QStringLiteral("歌词取不到（PC 离线）"));
    }
    applyMusic(payload);                         // 与真推送同一条路
}

void MainWindow::demoSend(const QString& text)
{
    ChatPanel* panel = chatPanel();
    if (panel == nullptr) {
        return;
    }
    if (panel->input() != nullptr) {
        panel->input()->setText(text);
    }
    if (panel->sendButton() != nullptr) {
        panel->sendButton()->click();     // 真实点击路径（含 doSend 的全部判断）
    }
}

bool MainWindow::sendToAgent(const QString& action, const QJsonObject& payload)
{
    if (client_ == nullptr || !agentUp_) {
        // ⚠ 这类是**自动回报**（进度/格数），断了就断在这里，**不往对话区刷屏**
        //   —— 用户没点什么，却每 2 秒冒一句"没发出去"会更糟。
        qDebug().noquote() << QStringLiteral("[bilibili] 与 Agent 未连接，这条不发: %1").arg(action);
        return false;
    }
    client_->sendCommand(action, payload);
    return true;
}

void MainWindow::onMessage(const QString& topic, const QJsonObject& data)
{    const QString raw = QString::fromUtf8(QJsonDocument(data).toJson(QJsonDocument::Compact));
    view_.applyMessage(topic, data, raw);
    if (debug_) {
        // debug 只影响日志详细程度（验收要求：不要把 agent/主机细节塞在界面上）
        qInfo().noquote() << QStringLiteral("[dbg] recv %1 %2").arg(topic, raw);
    }

    if (mainPage_ != nullptr) {
        if (topic == QLatin1String("status") && view_.hasStatus()) {
            if (mainPage_->modePanel() != nullptr) {
                mainPage_->modePanel()->setMode(view_.mode());
            }
            if (mainPage_->bottomBar() != nullptr) {
                mainPage_->bottomBar()->setMode(view_.mode());   // GAME → 封面，其它 → 音乐条
            }
            mainPage_->setGameMode(view_.mode() == QLatin1String("GAME"));
        }
        if (topic == QLatin1String("wallpaper")) {
            setWallpaperFromPath(view_.wallpaperPath(), view_.wallpaperIndex());
        }
        if (topic == QLatin1String("music")) {
            applyMusic(data);                      // T15-16：一条路走到底（推送与 --lyric-demo 共用）
        }
        if (topic == QLatin1String("ota_state")) {
            // T15-14-a：设置页的「系统升级」块**只展示**（界面上没有开始升级的按钮 ✓）。
            // 状态本身不留在这里 —— MainWindow 不参与它的绘制。
            if (settingsPage_ != nullptr) {
                settingsPage_->setOtaState(data);
            }
        }
        // T11-7：B 站队列 —— 预览栏 + 地址栏 + 下区域封面，**同一份载荷**喂两处；
        //   `stream` 由视频区自己决定换不换源（板端是 FIFO 路径，不是 URL）。
        if (topic == QLatin1String("bilibili")) {
            if (mainPage_->videoPanel() != nullptr) {
                mainPage_->videoPanel()->setBilibili(data);
            }
            if (mainPage_->bottomBar() != nullptr
                && mainPage_->bottomBar()->bilibiliCover() != nullptr) {
                mainPage_->bottomBar()->bilibiliCover()->setData(data);
            }
        }
        if (topic == QLatin1String("llm") && mainPage_->chatPanel() != nullptr) {
            mainPage_->chatPanel()->setThinking(false);   // 回复到了，收起"思考中…"
            if (view_.hasLlm()) {
                // T15-17 / bug②：**区分"谁说的"** ✓ —— Agent 会把**非 gui 来源**（例如命令行 ✓）
                // 的输入回显成 `llm{text, role:"user"}` ✓ ⇒ 那种要显示成**用户气泡** ✓，
                // 其余（**没有** role ✓）= 助手回复 ⇒ 照旧助手气泡 ✓ ⇒ **向后兼容** ✓✓
                //（老 Agent 不推 role ⇒ 行为与今天一模一样 ✓）。判据见 test_main_window ✓。
                if (data.value(QStringLiteral("role")).toString() == QLatin1String("user")) {
                    mainPage_->chatPanel()->appendUser(view_.llmText());
                } else {
                    mainPage_->chatPanel()->appendAssistant(view_.llmText());
                }
            }
        }
    }
    refreshLinkState();
}

void MainWindow::refreshLinkState()
{
    if (topBar_ == nullptr) {
        return;
    }
    const bool hostKnown = view_.hasStatus();
    const bool hostUp = hostKnown && view_.connected();

    // 三态合一：Agent 链路 + 串流主机 → 一个结论
    TopBar::LinkState state = TopBar::LinkState::Disconnected;
    if (!agentUp_) {
        state = everConnected_ ? TopBar::LinkState::Reconnecting
                               : TopBar::LinkState::Disconnected;
    } else if (hostKnown && !hostUp) {
        state = TopBar::LinkState::Reconnecting;   // Agent 在，但主机还没就绪
    } else {
        state = TopBar::LinkState::Connected;      // 主机状态未知时先算已连接
    }

    topBar_->setLinkState(state);
    topBar_->setMode(view_.hasStatus() ? view_.mode() : QString());
    if (mainPage_ != nullptr && mainPage_->chatPanel() != nullptr) {
        // 只有"已连接"允许发消息；否则对话区顶部挂提示条并禁用发送
        mainPage_->chatPanel()->setLinkUp(state == TopBar::LinkState::Connected);
    }
    if (sysPage_ != nullptr) {
        // T10：系统页也显示同一结论（串流主机）
        sysPage_->setStreamHostState(
            (state == TopBar::LinkState::Connected)
                ? QStringLiteral("已连接")
                : (state == TopBar::LinkState::Reconnecting ? QStringLiteral("重连中（主机未就绪）")
                                                            : QStringLiteral("未连接")));
    }

    if (state != lastLinkState_) {
        const QString agentText = agentUp_
                                      ? QStringLiteral("已连接")
                                      : (everConnected_ ? QStringLiteral("重连中")
                                                        : QStringLiteral("未连接"));
        const QString hostText = !hostKnown ? QStringLiteral("未知")
                                            : (hostUp ? QStringLiteral("已连接")
                                                      : QStringLiteral("未连接"));
        const QString stateText = (state == TopBar::LinkState::Connected)
                                      ? QStringLiteral("已连接")
                                      : (state == TopBar::LinkState::Reconnecting
                                             ? QStringLiteral("重连中")
                                             : QStringLiteral("未连接"));
        // agent/主机两个细节**只进日志**，不在界面显示（T4 验收要求）
        qInfo().noquote() << QStringLiteral("[ui] 连接: %1 (agent=%2, 主机=%3)")
                                 .arg(stateText, agentText, hostText);
        lastLinkState_ = state;
    }
}

void MainWindow::startIpc(const QString& path)
{
    if (client_ != nullptr) {
        client_->start(path);
    }
}

void MainWindow::showEvent(QShowEvent* event)
{
    QMainWindow::showEvent(event);
    if (wakeArmed_ || watcher_ == nullptr) {
        return;
    }
    wakeArmed_ = true;
    // 应用启动（GL/配置/IPC 连接）耗掉的时间不该算成"用户空闲"，从可见这一刻起计
    watcher_->notifyActivity();
    if (overlayWatcher_ != nullptr) {
        overlayWatcher_->notifyActivity();   // 内嵌控制条同样从可见这一刻起计
    }
}

QVector<RegionHost*> MainWindow::regions() const
{
    QVector<RegionHost*> out;
    out << topHost_ << navHost_;
    if (mainPage_ != nullptr) {
        out << mainPage_->bottomRegion() << mainPage_->rightRegion();
    }
    return out;
}

void MainWindow::applyConfig(const core::ConfigStore& gui)
{
    // debug 现在**只控制日志详细程度**（界面不再显示 [D]，见 T4 验收意见）
    debug_ = gui.boolValue(QStringLiteral("gui.debug"), false);
    refreshLinkState();

    // T6：输入源（pc / terminal / keyboard）+ 是否真去弹软键盘
    onboardAuto_ = gui.boolValue(QStringLiteral("gui.onboard_auto"), true);
    const QString inputSource =
        gui.value(QStringLiteral("gui.input_source"), QStringLiteral("keyboard"));
    inputSource_ = inputSource;      // S10：焦点策略要用它判断"要不要弹键盘"
    if (mainPage_ != nullptr && mainPage_->chatPanel() != nullptr) {
        // 只是把界面对齐配置；**不**在这里弹键盘（弹的时机是输入框获得焦点）
        mainPage_->chatPanel()->setInputType(inputSource);
    }

    if (settingsPage_ != nullptr) {
        // ⚠ T15-17 修正（真机 journal 查实 ✗→✓）：`loadFromConfig()` **必须放在下面三处 connect 之后** ✗。
        //   原来它在这里 ⇒ 它回填控件时触发的 `guiSettingsEdited` **先于接线** ✓
        //   ⇒ 收端收不到 ⇒ 真机上**从来没有**那句「设置页改动已即时套用」的日志 ✗
        //   （无害 ✓：启动套用由 `applyConfig()` 自己接着做 ✓；但对将来往 lambda 里加逻辑的人是**静默陷阱** ✗）。
        // ⚠ 如实说 ✓：修好之后**唯一可见的差别是那句日志** ✓（值本来就一样 ✓）⇒ 它是**正确性卫生** ✓，
        //   我**不声称**有会红的判据盯着它 ✗。
        // T14-3：页面只发"要改哪些键"，落盘交给 Agent（唯一写入者，见 docs/adr/0005）。
        connect(settingsPage_, &SettingsPage::saveRequested, this,
                [this](QJsonObject keys, QJsonObject credentials) {
                    sendConfigRequest(QStringLiteral("settings"), keys, credentials);
                }, Qt::UniqueConnection);
        connect(settingsPage_, &SettingsPage::startAgentRequested, this,
                [this]() { startAgentService(); }, Qt::UniqueConnection);
        // T14-9：网络卡片的请求（status/scan/connect/forget/autoconnect/reconnect）
        connect(settingsPage_, &SettingsPage::wifiRequested, this,
                [this](QString action, QJsonObject payload) {
                    sendWifiRequest(action, payload);
                }, Qt::UniqueConnection);
        // T15-17 / T3：**改动即预览** ✓ —— 设置页一改（**还没落盘** ✓）就当场重放一次 ✓。
        // ⚠⚠ **必须以磁盘那份为底** ✗：`reapplyGuiConfig()` 对每个键都带 fallback ✓，
        //     若内存里只有改动的那几个键 ✗ ⇒ **别的键会走默认值** ✗✗（等于"改一处、悄悄重置别处" ✗）
        //     ⇒ 所以先 `load(configPath_)` 铺底 ✓，再叠加界面上的当前值 ✓。
        // ⚠ 不落盘 ✓：写盘仍要按「保存」✓（唯一写入者 = Agent ✓，见 docs/adr/0005）。
        connect(settingsPage_, &SettingsPage::guiSettingsEdited, this, [this]() {
            core::ConfigStore store;
            if (!configPath_.isEmpty()) {
                store.load(configPath_);          // 底：磁盘 ✓（读不到也不致命 ✓，只是少一层底 ✓）
            }
            const auto modeOf = [this](const QString& region) {
                QComboBox* box = settingsPage_->regionMode(region);
                return box != nullptr ? box->currentData().toString() : QString();
            };
            const auto putIfSet = [&store](const QString& key, const QString& value) {
                if (!value.isEmpty()) {
                    store.set(key, value);
                }
            };
            putIfSet(QStringLiteral("gui.wake.top"), modeOf(QStringLiteral("top")));
            putIfSet(QStringLiteral("gui.wake.bottom"), modeOf(QStringLiteral("bottom")));
            putIfSet(QStringLiteral("gui.wake.left"), modeOf(QStringLiteral("left")));
            putIfSet(QStringLiteral("gui.wake.right"), modeOf(QStringLiteral("right")));
            if (settingsPage_->regionIdleSpin() != nullptr) {
                store.set(QStringLiteral("gui.wake.idle_ms"),
                          QString::number(settingsPage_->regionIdleSpin()->value()));
            }
            if (settingsPage_->overlayMode() != nullptr) {
                putIfSet(QStringLiteral("gui.video_overlay.mode"),
                         settingsPage_->overlayMode()->currentData().toString());
            }
            if (settingsPage_->overlayIdleSpin() != nullptr) {
                store.set(QStringLiteral("gui.video_overlay.idle_ms"),
                          QString::number(settingsPage_->overlayIdleSpin()->value()));
            }
            // T15-17 / T3b：通用卡片里那两项 ✓（`startPage_` 故意不覆盖 ✗ —— 它不在本函数读的键里 ✓）
            if (settingsPage_->debugCheck() != nullptr) {
                store.setBool(QStringLiteral("gui.debug"), settingsPage_->debugCheck()->isChecked());
            }
            if (settingsPage_->inputSourceBox() != nullptr) {
                putIfSet(QStringLiteral("gui.input_source"),
                         settingsPage_->inputSourceBox()->currentData().toString());
            }
            reapplyGuiConfig(store);
            qInfo().noquote() << QStringLiteral(
                "[ui] 设置页改动已**即时套用** ✓（未落盘 ✓ —— 按「保存」才写盘 ✓）");
        }, Qt::UniqueConnection);

        // ⚠ 放到**这三处 connect 之后** ✓（见本块开头那段 ✗→✓）：这样它回填控件时触发的信号
        //   **收端已就位** ✓ ⇒ 启动期也能看到「即时套用」日志 ✓ ⇒ 以后一眼可查 ✓。
        settingsPage_->loadFromConfig(configPath_);
    }
    if (modelPage_ != nullptr) {
        connect(modelPage_, &ModelPage::configSaveRequested, this,
                [this](QJsonObject keys, QJsonObject credentials) {
                    sendConfigRequest(QStringLiteral("model"), keys, credentials);
                }, Qt::UniqueConnection);
        connect(modelPage_, &ModelPage::serviceRequested, this,
                [this](QString action) { sendLlmServiceRequest(action); }, Qt::UniqueConnection);
    }

    // T15-17：套用交给 reapplyGuiConfig() —— **一份实现** ✓（启动与在线改动走同一条路 ✓）。
    reapplyGuiConfig(gui);
}

void MainWindow::reapplyGuiConfig(const core::ConfigStore& gui)
{
    const auto isActive = [&gui](const QString& key, bool fallback) {
        const QString value = gui.value(QStringLiteral("gui.wake.") + key);
        if (value.isEmpty()) {
            return fallback;
        }
        return value.compare(QLatin1String("active"), Qt::CaseInsensitive) == 0;
    };

    // 统一休眠时间：四个活动区域共用这一个值（方案 §4）
    watcher_->setIdleMs(gui.intValue(QStringLiteral("gui.wake.idle_ms"), 5000));

    // T10：系统页刷新间隔（GUI 私有项，不同步到其它配置）
    if (monitorTimer_ != nullptr) {
        const int intervalMs = gui.intValue(QStringLiteral("gui.monitor_interval_ms"), 1000);
        monitorTimer_->setInterval(intervalMs > 200 ? intervalMs : 200);
        // T15-16 G-B-4：改完间隔要**按可见性**决定启停 ✓（否则会把刚停掉的又拉起来 ✗）
        if (sysPageVisible()) {
            monitorTimer_->start();
        } else {
            monitorTimer_->stop();
        }
        qInfo().noquote() << QStringLiteral("[gui] 系统页刷新间隔: %1ms").arg(intervalMs);
    }
    // 默认：上/右 锁定（常显），左/下 活动（空闲折叠）
    if (topHost_ != nullptr) {
        topHost_->setActive(isActive(QStringLiteral("top"), false));
    }
    if (navHost_ != nullptr) {
        navHost_->setActive(isActive(QStringLiteral("left"), true));
    }
    if (mainPage_ != nullptr) {
        if (mainPage_->bottomRegion() != nullptr) {
            mainPage_->bottomRegion()->setActive(isActive(QStringLiteral("bottom"), true));
        }
        if (mainPage_->rightRegion() != nullptr) {
            mainPage_->rightRegion()->setActive(isActive(QStringLiteral("right"), false));
        }
    }

    // T9：内嵌控制条的 活动/锁定 + **自己的**休眠时间（不与 wake.idle_ms 共享）
    if (overlayWatcher_ != nullptr) {
        const QString overlayMode = gui.value(QStringLiteral("gui.video_overlay.mode"),
                                              QStringLiteral("active"));
        overlayAutoHide_ = overlayMode.compare(QLatin1String("active"), Qt::CaseInsensitive) == 0;
        overlayWatcher_->setIdleMs(gui.intValue(QStringLiteral("gui.video_overlay.idle_ms"), 3000));
        overlayWatcher_->setEnabled(overlayAutoHide_);
        if (!overlayAutoHide_ && mainPage_ != nullptr && mainPage_->videoPanel() != nullptr) {
            mainPage_->videoPanel()->setOverlayVisible(true);   // 锁定 = 常显
        }
        qInfo().noquote() << QStringLiteral("[gui] 视频控制条: %1 (idle_ms=%2，独立于 wake.idle_ms)")
                                 .arg(overlayAutoHide_ ? QStringLiteral("活动") 
                                                       : QStringLiteral("锁定"))
                                 .arg(gui.intValue(QStringLiteral("gui.video_overlay.idle_ms"), 3000));
    }

    // S5：日程区跟着配置一起刷新（gui.schedule.max_rows 在这里生效）
    reloadSchedule();
}

void MainWindow::reloadSchedule()
{
    if (mainPage_ == nullptr || mainPage_->schedulePanel() == nullptr) {
        return;
    }
    if (configPath_.isEmpty()) {
        core::ScheduleResult missing;
        missing.ok = false;
        missing.error = QStringLiteral("还没找到 config/config.yaml");
        mainPage_->schedulePanel()->setSchedule(missing, kDefaultScheduleRows);
        return;
    }

    int maxRows = kDefaultScheduleRows;
    core::ConfigStore store;
    QString error;
    if (store.load(configPath_, &error)) {
        maxRows = store.intValue(QStringLiteral("gui.schedule.max_rows"), kDefaultScheduleRows);
    }

    const core::ScheduleResult result =
        core::ScheduleModel::loadWindowed(configPath_, QDateTime::currentDateTime());
    mainPage_->schedulePanel()->setSchedule(result, maxRows);
    qInfo().noquote() << QStringLiteral("[gui] 日程: %1 行（配置 %2 条，最多显示 %3 行）%4")
                             .arg(result.totalRows())
                             .arg(result.totalInConfig)
                             .arg(maxRows)
                             .arg(result.problems.isEmpty()
                                      ? QString()
                                      : QStringLiteral("，%1 条读不出来")
                                            .arg(result.problems.size()));
}

QString MainWindow::wakeStateText() const
{
    QStringList parts;
    const auto add = [&parts](const QString& name, const RegionHost* host) {
        if (host == nullptr) {
            return;
        }
        parts << QStringLiteral("%1=%2/%3").arg(
            name, host->isCollapsed() ? QStringLiteral("折叠") : QStringLiteral("展开"),
            host->isActive() ? QStringLiteral("活动") : QStringLiteral("锁定"));
    };
    add(QStringLiteral("上"), topHost_);
    add(QStringLiteral("下"), mainPage_ != nullptr ? mainPage_->bottomRegion() : nullptr);
    add(QStringLiteral("左"), navHost_);
    add(QStringLiteral("右"), mainPage_ != nullptr ? mainPage_->rightRegion() : nullptr);
    return parts.join(QStringLiteral("  "));
}

QWidget* MainWindow::buildTopBar()
{
    topBar_ = new TopBar(this);
    return topBar_;
}

QWidget* MainWindow::buildNavBar()
{
    auto* bar = new QFrame(this);
    bar->setObjectName(QStringLiteral("NavBar"));
    bar->setFixedWidth(96);

    auto* box = new QVBoxLayout(bar);
    box->setContentsMargins(8, 16, 8, 16);   // 圆角卡片的内边距
    box->setSpacing(12);                     // 验收：左侧几项过于紧凑 → 放松间距

    const QVector<PageEntry>& entries = pageEntries();
    for (int i = 0; i < entries.size(); ++i) {
        const PageEntry& entry = entries.at(i);
        // "设置" 沉到底部：前面插一条伸缩
        if (entry.key == QLatin1String("settings")) {
            auto* sep = new QFrame(bar);
            sep->setObjectName(QStringLiteral("NavSep"));
            sep->setFixedHeight(1);
            sep->setStyleSheet(QStringLiteral("background:%1;").arg(QLatin1String(theme::kDivider)));
            box->addStretch(1);
            box->addWidget(sep);
        }

        // 验收：导航只留图标（文字去掉）——名字放 tooltip，触屏也就够了
        auto* button = new QPushButton(QString(), bar);
        button->setToolTip(entry.label);
        button->setObjectName(QStringLiteral("NavButton"));
        button->setCheckable(true);
        button->setAutoExclusive(true);
        button->setMinimumHeight(68);           // 触屏：导航项 ≥ 68 高（方案 §8 ≥88 含间距）
        const QString key = entry.key;
        connect(button, &QPushButton::clicked, this, [this, key]() { showPage(key); });

        // T14：自绘单色图标（导航用的是染成与文字同色的版本）
        const QString iconName = (entry.key == QLatin1String("home"))
                                     ? QStringLiteral("home")
                                     : (entry.key == QLatin1String("model")
                                            ? QStringLiteral("model")
                                            : (entry.key == QLatin1String("system")
                                                   ? QStringLiteral("system")
                                                   : QStringLiteral("settings")));
        button->setIcon(ui::icon(iconName));
        button->setIconSize(QSize(28, 28));   // 没有文字了，图标可以放大些
        navButtons_.append(button);
        navKeys_.append(key);
        box->addWidget(button);
    }
    return bar;
}

bool MainWindow::showPage(const QString& key)
{
    for (int i = 0; i < navKeys_.size(); ++i) {
        if (navKeys_.at(i) == key) {
            stack_->setCurrentIndex(i);
            navButtons_.at(i)->setChecked(true);
            return true;
        }
    }
    return false;
}

QString MainWindow::currentPage() const
{
    const int index = stack_ ? stack_->currentIndex() : -1;
    return (index >= 0 && index < navKeys_.size()) ? navKeys_.at(index) : QString();
}
