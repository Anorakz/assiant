// ============================================================================
//  gui/src/ui/music_bar.h — 下区域·音乐条（非游戏模式，方案 §3.3）
//
//  下区域高度 192px（96×2）。内部分区**不是三等分**：
//
//      ┌───────────────────────────────────┬──────────────────┐
//      │ 标题 / 歌手 · 专辑                  │  当前那句歌词     │
//      │ 进度 / 总时长                       │  下一句（灰）     │
//      │ [上一首] [ ▶ ] [下一首]             │                  │
//      └───────────────────────────────────┴──────────────────┘
//        行1、行2 按内容高度（保持原有字号），行3 吃掉剩余高度，
//        中间那颗播放/暂停按钮**略大**（56×56）。
//
//  T15-16 起这里**全部点亮**了（不再是占位块）：
//    · 数据：`setMusic(整份 music 载荷)` —— 曲目/歌手/专辑/播放状态/进度时长；
//    · 歌词：`setLyricsProvider()` + `setPosition()` —— 界面只认 D5 那个接口，
//      "显示哪一句"是 `core::TimedLyrics` 算的（见 core/lyrics.h 的分层说明）；
//    · 控制：三个按钮**发信号**，由主窗口转成现成的三条命令
//      （`music_prev` / `music_play_pause` / `music_next`）—— 控件自己不碰 IPC。
//
//  ⚠ 进度条**不做时间外推**：位置由调用方算好再喂（`setProgress`）。这样控件是纯
//    渲染层、单测里不用等时钟；外推与"暂停就不动"由主窗口那个 1 s 定时器负责。
// ============================================================================
#pragma once

#include <QJsonObject>
#include <QString>
#include <QWidget>

namespace core {
class LyricsProvider;
}

class QLabel;
class QProgressBar;
class QPushButton;

class MusicBar : public QWidget {
    Q_OBJECT

public:
    explicit MusicBar(QWidget* parent = nullptr);

    /// 协议 `music` 到达时调用（整份 `data`）。字段是**部分更新**：没带的保持原值。
    void setMusic(const QJsonObject& music);

    /// 进度（秒）。调用方自己外推后喂进来；`durationS <= 0` = 没有进度可显示。
    void setProgress(double positionS, double durationS);

    /// 歌词来源（D5 的 `core::LyricsProvider`）。不设 = 右列显示"歌词未接入"。
    void setLyricsProvider(core::LyricsProvider* provider);

    /// 当前播放位置（秒）—— 用它去问 provider"该显示哪句 / 下一句"。
    void setPosition(double positionS);

    /// 与 Agent 的链路状态：断连时三个控制按钮**禁用**（点了也发不出去）。
    void setConnected(bool connected);

    bool hasMusic() const { return hasMusic_; }
    bool playing() const { return playing_; }
    bool connected() const { return connected_; }
    QString title() const;

    // 供单测/验收演示直接拿控件
    QLabel* lyricsLabel() const { return lyrics_; }
    QLabel* nextLyricsLabel() const { return nextLyrics_; }
    QLabel* titleLabel() const { return title_; }
    QLabel* artistLabel() const { return artist_; }
    QLabel* albumLabel() const { return album_; }
    QLabel* timeLabel() const { return time_; }
    QProgressBar* progressBar() const { return progress_; }
    QPushButton* previousButton() const { return prev_; }
    QPushButton* nextButton() const { return next_; }
    /// 中间那颗大的播放/暂停按钮（图标显示 ▶ / ⏸）
    QPushButton* playButton() const { return play_; }

signals:
    /// 上一首 / 播放暂停 / 下一首（主窗口接上现成的三条命令）
    void prevClicked();
    void playPauseClicked();
    void nextClicked();

private:
    void refreshLyrics();

    QLabel* lyrics_ = nullptr;      ///< 右列第一行：当前那句
    QLabel* nextLyrics_ = nullptr;  ///< 右列第二行：下一句（灰）
    QLabel* title_ = nullptr;
    QLabel* artist_ = nullptr;
    QLabel* album_ = nullptr;
    QProgressBar* progress_ = nullptr;
    QLabel* time_ = nullptr;
    QPushButton* prev_ = nullptr;
    QPushButton* play_ = nullptr;
    QPushButton* next_ = nullptr;

    core::LyricsProvider* provider_ = nullptr;
    double position_ = 0.0;         ///< 最近一次喂进来的播放位置（秒）
    double duration_ = 0.0;         ///< 最近一次知道的时长（秒）
    bool hasMusic_ = false;
    bool playing_ = false;
    bool connected_ = false;        ///< 默认断连（还没收到任何链路消息）
};
