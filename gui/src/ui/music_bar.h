// ============================================================================
//  gui/src/ui/music_bar.h — 下区域·音乐条（非游戏模式，方案 §3.3）
//
//  下区域高度 192px（96×2）。内部分区**不是三等分**：
//
//      ┌───────────────────────────────────┬──────────────────┐
//      │ 标题 / 歌手 · 专辑                  │                  │
//      │ 进度 / 总时长                       │      歌词         │
//      │ [上一首] [ ▶ ] [下一首]             │                  │
//      └───────────────────────────────────┴──────────────────┘
//        行1、行2 按内容高度（保持原有字号），行3 吃掉剩余高度，
//        中间那颗播放/暂停按钮**略大**（56×56）。
//
//  数据来源只有协议 `music{title,playing}`；**控制类（上一首/暂停/下一首）
//  协议里没有对应命令**，所以按方案 §7 做成"可点但只是给说明"的占位。
// ============================================================================
#pragma once

#include <QString>
#include <QWidget>

class QLabel;
class QProgressBar;
class QPushButton;

class MusicBar : public QWidget {
    Q_OBJECT

public:
    explicit MusicBar(QWidget* parent = nullptr);

    /// 协议 `music` 到达时调用。title 为空 = 没有曲目信息。
    void setMusic(const QString& title, bool playing);

    bool hasMusic() const { return hasMusic_; }
    QString title() const;
    /// 当前显示的占位说明（点击占位块后会有内容；供单测/验收核对）
    QString noteText() const;

    // 供单测/验收演示直接拿控件
    QLabel* lyricsLabel() const { return lyrics_; }
    QLabel* titleLabel() const { return title_; }
    QLabel* artistLabel() const { return artist_; }
    QLabel* albumLabel() const { return album_; }
    QLabel* timeLabel() const { return time_; }
    QProgressBar* progressBar() const { return progress_; }
    QPushButton* previousButton() const { return prev_; }
    QPushButton* nextButton() const { return next_; }
    /// 中间那颗大的播放/暂停按钮（文本显示 ▶ / ⏸）
    QPushButton* playButton() const { return play_; }

    /// 触发某个占位块的说明。what ∈ {歌词, 歌手, 专辑, 进度, 控制}。
    /// 点击与"验收演示"都走这一个入口（单测也能直接调，不用模拟鼠标）。
    void triggerPlaceholder(const QString& what);

signals:
    /// 点了"未接入"的占位区；what 说明是哪一块
    void placeholderClicked(const QString& what);

private:
    void showNote(const QString& what, const QString& text);

    QLabel* lyrics_ = nullptr;     ///< 右列：歌词（也是占位说明的显示位）
    QLabel* title_ = nullptr;
    QLabel* artist_ = nullptr;
    QLabel* album_ = nullptr;
    QProgressBar* progress_ = nullptr;
    QLabel* time_ = nullptr;
    QPushButton* prev_ = nullptr;
    QPushButton* play_ = nullptr;
    QPushButton* next_ = nullptr;

    bool hasMusic_ = false;
    bool playing_ = false;
    QString noteText_;             ///< 当前占位说明（空 = 没有点过占位块）
};
