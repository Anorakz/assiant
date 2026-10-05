// ============================================================================
//  gui/src/ui/music_bar.cpp — 音乐条实现（192px：左列三行 + 右列两句歌词）
//
//  T15-16：占位块全部点亮。控件只做三件事：**渲染**、**发信号**、**问 provider 要两句词**。
//  它不认识时钟（进度由调用方算好喂进来）、也不认识 IPC（命令由主窗口发）。
// ============================================================================
#include "ui/music_bar.h"
#include "ui/theme.h"    // T15-16 G-A-1b：视觉常量（唯一来源）

#include "core/lyrics.h"
#include "ui/icons.h"

#include <QColor>
#include <QHBoxLayout>
#include <QIcon>
#include <QJsonValue>
#include <QLabel>
#include <QProgressBar>
#include <QPushButton>
#include <QSizePolicy>
#include <QVBoxLayout>

namespace {

/// 颜色：正常前景 / 次要（灰）/ 提示（琥珀）/ 完全不可用（更暗）
const QColor kPrimary(0xE6, 0xE6, 0xE6);
const QColor kMuted(0x9A, 0xA1, 0xA9);
const QColor kHint(0xF5, 0x9E, 0x0B);
const QColor kOff(0x6F, 0x75, 0x7C);

/// 秒 -> `m:ss`（1:10）；不能显示时给 `—:—`。
QString formatClock(double seconds)
{
    if (!(seconds >= 0.0)) {                       // 负数与 NaN 都当"没有"
        return QStringLiteral("—:—");
    }
    const int total = static_cast<int>(seconds + 0.5);
    return QStringLiteral("%1:%2").arg(total / 60)
                                  .arg(total % 60, 2, 10, QLatin1Char('0'));
}

/// 控制类按钮（上一首/下一首/播放暂停）
///
/// 符号选择是按**板端字体实测**定的（fc-list 查码位）：
///   U+25B6 ▶ / U+25C0 ◀ 有；U+23F8 ⏸、U+23EE ⏮、U+23ED ⏭ **全都没有**（会画成豆腐块）。
///   所以图标一律走 `ui::tintedIcon()`（自绘 SVG，不依赖字体码位）。
QPushButton* makeCtlButton(bool big, QWidget* parent)
{
    auto* button = new QPushButton(parent);
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

    // ---- 行 1：标题 / 歌手 · 专辑 ----
    auto* row1 = new QWidget(left);
    auto* row1Box = new QVBoxLayout(row1);
    row1Box->setContentsMargins(0, 0, 0, 0);
    row1Box->setSpacing(2);

    title_ = new QLabel(QStringLiteral("未播放"), row1);
    title_->setObjectName(QStringLiteral("MusicTitle"));
    row1Box->addWidget(title_);

    auto* subRow = new QWidget(row1);
    auto* subBox = new QHBoxLayout(subRow);
    subBox->setContentsMargins(0, 0, 0, 0);
    subBox->setSpacing(6);

    artist_ = new QLabel(QStringLiteral("歌手 —"), subRow);
    artist_->setObjectName(QStringLiteral("MusicArtist"));
    album_ = new QLabel(QStringLiteral("专辑 —"), subRow);
    album_->setObjectName(QStringLiteral("MusicAlbum"));
    subBox->addWidget(artist_);
    subBox->addWidget(album_);
    subBox->addStretch(1);
    row1Box->addWidget(subRow);
    leftBox->addWidget(row1);

    // ---- 行 2：进度 / 总时长 ----
    auto* row2 = new QWidget(left);
    auto* row2Box = new QHBoxLayout(row2);
    row2Box->setContentsMargins(0, 0, 0, 0);
    row2Box->setSpacing(10);

    progress_ = new QProgressBar(row2);
    progress_->setObjectName(QStringLiteral("MusicProgress"));
    progress_->setRange(0, 1000);           // 千分比：1 s 的进度条也看得出动
    progress_->setValue(0);
    progress_->setTextVisible(false);
    progress_->setFixedHeight(10);
    progress_->setFixedWidth(150);          // 验收：缩短一半（原来铺满整行 ~290px）
    progress_->setToolTip(QStringLiteral("还没有进度（还没起播 / 不是本机在放）"));

    time_ = new QLabel(QStringLiteral("—:— / —:—"), row2);
    time_->setObjectName(QStringLiteral("MusicTime"));

    row2Box->addWidget(progress_);
    row2Box->addWidget(time_);
    row2Box->addStretch(1);                 // 左对齐，剩余空间留在右边
    leftBox->addWidget(row2);

    // ---- 行 3：上一首 / 播放暂停 / 下一首（发信号，由主窗口发命令）----
    auto* row3 = new QWidget(left);
    auto* row3Box = new QHBoxLayout(row3);
    row3Box->setContentsMargins(0, 6, 0, 0);
    row3Box->setSpacing(12);

    prev_ = makeCtlButton(false, row3);
    prev_->setIcon(ui::tintedIcon(QStringLiteral("prev"), kPrimary));
    prev_->setIconSize(QSize(20, 20));
    prev_->setToolTip(QStringLiteral("上一首"));
    play_ = makeCtlButton(true, row3);
    play_->setIcon(ui::tintedIcon(QStringLiteral("play"), kPrimary));
    play_->setIconSize(QSize(24, 24));
    play_->setToolTip(QStringLiteral("播放 / 暂停"));
    next_ = makeCtlButton(false, row3);
    next_->setIcon(ui::tintedIcon(QStringLiteral("next"), kPrimary));
    next_->setIconSize(QSize(20, 20));
    next_->setToolTip(QStringLiteral("下一首"));

    connect(prev_, &QPushButton::clicked, this, &MusicBar::prevClicked);
    connect(play_, &QPushButton::clicked, this, &MusicBar::playPauseClicked);
    connect(next_, &QPushButton::clicked, this, &MusicBar::nextClicked);

    row3Box->addWidget(prev_);
    row3Box->addWidget(play_);
    row3Box->addWidget(next_);
    row3Box->addStretch(1);
    leftBox->addWidget(row3, 1);           // 行 3 吃掉剩余高度（不是三等分）
    root->addWidget(left, 3);

    // ==================================================================== 右列
    auto* right = new QWidget(this);
    auto* rightBox = new QVBoxLayout(right);
    rightBox->setContentsMargins(0, 0, 0, 0);
    rightBox->setSpacing(4);

    lyrics_ = new QLabel(QStringLiteral("歌词未接入"), right);
    lyrics_->setObjectName(QStringLiteral("MusicLyric"));
    // ⚠ 不许让歌词/提示文本驱动布局宽度：否则一句长词会把整个窗口撑宽
    //   （T7 出图时实测 1280 → 1290）。让它在拉伸区里自己收缩。
    lyrics_->setSizePolicy(QSizePolicy::Ignored, QSizePolicy::Preferred);
    lyrics_->setWordWrap(true);
    lyrics_->setAlignment(Qt::AlignTop | Qt::AlignLeft);

    nextLyrics_ = new QLabel(QString(), right);
    nextLyrics_->setObjectName(QStringLiteral("MusicLyricNext"));
    nextLyrics_->setSizePolicy(QSizePolicy::Ignored, QSizePolicy::Preferred);
    nextLyrics_->setWordWrap(true);
    nextLyrics_->setAlignment(Qt::AlignTop | Qt::AlignLeft);
    nextLyrics_->setStyleSheet(QStringLiteral("color:%1; font-size:%2px;").arg(QLatin1String(theme::kTextDimDrift)).arg(theme::kFontXs));

    rightBox->addWidget(lyrics_);
    rightBox->addWidget(nextLyrics_);
    rightBox->addStretch(1);
    root->addWidget(right, 2);

    setConnected(false);                  // 还没收到链路消息：按钮先按不动
    setMusic(QJsonObject());              // 空载荷 = 回到"未播放"
}

QString MusicBar::title() const
{
    return title_ != nullptr ? title_->text() : QString();
}

void MusicBar::setMusic(const QJsonObject& music)
{
    const QJsonValue title = music.value(QStringLiteral("title"));
    if (title.isString()) {
        hasMusic_ = !title.toString().isEmpty();
        title_->setText(hasMusic_ ? title.toString() : QStringLiteral("未播放"));
    }
    const QJsonValue playing = music.value(QStringLiteral("playing"));
    if (playing.isBool()) {
        playing_ = playing.toBool();
    }
    const QJsonValue artist = music.value(QStringLiteral("artist"));
    if (artist.isString()) {
        const QString name = artist.toString().trimmed();
        artist_->setText(name.isEmpty() ? QStringLiteral("歌手 —")
                                        : QStringLiteral("歌手 %1").arg(name));
    }
    const QJsonValue album = music.value(QStringLiteral("album"));
    if (album.isString()) {
        const QString name = album.toString().trimmed();
        album_->setText(name.isEmpty() ? QStringLiteral("专辑 —")
                                       : QStringLiteral("专辑 %1").arg(name));
    }

    // 进度：协议里是"部分更新"，但 position/duration 总是成对出现（同一条 snapshot）
    const QJsonValue position = music.value(QStringLiteral("position_s"));
    const QJsonValue duration = music.value(QStringLiteral("duration_s"));
    if (position.isDouble() || duration.isDouble()) {
        setProgress(position.toDouble(0.0), duration.toDouble(0.0));
    }

    // 中间大按钮兼作状态显示
    if (play_ != nullptr) {
        if (!hasMusic_) {
            play_->setIcon(ui::tintedIcon(QStringLiteral("play"), kOff));
        } else if (playing_) {
            play_->setIcon(ui::tintedIcon(QStringLiteral("pause"), kPrimary));
        } else {
            play_->setIcon(ui::tintedIcon(QStringLiteral("play"), kPrimary));
        }
    }
}

void MusicBar::setProgress(double positionS, double durationS)
{
    position_ = positionS;
    duration_ = durationS;
    const bool known = durationS > 0.0;
    if (time_ != nullptr) {
        // 时长不知道（没起播 / 不是本机在放）就连位置都不显示 —— 显示 "0:00 / 0:00"
        // 会让人以为"正在播但停在 0 秒"。
        time_->setText(known ? QStringLiteral("%1 / %2").arg(formatClock(positionS),
                                                           formatClock(durationS))
                             : QStringLiteral("—:— / —:—"));
        applyStyle(time_, &timeStyle_,
                   known ? QString()
                         : QStringLiteral("color:%1;").arg(QLatin1String(theme::kTextDimDrift)));
    }
    if (progress_ != nullptr) {
        progress_->setEnabled(known);
        int permille = 0;
        if (known) {
            const double ratio = qBound(0.0, positionS / durationS, 1.0);
            permille = static_cast<int>(ratio * 1000.0 + 0.5);
        }
        progress_->setValue(permille);
        progress_->setToolTip(known ? QStringLiteral("%1 / %2")
                                          .arg(formatClock(positionS), formatClock(durationS))
                                    : QStringLiteral("还没有进度（还没起播 / 不是本机在放）"));
    }
}

void MusicBar::setLyricsProvider(core::LyricsProvider* provider)
{
    provider_ = provider;
    if (provider_ != nullptr) {
        provider_->setPosition(position_);   // 先把"现在放到哪了"喂进去再问
    }
    refreshLyrics();
}

void MusicBar::setPosition(double positionS)
{
    position_ = positionS;
    if (provider_ != nullptr) {
        provider_->setPosition(positionS);
    }
    refreshLyrics();
}

void MusicBar::setConnected(bool connected)
{
    connected_ = connected;
    for (QPushButton* button : {prev_, play_, next_}) {
        if (button == nullptr) {
            continue;
        }
        button->setEnabled(connected);
        if (!connected) {
            button->setToolTip(QStringLiteral("还没连上 Agent —— 音乐控制暂时按不动"));
        }
    }
    if (connected) {
        if (prev_ != nullptr) {
            prev_->setToolTip(QStringLiteral("上一首"));
        }
        if (play_ != nullptr) {
            play_->setToolTip(QStringLiteral("播放 / 暂停"));
        }
        if (next_ != nullptr) {
            next_->setToolTip(QStringLiteral("下一首"));
        }
    }
}

void MusicBar::applyStyle(QLabel* label, QString* cache, const QString& style)
{
    // T15-16 G-B-1：**内容没变就一次都不写** ✓
    //   改前这里每秒都会走一遍 setStyleSheet（值往往和上次完全一样 ✗）——
    //   而 setStyleSheet 会触发样式重算 + 重绘 ✓（music_bar.cpp:239 与 refreshLyrics 的四个分支 ✓）。
    if (label == nullptr || cache == nullptr || *cache == style) {
        return;
    }
    *cache = style;
    ++styleWrites_;                  // 单测靠它判断"有没有白写" ✓
    label->setStyleSheet(style);
}

void MusicBar::refreshLyrics()
{
    if (lyrics_ == nullptr || nextLyrics_ == nullptr) {
        return;
    }
    if (provider_ == nullptr) {
        lyrics_->setText(QStringLiteral("歌词未接入"));
        applyStyle(lyrics_, &lyricsStyle_, QString());
        nextLyrics_->clear();
        return;
    }
    if (!provider_->available()) {
        // 没歌词：有话说就显示那句话（琥珀），否则是"还没取到"（灰点，别让人以为坏了）
        const QString reason = provider_->reason();
        if (reason.isEmpty()) {
            lyrics_->setText(QStringLiteral("♪"));
            applyStyle(lyrics_, &lyricsStyle_,
                       QStringLiteral("color:%1;").arg(QLatin1String(theme::kTextDimDrift)));
        } else {
            lyrics_->setText(reason);
            applyStyle(lyrics_, &lyricsStyle_,
                       QStringLiteral("color:%1;").arg(QLatin1String(theme::kWarn)));
        }
        nextLyrics_->clear();
        return;
    }
    lyrics_->setText(provider_->currentLine());
    applyStyle(lyrics_, &lyricsStyle_, QString());
    nextLyrics_->setText(provider_->nextLine());
}
