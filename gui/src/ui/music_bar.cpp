// ============================================================================
//  gui/src/ui/music_bar.cpp — 音乐条实现（192px：左列三行 + 右列歌词）
//
//  占位块（歌词/歌手/专辑/进度/控制）都能点，点了就在歌词位置给出说明 ——
//  比 tooltip 好的一点是它能被 grab() 抓进验收截图。
// ============================================================================
#include "ui/music_bar.h"

#include "ui/icons.h"

#include <QHBoxLayout>
#include <QLabel>
#include <QMouseEvent>
#include <QColor>
#include <QIcon>
#include <QProgressBar>
#include <QPushButton>
#include <QSizePolicy>
#include <QVBoxLayout>

#include <functional>

namespace {

/// 点得动的标签：不引入 Q_OBJECT，直接挂一个回调
class ClickableLabel : public QLabel {
public:
    explicit ClickableLabel(const QString& text, QWidget* parent = nullptr)
        : QLabel(text, parent)
    {
        setCursor(Qt::PointingHandCursor);
    }
    std::function<void()> onClick;

protected:
    void mousePressEvent(QMouseEvent* event) override
    {
        if (onClick) {
            onClick();
        }
        QLabel::mousePressEvent(event);
    }
};

/// "未接入"小角标（方案 §7：占位必须可见地"不能当真"）
QLabel* makeTag(const QString& text, QWidget* parent)
{
    auto* tag = new QLabel(text, parent);
    tag->setObjectName(QStringLiteral("PlaceholderTag"));
    return tag;
}

/// 控制类按钮（上一首/下一首/播放暂停）：点了只给说明，不发协议
///
/// 符号选择是按**板端字体实测**定的（fc-list 查码位）：
///   U+25B6 ▶ / U+25C0 ◀ 有；U+23F8 ⏸、U+23EE ⏮、U+23ED ⏭ **全都没有**（会画成豆腐块）。
///   所以用 `|◀` / `||` / `▶` / `▶|` —— 只用被覆盖的码位。
QPushButton* makeCtlButton(const QString& text, bool big, QWidget* parent)
{
    auto* button = new QPushButton(text, parent);
    button->setObjectName(big ? QStringLiteral("MusicCtlMain") : QStringLiteral("MusicCtl"));
    const int side = big ? 56 : 44;        // 验收：播放/暂停略大一点
    button->setFixedSize(side, side);      // 方形标准按钮
    button->setCursor(Qt::PointingHandCursor);
    return button;
}

} // namespace

MusicBar::MusicBar(QWidget* parent)
    : QWidget(parent)
{
    auto* root = new QHBoxLayout(this);
    root->setContentsMargins(0, 0, 0, 0);
    root->setSpacing(24);

    // ==================================================================== 左列
    auto* left = new QWidget(this);
    auto* leftBox = new QVBoxLayout(left);
    leftBox->setContentsMargins(0, 0, 0, 0);
    leftBox->setSpacing(8);

    // ---- 行 1：标题 / 歌手 · 专辑（按内容高度，字号保持原样）----
    auto* row1 = new QWidget(left);
    auto* row1Box = new QVBoxLayout(row1);
    row1Box->setContentsMargins(0, 0, 0, 0);
    row1Box->setSpacing(2);

    title_ = new QLabel(row1);
    title_->setObjectName(QStringLiteral("MusicTitle"));
    row1Box->addWidget(title_);

    auto* subRow = new QWidget(row1);
    auto* subBox = new QHBoxLayout(subRow);
    subBox->setContentsMargins(0, 0, 0, 0);
    subBox->setSpacing(6);

    auto* artistLabel = new ClickableLabel(QStringLiteral("歌手 —"), subRow);
    artistLabel->setObjectName(QStringLiteral("MusicPlaceholder"));
    artistLabel->onClick = [this]() { triggerPlaceholder(QStringLiteral("歌手")); };
    artist_ = artistLabel;

    auto* albumLabel = new ClickableLabel(QStringLiteral("专辑 —"), subRow);
    albumLabel->setObjectName(QStringLiteral("MusicPlaceholder"));
    albumLabel->onClick = [this]() { triggerPlaceholder(QStringLiteral("专辑")); };
    album_ = albumLabel;

    subBox->addWidget(artist_);
    subBox->addWidget(album_);
    subBox->addWidget(makeTag(QStringLiteral("未接入"), subRow));
    subBox->addStretch(1);
    row1Box->addWidget(subRow);
    leftBox->addWidget(row1);

    // ---- 行 2：进度 / 总时长（占位）----
    auto* row2 = new QWidget(left);
    auto* row2Box = new QHBoxLayout(row2);
    row2Box->setContentsMargins(0, 0, 0, 0);
    row2Box->setSpacing(10);

    auto* timeClickable = new ClickableLabel(QStringLiteral("—:— / —:—"), row2);
    timeClickable->setObjectName(QStringLiteral("MusicPlaceholder"));
    timeClickable->onClick = [this]() { triggerPlaceholder(QStringLiteral("进度")); };
    time_ = timeClickable;

    progress_ = new QProgressBar(row2);
    progress_->setObjectName(QStringLiteral("MusicProgress"));
    progress_->setRange(0, 100);
    progress_->setValue(0);
    progress_->setTextVisible(false);
    progress_->setFixedHeight(10);
    progress_->setFixedWidth(150);          // 验收：缩短一半（原来铺满整行 ~290px）
    progress_->setToolTip(QStringLiteral("播放进度未接入"));

    row2Box->addWidget(progress_);
    row2Box->addWidget(time_);
    row2Box->addWidget(makeTag(QStringLiteral("未接入"), row2));
    row2Box->addStretch(1);                 // 左对齐，剩余空间留在右边
    leftBox->addWidget(row2);

    // ---- 行 3：上一首 / 播放暂停 / 下一首（协议无控制命令 → 占位）----
    auto* row3 = new QWidget(left);
    auto* row3Box = new QHBoxLayout(row3);
    row3Box->setContentsMargins(0, 6, 0, 0);
    row3Box->setSpacing(12);

    prev_ = makeCtlButton(QString(), false, row3);
    prev_->setIcon(ui::tintedIcon(QStringLiteral("prev"), QColor(0xE6, 0xE6, 0xE6)));
    prev_->setIconSize(QSize(20, 20));
    play_ = makeCtlButton(QString(), true, row3);
    play_->setIcon(ui::tintedIcon(QStringLiteral("play"), QColor(0xE6, 0xE6, 0xE6)));
    play_->setIconSize(QSize(24, 24));
    next_ = makeCtlButton(QString(), false, row3);
    next_->setIcon(ui::tintedIcon(QStringLiteral("next"), QColor(0xE6, 0xE6, 0xE6)));
    next_->setIconSize(QSize(20, 20));
    const auto asPlaceholder = [this](QPushButton* button) {
        connect(button, &QPushButton::clicked, this,
                [this]() { triggerPlaceholder(QStringLiteral("控制")); });
    };
    asPlaceholder(prev_);
    asPlaceholder(play_);
    asPlaceholder(next_);

    row3Box->addWidget(prev_);
    row3Box->addWidget(play_);
    row3Box->addWidget(next_);
    row3Box->addWidget(makeTag(QStringLiteral("未接入"), row3));
    row3Box->addStretch(1);
    leftBox->addWidget(row3, 1);           // 行 3 吃掉剩余高度（不是三等分）
    root->addWidget(left, 3);

    // ==================================================================== 右列
    auto* right = new QWidget(this);
    auto* rightBox = new QVBoxLayout(right);
    rightBox->setContentsMargins(0, 0, 0, 0);
    rightBox->setSpacing(4);

    auto* lyricsClickable = new ClickableLabel(QStringLiteral("歌词未接入"), right);
    lyricsClickable->setObjectName(QStringLiteral("MusicPlaceholder"));
    lyricsClickable->onClick = [this]() { triggerPlaceholder(QStringLiteral("歌词")); };
    // ⚠ 不许让提示文本驱动布局宽度：否则一句长的占位说明会把整个窗口撑宽
    //   （T7 出图时实测 1280 → 1290）。让它在拉伸区里自己收缩。
    lyricsClickable->setSizePolicy(QSizePolicy::Ignored, QSizePolicy::Preferred);
    lyricsClickable->setWordWrap(true);
    lyricsClickable->setAlignment(Qt::AlignTop | Qt::AlignLeft);
    lyrics_ = lyricsClickable;

    auto* lyricsRow = new QWidget(right);
    auto* lyricsRowBox = new QHBoxLayout(lyricsRow);
    lyricsRowBox->setContentsMargins(0, 0, 0, 0);
    lyricsRowBox->setSpacing(6);
    lyricsRowBox->addWidget(lyrics_, 1);
    lyricsRowBox->addWidget(makeTag(QStringLiteral("未接入"), lyricsRow), 0, Qt::AlignTop);
    rightBox->addWidget(lyricsRow);
    rightBox->addStretch(1);
    root->addWidget(right, 2);

    setMusic(QString(), false);
}

QString MusicBar::title() const
{
    return title_ != nullptr ? title_->text() : QString();
}

QString MusicBar::noteText() const
{
    return noteText_;
}

void MusicBar::triggerPlaceholder(const QString& what)
{
    if (what == QStringLiteral("歌词")) {
        showNote(what, QStringLiteral("歌词来源未接入（D5 已预留 LyricsProvider 接口）"));
    } else if (what == QStringLiteral("歌手")) {
        showNote(what, QStringLiteral("歌手信息未接入（协议 music 只有 title/playing）"));
    } else if (what == QStringLiteral("专辑")) {
        showNote(what, QStringLiteral("专辑信息未接入（协议 music 只有 title/playing）"));
    } else if (what == QStringLiteral("进度")) {
        showNote(what, QStringLiteral("播放进度未接入（协议 music 无 position/duration）"));
    } else if (what == QStringLiteral("控制")) {
        showNote(what, QStringLiteral("音乐控制未接入（协议暂无 上一首/暂停/下一首 命令）"));
    }
}

void MusicBar::showNote(const QString& what, const QString& text)
{
    noteText_ = text;
    if (lyrics_ != nullptr) {
        lyrics_->setText(text);
        lyrics_->setStyleSheet(QStringLiteral("color:#F59E0B;"));
    }
    emit placeholderClicked(what);
}

void MusicBar::setMusic(const QString& title, bool playing)
{
    hasMusic_ = !title.isEmpty();
    playing_ = playing;

    // 收到新的 music 数据就把占位说明清掉，回到正常显示
    noteText_.clear();
    if (lyrics_ != nullptr) {
        lyrics_->setText(QStringLiteral("歌词未接入"));
        lyrics_->setStyleSheet(QString());
    }

    if (title_ != nullptr) {
        title_->setText(hasMusic_ ? title : QStringLiteral("未播放"));
    }
    if (play_ != nullptr) {
        // 中间大按钮兼作状态显示（可点，但点了只给"未接入"说明）
        // T14/验收：一律用**按按钮前景色染色**的单色图标（不再用 — / || / ▶ 这些文字符号）
        play_->setText(QString());
        if (!hasMusic_) {
            // 没曲目：暗色播放图标（以前是"—"，看着像"圆里一道横"，看不懂）
            play_->setIcon(ui::tintedIcon(QStringLiteral("play"), QColor(0x6F, 0x75, 0x7C)));
        } else if (playing) {
            play_->setIcon(ui::tintedIcon(QStringLiteral("pause"), QColor(0xE6, 0xE6, 0xE6)));
        } else {
            play_->setIcon(ui::tintedIcon(QStringLiteral("play"), QColor(0xE6, 0xE6, 0xE6)));
        }
    }
    if (progress_ != nullptr) {
        progress_->setValue(0);      // 协议没有进度，横条保持 0 且不可拖
        progress_->setEnabled(false);
    }
}
