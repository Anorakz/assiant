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
class RegionHost;
class ModePanel;
class ChatPanel;
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

    /// 右区域的两块（T5）：上=模式切换按钮区，下=对话区
    ModePanel* modePanel() const { return modePanel_; }
    ChatPanel* chatPanel() const { return chatPanel_; }

    /// 下区域（T7）：非游戏=音乐条，游戏=B站封面（互斥）
    BottomBar* bottomBar() const { return bottomBar_; }

    /// 主区（T8/T9）：非游戏 = 完全留给壁纸（右下角一个"下一张"）；游戏 = T9 的视频区
    QPushButton* nextWallpaperButton() const { return nextWallpaper_; }
    QLabel* mainHintLabel() const { return mainHint_; }
    VideoPanel* videoPanel() const { return videoPanel_; }
    void setMainHint(const QString& text, bool warn = false);
    void setGameMode(bool game);

signals:
    /// 用户点了主区右下角的"下一张"（T8：发给 Agent 的 next_wallpaper）
    void nextWallpaperRequested();

private:
    RegionHost* bottomRegion_ = nullptr;
    RegionHost* rightRegion_ = nullptr;
    ModePanel* modePanel_ = nullptr;
    ChatPanel* chatPanel_ = nullptr;
    BottomBar* bottomBar_ = nullptr;
    QPushButton* nextWallpaper_ = nullptr;
    QLabel* mainHint_ = nullptr;
    QStackedWidget* mainStack_ = nullptr;
    VideoPanel* videoPanel_ = nullptr;
};

/// 造一个带标题的区域容器（占位用）。objectName 供后续样式表定位。
///
/// @param bare true = 不画背景与边框（主区用：完全留给全局壁纸，见方案 §3.2）
QWidget* makeArea(const QString& title, const QString& hint, QWidget* parent = nullptr,
                  bool bare = false);
