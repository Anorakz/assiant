// ============================================================================
//  gui/src/main_window.cpp — 主窗口框架实现（T1 骨架 + T3 唤醒接线）
//
//  1. 上区域通栏顶栏（T4 填内容）、左区域导航栏 + 页面栈
//  2. 四区域包进 RegionHost，接 IdleWatcher：空闲折叠 / 点击唤醒（T3）
//  3. 一份**最小**高级灰样式（配色见方案 §8），T15 再统一打磨
// ============================================================================
#include "main_window.h"

#include "core/bilibili_format.h"
#include "core/config_store.h"
#include "core/idle_watcher.h"
#include "core/image_fit.h"
#include "core/schedule_model.h"
#include "services/local_client.h"
#include "services/onboard_ctl.h"
#include "ui/bilibili_cover.h"
#include "ui/bilibili_preview.h"
#include "ui/bottom_bar.h"
#include "ui/chat_panel.h"
#include "ui/cover_loader.h"
#include "ui/mode_panel.h"
#include "ui/music_bar.h"
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

/// 方案 §8 配色：底 #1E1F22 / 面板 #2B2D31 / 分隔 #3A3D42 / 主文字 #E6E6E6 /
/// 次文字 #9AA0A6 / 强调 #7AA2F7。
const char* const kBaseStyle = R"(
QWidget { color: #E6E6E6; font-size: 20px; }
/* ⚠ 底色只给顶层窗口：子控件一律默认透明，卡片自己的 #2B2D31 才能透出来。
   以前写成 `QWidget { background: #1E1F22 }` 会把卡片内部刷成页面底色
   （T7 量色带时发现音乐卡内部不是面板色）。 */
QMainWindow { background: #1E1F22; }
QStackedWidget#PageStack { background: transparent; }
QFrame#TopBar { background: rgba(43, 45, 49, 0.90); border: none; border-bottom: 1px solid #3A3D42; }
QFrame#NavBar { background: rgba(35, 36, 40, 0.90); border: none; border-radius: 12px; }
/* ⚠ 半透明：方案 §3 的全局壁纸要能透过面板看到（T8） */
QFrame#AreaFrame { background: rgba(43, 45, 49, 0.90); border: 1px solid #3A3D42; border-radius: 8px; }
/* 主区：非游戏模式不画背景与边框，完全留给全局壁纸（T8 接壁纸后即可见效果） */
QFrame#AreaFrameBare { background: transparent; border: none; }
QLabel#AreaTitle { color: #9AA0A6; font-size: 20px; font-weight: bold; background: transparent; }
QLabel#AreaHint { color: #6F757C; font-size: 15px; background: transparent; }
/* S5：日程区。时间列等宽一点（用同一档字号 + 固定最小宽度，见 SchedulePanel），
   标题比提示亮一档 —— 和对话区气泡的层次保持一致。 */
QLabel#ScheduleTime { color: #9AA0A6; font-size: 16px; background: transparent; }
QLabel#ScheduleTitle { color: #E6E6E6; font-size: 17px; background: transparent; }
/* 主区占位文字（T8/T9 落地后删除），做得很淡以免干扰壁纸 */
QLabel#AreaTitleBare { color: #4A4E54; font-size: 18px; background: transparent; }
QLabel#AreaHintBare { color: #3A3D42; font-size: 14px; background: transparent; }
QLabel#TopBarText { color: #9AA0A6; font-size: 18px; background: transparent; }
QLabel#TopBarClock { color: #E6E6E6; font-size: 20px; background: transparent; }
QPushButton#NavButton {
    background: transparent; border: none; color: #9AA0A6;
    /* T14：加了图标后 96px 宽里要放"图标+4 个汉字"，字号与内边距都得收 */
    padding: 14px 2px; font-size: 15px; text-align: center;
}
QPushButton#NavButton:hover { color: #E6E6E6; }
QPushButton#NavButton:checked {
    color: #E6E6E6; background: #2B2D31; border-left: 4px solid #7AA2F7;
}
/* T5：模式切换按钮区 */
QPushButton#ModeButton {
    background: #2B2D31; border: 1px solid #3A3D42; border-radius: 8px;
    padding: 4px 10px; font-size: 18px; color: #E6E6E6; text-align: center;
}
QPushButton#ModeButton:hover { background: #35373B; }
QPushButton#ModeButton:pressed { background: #7AA2F7; color: #1E1F22; }
/* T5：对话区 */
/* ⚠ 这条必须写在气泡规则之前并保持"只清列表背景"的语义：
   它的特异度是 (0,1,2)，写成 QWidget#ChatList QWidget 会盖掉 (0,1,1) 的气泡背景，
   导致用户气泡变透明 + 深色文字看不见（T5 出图时踩到）。所以气泡规则要带祖先前缀。 */
QWidget#ChatList, QWidget#ChatList QWidget { background: transparent; }
QWidget#ChatList QLabel#ChatBubbleUser { background: #7AA2F7; color: #1E1F22; border-radius: 10px;
                                         padding: 10px 12px; font-size: 17px; }
QWidget#ChatList QLabel#ChatBubbleAssistant { background: #35373B; color: #E6E6E6; border-radius: 10px;
                                              padding: 10px 12px; font-size: 17px; }
QLabel#ChatSystem { color: #9AA0A6; font-size: 15px; background: transparent; }
QLabel#LinkBanner { background: #F59E0B; color: #1E1F22; padding: 6px 10px;
                    border-radius: 6px; font-size: 15px; }
QLineEdit#ChatInput { background: #232428; border: 1px solid #3A3D42; border-radius: 8px;
                      padding: 8px 12px; color: #E6E6E6; font-size: 18px; }
QPushButton#ChatSend { background: #7AA2F7; color: #1E1F22; border: none; border-radius: 8px;
                       padding: 8px 16px; font-size: 18px; font-weight: bold; }
QPushButton#ChatSend:disabled { background: #3A3D42; color: #6F757C; }
/* T6：输入源小按钮 */
QToolButton#InputTypeButton {
    background: #232428; border: 1px solid #3A3D42; border-radius: 8px;
    padding: 6px 10px; color: #E6E6E6; font-size: 16px;
}
QToolButton#InputTypeButton:hover { background: #2B2D31; }
QToolButton#InputTypeButton::menu-indicator { image: none; }
QScrollArea#ChatScroll { background: transparent; border: none; }
/* T7：下区域音乐条 */
QLabel#MusicTitle { color: #E6E6E6; font-size: 20px; font-weight: bold; background: transparent; }
QLabel#MusicPlaceholder { color: #6F757C; font-size: 16px; background: transparent; }
QLabel#MusicState { background: transparent; }
QLabel#PlaceholderTag {
    background: #3A3D42; color: #9AA0A6; border-radius: 4px;
    padding: 1px 6px; font-size: 13px;
}
QProgressBar#MusicProgress {
    background: #232428; border: 1px solid #3A3D42; border-radius: 5px;
}
QProgressBar#MusicProgress::chunk { background: #7AA2F7; border-radius: 4px; }
/* T7 调整：控制按钮（协议无控制命令，点了只给"未接入"说明） */
QPushButton#MusicCtl {
    background: #2B2D31; border: 1px solid #3A3D42; border-radius: 10px;
    padding: 0; color: #E6E6E6; font-size: 18px;
}
QPushButton#MusicCtl:hover { background: #35373B; }
QPushButton#MusicCtlMain {
    background: #2B2D31; border: 1px solid #7AA2F7; border-radius: 28px;
    padding: 0; color: #E6E6E6; font-size: 24px;
}
QPushButton#MusicCtlMain:hover { background: #35373B; }
QWidget#CoverPage { background: transparent; }
/* T11：模型测试页 */
QRadioButton#ModeRadio { color: #E6E6E6; font-size: 17px; spacing: 8px; }
QPlainTextEdit#ModelLog {
    background: rgba(20, 21, 24, 0.72); border: 1px solid #33363B; border-radius: 8px;
    color: #C9CED6; font-size: 14px; padding: 6px;
}
QLabel#BenchBanner {
    background: rgba(245, 158, 11, 0.16); border: 1px solid #F59E0B; border-radius: 6px;
    color: #F59E0B; padding: 6px 10px; font-size: 15px;
}
QSpinBox, QDoubleSpinBox, QLineEdit {
    background: rgba(35, 36, 40, 0.9); border: 1px solid #3A3D42; border-radius: 6px;
    color: #E6E6E6; padding: 4px 8px; font-size: 15px; selection-background-color: #4A5568;
}
QSpinBox:disabled, QDoubleSpinBox:disabled { color: #6F757C; }
QComboBox {
    background: rgba(35, 36, 40, 0.92); border: 1px solid #3A3D42; border-radius: 6px;
    color: #E6E6E6; padding: 4px 8px; font-size: 15px; min-height: 24px;
}
QComboBox::drop-down { border: none; width: 22px; }
QComboBox QAbstractItemView {
    background: #232428; color: #E6E6E6; border: 1px solid #3A3D42;
    selection-background-color: #3A3D42; font-size: 15px;
}
QCheckBox { color: #E6E6E6; font-size: 16px; spacing: 8px; }
QComboBox#ModelCombo {
    background: rgba(35, 36, 40, 0.9); border: 1px solid #3A3D42; border-radius: 6px;
    color: #E6E6E6; padding: 4px 8px; font-size: 15px;
}
/* 系统页的看门狗按钮（T7-3：原先是借「下一张」那条规则的，现在自己有一条） */
QPushButton#WatchdogButton {
    background: rgba(43, 45, 49, 0.85); border: 1px solid #3A3D42; border-radius: 8px;
    padding: 6px 14px; color: #9AA0A6; font-size: 16px;
}
QPushButton#WatchdogButton:hover { color: #E6E6E6; background: rgba(53, 55, 59, 0.92); }
/* T9：视频区 */
QStackedWidget#VideoStage { background: transparent; }
QLabel#VideoPlaceholder { color: #6F757C; font-size: 22px; background: transparent; }
QPushButton#VideoCtl {
    background: rgba(20, 21, 24, 0.72); border: 1px solid rgba(255, 255, 255, 0.10);
    border-radius: 8px; padding: 0; color: #E6E6E6; font-size: 18px;
}
QPushButton#VideoCtl:hover { background: rgba(40, 42, 47, 0.86); }
QPushButton#VideoCtlPlay {
    background: rgba(122, 162, 247, 0.82); border: 1px solid rgba(255, 255, 255, 0.14);
    border-radius: 20px; padding: 0; color: #12141A; font-size: 18px; font-weight: bold;
}
QPushButton#VideoCtlPlay:hover { background: rgba(140, 176, 250, 0.92); }
QToolButton#VideoSpeed {
    background: rgba(20, 21, 24, 0.72); border: 1px solid rgba(255, 255, 255, 0.10);
    border-radius: 8px; padding: 0; color: #D6DAE0; font-size: 16px;
}
QToolButton#VideoSpeed:hover { background: rgba(40, 42, 47, 0.86); }
QToolButton#VideoSpeed::menu-indicator { image: none; }
/* T11-7：预览栏 + 地址栏 + 下区域封面 */
QWidget#BilibiliPreview { background: transparent; }
QLabel#BilibiliCaption { color: #9AA0A6; font-size: 14px; background: transparent; }
QLineEdit#BilibiliAddress {
    background: rgba(20, 21, 24, 0.72); border: 1px solid #3A3D42; border-radius: 6px;
    color: #C9CED6; font-size: 14px; padding: 2px 8px;
}
QLineEdit#BilibiliAddress:read-only { color: #9AA0A6; }
QLabel#BilibiliSource { color: #7AA2F7; font-size: 14px; background: transparent; }
QLabel#BilibiliHint { color: #6F757C; font-size: 15px; background: transparent; padding-left: 4px; }
QListWidget#BilibiliList {
    background: transparent; border: none; outline: none;
}
QListWidget#BilibiliList::item {
    background: rgba(20, 21, 24, 0.62); border: 1px solid #33363B; border-radius: 8px;
    color: #C9CED6; font-size: 12px; padding: 2px;
}
QListWidget#BilibiliList::item:selected {
    border: 1px solid #7AA2F7; background: rgba(122, 162, 247, 0.18); color: #E6E6E6;
}
QWidget#BilibiliCover { background: transparent; }
QLabel#BilibiliCoverImage {
    background: rgba(20, 21, 24, 0.62); border: 1px solid #33363B; border-radius: 8px;
    color: #6F757C; font-size: 15px;
}
QLabel#BilibiliCoverTitle { color: #E6E6E6; font-size: 19px; font-weight: bold; background: transparent; }
QLabel#BilibiliCoverMeta { color: #9AA0A6; font-size: 15px; background: transparent; }
QLabel#BilibiliCoverPosition { color: #9AA0A6; font-size: 14px; background: transparent; }
QLabel#BilibiliCoverSource { color: #7AA2F7; font-size: 14px; background: transparent; }
/* 内嵌控制条：**没有整条背景**，只有一个半透明胶囊（验收：不要实体化） */
QWidget#VideoOverlay { background: transparent; }
QWidget#VideoPill {
    background: rgba(20, 21, 24, 0.42); border-radius: 22px;
}
QWidget#VideoPage { background: #0B0C0E; }
QStackedWidget#VideoStage { background: #0B0C0E; }
/* T10：系统页指标瓷砖 */
QFrame#SysTile {
    background: rgba(35, 36, 40, 0.72); border: 1px solid #33363B; border-radius: 8px;
}
QLabel#SysValue { color: #E6E6E6; font-size: 22px; font-weight: bold; background: transparent; }
QLabel#SysValueSmall { color: #E6E6E6; font-size: 17px; background: transparent; }
QLabel#SysLabel { color: #8A9099; font-size: 15px; background: transparent; }
)";

} // namespace

MainWindow::MainWindow(QWidget* parent)
    : QMainWindow(parent)
{
    setWindowTitle(QStringLiteral("板端助手"));
    setStyleSheet(QString::fromUtf8(kBaseStyle));

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
    connect(client_, &LocalClient::bilibiliReceived, this,
            [this](const QJsonObject& d) { onMessage(QStringLiteral("bilibili"), d); });
    // T14-3：两条回执按请求 id 的路由（`<prefix>-<n>`）—— 页面自己不认识就忽略
    connect(client_, &LocalClient::configResultReceived, this, [this](const QJsonObject& d) {
        const QString id = d.value(QStringLiteral("id")).toString();
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
    });
    connect(client_, &LocalClient::serviceResultReceived, this, [this](const QJsonObject& d) {
        if (modelPage_ != nullptr) {
            modelPage_->onServiceResult(d);
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
        if (sysPage_ != nullptr && stack_ != nullptr && stack_->currentWidget() == sysPage_) {
            sysPage_->refresh();
        }
    });
    monitorTimer_->setInterval(1000);   // applyConfig() 里会按配置改写
    monitorTimer_->start();

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
                wallpaperFade_ = value.toReal();
                update();
            });
    connect(wallpaperAnim_, &QVariantAnimation::finished, this, [this]() {
        prevWallpaper_ = QPixmap();      // 淡入结束就把旧图丢掉
        wallpaperFade_ = 1.0;
        update();
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
    painter.fillRect(target, QColor(0x1E, 0x1F, 0x22));   // 兜底底色（也算"占位底图"）

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

void MainWindow::demoPlaceholderNote(const QString& what)
{
    BottomBar* bar = bottomBar();
    if (bar != nullptr && bar->musicBar() != nullptr) {
        bar->musicBar()->triggerPlaceholder(what);
    }
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
        if (topic == QLatin1String("music") && mainPage_->bottomBar() != nullptr
            && mainPage_->bottomBar()->musicBar() != nullptr) {
            mainPage_->bottomBar()->musicBar()->setMusic(view_.musicTitle(), view_.musicPlaying());
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
                mainPage_->chatPanel()->appendAssistant(view_.llmText());
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
        settingsPage_->loadFromConfig(configPath_);
        // T14-3：页面只发"要改哪些键"，落盘交给 Agent（唯一写入者，见 docs/adr/0005）。
        connect(settingsPage_, &SettingsPage::saveRequested, this,
                [this](QJsonObject keys, QJsonObject credentials) {
                    sendConfigRequest(QStringLiteral("settings"), keys, credentials);
                }, Qt::UniqueConnection);
        connect(settingsPage_, &SettingsPage::startAgentRequested, this,
                [this]() { startAgentService(); }, Qt::UniqueConnection);
    }
    if (modelPage_ != nullptr) {
        connect(modelPage_, &ModelPage::configSaveRequested, this,
                [this](QJsonObject keys, QJsonObject credentials) {
                    sendConfigRequest(QStringLiteral("model"), keys, credentials);
                }, Qt::UniqueConnection);
        connect(modelPage_, &ModelPage::serviceRequested, this,
                [this](QString action) { sendLlmServiceRequest(action); }, Qt::UniqueConnection);
    }

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
            sep->setStyleSheet(QStringLiteral("background:#3A3D42;"));
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
