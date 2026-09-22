// ============================================================================
//  gui/src/main_window.h — 主窗口框架（上区域通栏 + 左区域导航 + 页面栈）
//
//  布局（方案 §2 v5）：
//      ┌───────────────────────────────────────────────────────────┐
//      │ 上区域·通栏 72px：连接状态 / 模式徽标 / 串流 / 时钟 / [D]  │
//      ├────────┬──────────────────────────────────────────────────┤
//      │ 左区域 │ QStackedWidget：主页面 / 模型测试 / 系统 / 设置    │
//      │ 96px   │   （主页面内部再分 中部[主区+下区域] 与 右区域）    │
//      └────────┴──────────────────────────────────────────────────┘
//
//  T3：四区域由 RegionHost 包住，接 IdleWatcher（活动空闲折叠 / 点击唤醒）。
//  T4：本窗口持有 LocalClient 与 ViewState，收到消息就刷新顶栏。
//      main.cpp 只负责把终端桥（[recv] 打印 + stdin 命令）接到 client() 上，
//      这样 --stdio 与 GUI 两种模式的 IPC 行为完全一致。
// ============================================================================
#pragma once

#include <QMainWindow>
#include <QPixmap>
#include <QString>
#include <QVector>

#include "core/view_state.h"
#include "ui/top_bar.h"

class QJsonObject;
class QPushButton;
class QStackedWidget;
class QVariantAnimation;
class ChatPanel;
class SchedulePanel;
class BottomBar;
class LocalClient;
class MainPage;
class ModePanel;
class OnboardCtl;
class RegionHost;
class SysPage;
class ModelPage;
class SettingsPage;
namespace core {
class ConfigStore;
class IdleWatcher;
}

class MainWindow : public QMainWindow {
    Q_OBJECT

public:
    explicit MainWindow(QWidget* parent = nullptr);

    /// 切到某页；key 见 pageEntries()。返回 false 表示 key 不认识（不改变当前页）。
    bool showPage(const QString& key);

    /// 当前页 key。
    QString currentPage() const;
    /// 按配置应用界面：gui.wake.*（四区域/统一休眠）+ gui.debug（[D] 指示）。
    void applyConfig(const core::ConfigStore& gui);

    /// 重新读一遍日程并灌进右区域的日程区（S5）。
    /// 触发时机：启动（applyConfig 末尾）、设置页保存后、每 60 秒一次
    /// （60 秒是为了让窗口往前滑：新条目进来、已过的滑出去，跨零点时"今天/明天"也会翻页）。
    void reloadSchedule();

    /// 开始连接 Agent（非阻塞，断线由 LocalClient 自己重连）。
    void startIpc(const QString& path);

    /// 验收辅助：走**真实控件**（输入框 + 发送按钮）发一条消息，而不是直接调协议。
    void demoSend(const QString& text);

    /// config.yaml 路径（切换输入源时要写回它的 gui: 段）。空 = 不持久化。
    void setConfigPath(const QString& path);
    /// 处理输入源变化：只更新"政策"与提示行、并把选择写回 config.yaml。
    /// ⚠ S10 起**不**在这里弹软键盘 —— 弹的时机只有一个：输入框拿到焦点
    /// （见 onChatInputFocused）；切到"命令行"时会把已弹出的键盘收掉。
    void applyInputType(const QString& type);

    /// 输入框焦点变化 → 按 OnboardCtl::shouldShow() 弹/收软键盘（S10）
    void onChatInputFocused(bool focused);

    /// 验收辅助：触发音乐条某个占位块的说明（歌词/歌手/专辑/进度）。
    void demoPlaceholderNote(const QString& what);

    /// 验收辅助：点一下主区右下角的"下一张"（走真实按钮）
    void demoNextWallpaper();

    /// 验收辅助：点一下视频区的「下一集」
    void demoNextBilibili();
    /// 验收辅助：触发视频区某个占位项的说明（上一集/全屏/倍速）
    void demoVideoNote(const QString& what);
    /// 验收辅助：切换内嵌控制条的播放/暂停
    void demoPlayPause();
    /// 验收辅助：切换全屏
    void demoFullscreen();
    /// 设置视频源（本地文件路径或 URL）；空字符串 = 回到"视频源未接入"占位
    void setVideoSource(const QString& path);

    ChatPanel* chatPanel() const;
    ModePanel* modePanel() const;
    /// 日程区（S8 取证用：--dump-schedule 打印它真实渲染出来的行）
    SchedulePanel* schedulePanel() const;
    BottomBar* bottomBar() const;
    SysPage* sysPage() const { return sysPage_; }
    ModelPage* modelPage() const { return modelPage_; }
    SettingsPage* settingsPage() const { return settingsPage_; }
    /// 仓库根（配置同步与服务脚本都相对它定位）
    void setRepoRoot(const QString& root);

    LocalClient* client() const { return client_; }
    core::ViewState& viewState() { return view_; }
    core::IdleWatcher* idleWatcher() const { return watcher_; }

    /// 四区域折叠状态的可读文本（验收日志 / T13 debug 面板用）。
    QString wakeStateText() const;

protected:
    /// 首次显示时才开始空闲计时：应用启动（GL/配置/IPC 连接）耗时不该算进"用户空闲"。
    void showEvent(QShowEvent* event) override;
    /// 整个窗口的底：画壁纸（等比填满 + 居中裁切）；各面板半透明叠在上面
    void paintEvent(QPaintEvent* event) override;

private:
    QWidget* buildTopBar();
    QWidget* buildNavBar();
    QVector<RegionHost*> regions() const;

    void onMessage(const QString& topic, const QJsonObject& data);
    /// 把「Agent 链路 + 串流主机」合成一个三态结论刷到顶栏；状态变化时写日志。
    void refreshLinkState();
    /// 换壁纸：加载 → 交叉淡入 200ms；读不到就记错并在主区给灰字提示
    void setWallpaperFromPath(const QString& path, int index);
    /// 视频全屏：隐藏/恢复四区域面板（画面铺满整个屏幕）
    void setVideoFullscreen(bool on);

    /// 弹/收软键盘（失败时往对话里说明一次）。S10：弹的调用点只有焦点变化那一处。
    void showOnboard(const QString& why);
    void hideOnboard(const QString& why);

    QStackedWidget* stack_ = nullptr;
    QVector<QPushButton*> navButtons_;
    QVector<QString> navKeys_;

    TopBar* topBar_ = nullptr;
    LocalClient* client_ = nullptr;
    core::ViewState view_;
    core::IdleWatcher* watcher_ = nullptr;
    RegionHost* topHost_ = nullptr;
    RegionHost* navHost_ = nullptr;
    MainPage* mainPage_ = nullptr;
    bool wakeArmed_ = false;
    bool everConnected_ = false;
    bool agentUp_ = false;
    bool debug_ = false;
    OnboardCtl* onboard_ = nullptr;
    QString configPath_;
    bool onboardAuto_ = true;
    /// 当前输入源（S10：焦点策略要知道它；由 applyInputType/applyConfig 维护）
    QString inputSource_ = QStringLiteral("keyboard");
    QPixmap wallpaper_;
    QPixmap prevWallpaper_;
    QString wallpaperPath_;
    qreal wallpaperFade_ = 1.0;
    QVariantAnimation* wallpaperAnim_ = nullptr;
    core::IdleWatcher* overlayWatcher_ = nullptr;   ///< 内嵌控制条自己的空闲计时（不与四区域共享）
    bool overlayAutoHide_ = true;
    bool videoFullscreen_ = false;
    SysPage* sysPage_ = nullptr;
    ModelPage* modelPage_ = nullptr;
    SettingsPage* settingsPage_ = nullptr;
    QString repoRoot_;
    QTimer* monitorTimer_ = nullptr;
    QTimer* scheduleTimer_ = nullptr;    ///< 日程区的定时刷新（S5，60 秒）
    TopBar::LinkState lastLinkState_ = TopBar::LinkState::Disconnected;
};
