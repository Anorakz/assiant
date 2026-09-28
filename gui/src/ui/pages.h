// ============================================================================
//  gui/src/ui/pages.h — 页面注册表与页面控件
//
//  四个页面（方案 §2 导航）：主页面 / 模型测试 / 系统 / 设置。
//  框架（上区域通栏 + 左区域导航）在 MainWindow，页面只占右侧剩余区域。
//
//  · MainPage   主页面：中部（主区 + 下区域·不通栏）+ 右区域（模式按钮区 + 对话区）
//  · 其余三页   T1 阶段是占位页，后续任务逐个替换（T11/T12 模型测试、T10 系统、T13 设置）
//
//  T1 的验收方式就是逐页截图，所以每个占位页都写清"将由哪个任务填充"。
// ============================================================================
#pragma once

#include <QString>
#include <QVector>
#include <QWidget>

class QLabel;
class QPushButton;
class QVBoxLayout;
class QSpacerItem;
class RegionHost;
class ModePanel;
class ChatPanel;
class SchedulePanel;
class BottomBar;
class VideoPanel;
class QStackedWidget;

/// 导航项：key 用于 --page 参数与 showPage()，label 是导航栏上的中文短标签。
struct PageEntry {
    QString key;
    QString label;
};

/// 导航顺序（左区域从上到下）；"设置" 由 MainWindow 用分隔线压到底部。
const QVector<PageEntry>& pageEntries();

/// 创建一个页面控件；key 不认识时返回 nullptr（调用方负责报错）。
QWidget* createPage(const QString& key, QWidget* parent = nullptr);

/// 带标题与说明的占位页（T1 用，后续任务替换成真页面）。
class PlaceholderPage : public QWidget {
public:
    PlaceholderPage(const QString& title, const QString& hint, QWidget* parent = nullptr);
};

/// 主页面：中部（主区 + 下区域）+ 右区域（模式切换按钮区 + 对话区）。
///
/// T1 只搭骨架并标出各区域；内容由后续任务填充：
///   主区      → T9 游戏模式真视频 / T8 非游戏模式露出全局壁纸
///   下区域    → T7 音乐条 / T9 游戏模式 B站封面缩略图（互斥）
///   模式按钮区 → T5
///   对话区    → T5（含底部输入行）
class MainPage : public QWidget {
    Q_OBJECT

public:
    explicit MainPage(QWidget* parent = nullptr);

    /// 下区域 / 右区域的折叠宿主（T3 唤醒机制挂载点，见方案 §4）。
    RegionHost* bottomRegion() const { return bottomRegion_; }
    RegionHost* rightRegion() const { return rightRegion_; }

    /// 右区域的三块：上=模式切换按钮区，中=对话区，下=日程区（S5 加）
    ModePanel* modePanel() const { return modePanel_; }
    ChatPanel* chatPanel() const { return chatPanel_; }
    SchedulePanel* schedulePanel() const { return schedulePanel_; }

    /// 对话区 / 日程区的容器（几何断言用；两者 objectName 都是 AreaFrame —— 卡片样式）
    QWidget* chatFrame() const { return chatFrame_; }
    QWidget* scheduleFrame() const { return scheduleFrame_; }

    /// 下区域（T7）：非游戏=音乐条，游戏=B站封面（互斥）
    BottomBar* bottomBar() const { return bottomBar_; }

    /// 主区（T8/T9）：非游戏 = 完全留给壁纸；游戏 = T9 的视频区
    /// ⚠ T7-3：主区那个「下一张」按钮**已删除** —— 换壁纸只走对话（见 docs/gui.md）
    QLabel* mainHintLabel() const { return mainHint_; }
    VideoPanel* videoPanel() const { return videoPanel_; }
    /// 主区那行提示。**空字符串 = 藏起来**（T3：壁纸来了以后占位文字要让位）；
    /// warn=true 用橙色（壁纸读不到这类"要看得出来"的情况）
    void setMainHint(const QString& text, bool warn = false);
    void setGameMode(bool game);

    /// T15-1 1b：虚拟键盘是**浮在窗口上**的独立窗口（Qt 的 DesktopInputPanel），
    /// 它盖住的正好是屏幕下半截 —— 而对话输入行就在那儿。这里把对话区的高度上限
    /// 压到键盘上沿以内（输入行在对话区底部，所以它就被顶到键盘上方了）。
    /// `px` = 本页被键盘盖住的高度（本页坐标）；px<=0 恢复原样。
    void setKeyboardInset(int px);

private:
    RegionHost* bottomRegion_ = nullptr;
    RegionHost* rightRegion_ = nullptr;
    ModePanel* modePanel_ = nullptr;
    ChatPanel* chatPanel_ = nullptr;
    SchedulePanel* schedulePanel_ = nullptr;
    QWidget* chatFrame_ = nullptr;
    QWidget* scheduleFrame_ = nullptr;
    BottomBar* bottomBar_ = nullptr;
    QLabel* mainHint_ = nullptr;
    /// 当前是不是游戏模式（主区是视频区那页）。T3：给 setMainHint 判断可见性用
    bool isGameMode_ = false;
    QStackedWidget* mainStack_ = nullptr;
    VideoPanel* videoPanel_ = nullptr;
    /// 右区域（模式区 / 对话区 / 日程区）那个竖排布局（几何断言/以后调让位用）
    QVBoxLayout* rightBox_ = nullptr;
    /// 模式卡（打字时收起来给对话卡腾地方，见 setKeyboardInset）
    QWidget* modeFrame_ = nullptr;
    /// 打字让位时插在列尾的弹簧（吸掉多余空间，逼对话卡贴列顶）；收起键盘就删掉
    QSpacerItem* tailSpacer_ = nullptr;
    /// 当前已经让出去的高度（-1 = 还没设过），避免重复改上限触发重排
    int keyboardInset_ = -1;
};

/// 造一个带标题的区域容器（占位用）。objectName 供后续样式表定位。
///
/// @param bare true = 不画背景与边框（主区用：完全留给全局壁纸，见方案 §3.2）
QWidget* makeArea(const QString& title, const QString& hint, QWidget* parent = nullptr,
                  bool bare = false);
