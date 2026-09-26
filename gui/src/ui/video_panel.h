// ============================================================================
//  gui/src/ui/video_panel.h — 主区·视频区（游戏模式，方案 §3.2）
//
//  板端实测（temp/video_probe.cpp）：
//    · QMediaPlayer(GStreamer 后端) 能解 H.264：duration=10030ms、position 递增 ✓
//    · **QVideoWidget 的画面 grab() 抓不到**（原生视频表面）→ 验收截图必须 scrot
//    · **不要把控件盖在视频窗口"里面"**：实测视频的原生窗口会盖住同区域的兄弟控件
//      （连占位态也一样）。所以控制条放在画面区**底部预留的一条**里，位于视频窗口
//      几何之外 —— 视觉上仍在画面区内、浮在画面下沿，但不会被视频盖掉。
//
//  版面（T11-7 起）
//  ---------------------------------------------------------------------------
//      ┌───────────────────────────────────────────────┐
//      │ 地址栏 + 队列来源（BilibiliPreview 的第一行）    │
//      ├───────────────────────────────────────────────┤
//      │ 画面（QVideoWidget）                            │
//      │   [|◀] [▶/||] [▶|] [1.0x] [ ]  ← 浮在画面下沿    │
//      ├───────────────────────────────────────────────┤
//      │ 预览栏：缩略图 N 格（Agent 的队列 = 3N）          │
//      └───────────────────────────────────────────────┘
//
//  控制条（符号化、居中、不实体化：只有按钮本身有半透明底，没有整条背景）：
//    · 播放/暂停：**真功能**（本地文件/本地 FIFO 流都用 QMediaPlayer 控制）
//    · 上一集/下一集：**真功能**（T11-7 起发 prev_bilibili / next_bilibili）
//    · 全屏：**真功能**（覆盖整个屏幕：隐藏四区域面板，画面铺满）
//    · 倍速：仍是占位（你定的：T11 搁置），点了只给说明
//    · 自带"活动/锁定"：active = 空闲后自动隐藏；idle_ms 用 video_overlay.idle_ms，
//      **不与四个区域共享**（见 gui.video_overlay.idle_ms）
//
//  ⚠ **清晰度一个字段都没有**（你定的：清晰度只走 LLM 聊天气泡）。
// ============================================================================
#pragma once

#include <QJsonObject>
#include <QString>
#include <QWidget>

class QLabel;
class QMediaPlayer;
class QPushButton;
class QStackedWidget;
class QToolButton;
class QVideoWidget;
class BilibiliPreview;
class CoverLoader;

class VideoPanel : public QWidget {
    Q_OBJECT

public:
    explicit VideoPanel(QWidget* parent = nullptr);
    ~VideoPanel() override;

    /// 设置视频源（本地文件路径或 URL）。空字符串 = 清空回占位态。
    void setSource(const QString& fileOrUrl);
    QString source() const { return source_; }

    /// 协议 topic `bilibili` 的 data：填预览栏 + 地址栏；**有 `stream` 就换源**。
    /// @note `stream` 空 = Agent 那边已经没在缓冲（换条/清空/放完）→ 回到占位态。
    void setBilibili(const QJsonObject& data);
    BilibiliPreview* preview() const { return preview_; }
    void setCoverLoader(CoverLoader* loader);

    void play();
    void pause();
    /// 播放/暂停切换（内嵌控制条那颗按钮走这里）
    void togglePlayPause();
    bool isPlaying() const;

    /// 全屏（覆盖整个屏幕）。面板只负责按钮文案与信号，真正的铺满由 MainWindow 做。
    void setFullscreen(bool on);
    bool isFullscreen() const { return fullscreenOn_; }

    /// 内嵌控制条的显隐（活动/锁定里的"活动"部分）
    void setOverlayVisible(bool visible);
    bool overlayVisible() const;

    QMediaPlayer* player() const { return player_; }
    QVideoWidget* videoWidget() const { return video_; }
    QStackedWidget* stage() const { return stage_; }
    QWidget* overlayBar() const { return overlay_; }
    QPushButton* previousButton() const { return previous_; }
    QPushButton* playButton() const { return play_; }
    QPushButton* nextButton() const { return next_; }
    QToolButton* speedButton() const { return speed_; }
    QPushButton* fullscreenButton() const { return fullscreen_; }
    QLabel* placeholderLabel() const { return placeholder_; }
    /// 最近一次占位说明（点击占位项后会有内容）
    QString noteText() const { return noteText_; }

    /// 触发某个占位项的说明。what ∈ {倍速}
    void triggerPlaceholder(const QString& what);

    /// 立刻回报一次 `video_state`（时长/位置/在不在放）。
    /// @note 内嵌那个 2 秒定时器调的就是它；单测/验收也能直接调，不用等定时器。
    void reportVideoState();
    /// 「这一条放完了」（等于播放器报 EndOfMedia）。给单测/验收一个不依赖媒体后端
    /// 的入口 —— 真机上这条路是 QMediaPlayer::mediaStatusChanged 进来的。
    void notifyEndOfMedia();

signals:
    /// 点了「下一集」→ 发协议 next_bilibili
    void nextBilibiliRequested();
    /// 点了「上一集」→ 发协议 prev_bilibili（T11-7 起转正）
    void prevBilibiliRequested();
    /// 点了预览栏第 index 格 → 发协议 bilibili_pick{index}
    void pickBilibiliRequested(int index);
    /// 预览栏能放下几格 → 发协议 bilibili_viewport{visible}
    void viewportChanged(int visible);
    /// 该回报 `video_state` 了（MainWindow 组装载荷并发送）
    void videoStateReported(qint64 positionMs, qint64 durationMs, bool playing, bool eof);
    /// 点了被占位的控制项（倍速）
    void placeholderClicked(const QString& what);
    /// 全屏开/关（MainWindow 据此隐藏或恢复四区域面板）
    void fullscreenToggled(bool on);
    /// 播放/暂停状态变化（做日志与验收用）
    void playingChanged(bool playing);

protected:
    /// 画面区自己排版：视频占上部，控制条占底部预留的一条
    void resizeEvent(QResizeEvent* event) override;

private:
    void setStageVideo(bool video);
    void relayoutStage();

    QStackedWidget* stage_ = nullptr;    ///< 0=占位 / 1=画面（视频 + 内嵌控制条 + 预览栏）
    QLabel* placeholder_ = nullptr;
    QWidget* videoPage_ = nullptr;       ///< 画面页容器（地址栏/画面/预览栏）
    QWidget* screen_ = nullptr;          ///< 画面那块（视频 + 控制条两个兄弟）
    QVideoWidget* video_ = nullptr;
    QWidget* overlay_ = nullptr;         ///< 内嵌控制条（浮在画面下沿）
    BilibiliPreview* preview_ = nullptr; ///< 预览栏 + 地址栏（T11-7）
    QMediaPlayer* player_ = nullptr;
    QPushButton* previous_ = nullptr;
    QPushButton* play_ = nullptr;
    QPushButton* next_ = nullptr;
    QToolButton* speed_ = nullptr;
    QPushButton* fullscreen_ = nullptr;
    QLabel* note_ = nullptr;             ///< 保留位（说明实际挂右侧提示行），恒为 nullptr

    QString source_;
    QString noteText_;
    bool fullscreenOn_ = false;
    bool eofSent_ = false;               ///< 这一条已经报过"放完了"（别重复自动下一集）
};
