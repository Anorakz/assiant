// ============================================================================
//  gui/src/ui/video_panel.cpp — 视频区实现（内嵌控制条 + 播放/暂停 + 真全屏）
// ============================================================================
#include "ui/video_panel.h"

#include "ui/icons.h"

#include <QColor>

#include <QDebug>
#include <QHBoxLayout>
#include <QLabel>
#include <QMediaPlayer>
#include <QMenu>
#include <QPushButton>
#include <QResizeEvent>
#include <QStackedWidget>
#include <QTimer>
#include <QToolButton>
#include <QUrl>
#include <QVBoxLayout>
#include <QVideoWidget>

#include <functional>

namespace {

/// 画面页：自己的 resize 也要触发排版（只在 VideoPanel 上挂 resizeEvent 不够 ——
/// 页面尺寸由布局决定，面板不一定收到 resize，实测视频窗口会停在很小的一块）
class VideoPage : public QWidget {
public:
    explicit VideoPage(QWidget* parent = nullptr)
        : QWidget(parent)
    {
    }
    std::function<void()> onResize;

protected:
    void resizeEvent(QResizeEvent* event) override
    {
        QWidget::resizeEvent(event);
        if (onResize) {
            onResize();
        }
    }
};

/// 画面区底部给内嵌控制条预留的高度
constexpr int kOverlayHeight = 56;
/// 控制条左右留白（让它看着是"浮"在画面里，而不是贴边）
constexpr int kOverlayMargin = 16;

/// 符号化按钮：只有按钮自己有半透明底，**没有整条背景**（验收：不要实体化）
QPushButton* makeButton(const QString& text, const QString& objectName, QWidget* parent)
{
    auto* button = new QPushButton(text, parent);
    button->setObjectName(objectName);
    button->setCursor(Qt::PointingHandCursor);
    button->setFixedHeight(40);
    button->setMinimumWidth(48);
    return button;
}

} // namespace

VideoPanel::VideoPanel(QWidget* parent)
    : QWidget(parent)
{
    auto* root = new QVBoxLayout(this);
    root->setContentsMargins(0, 0, 0, 0);
    root->setSpacing(0);

    stage_ = new QStackedWidget(this);
    stage_->setObjectName(QStringLiteral("VideoStage"));

    // ---- 页 0：占位 ----
    placeholder_ = new QLabel(QStringLiteral("视频源未接入"), stage_);
    placeholder_->setObjectName(QStringLiteral("VideoPlaceholder"));
    placeholder_->setAlignment(Qt::AlignCenter);
    stage_->addWidget(placeholder_);

    // ---- 页 1：画面（视频 + 内嵌控制条，两个兄弟，由 resizeEvent 排版）----
    auto* page = new VideoPage(stage_);
    page->onResize = [this]() { relayoutStage(); };
    videoPage_ = page;
    videoPage_->setObjectName(QStringLiteral("VideoPage"));

    video_ = new QVideoWidget(videoPage_);
    video_->setObjectName(QStringLiteral("VideoSurface"));
    video_->setMinimumSize(0, 0);        // 尺寸交给 relayoutStage() 算

    overlay_ = new QWidget(videoPage_);
    overlay_->setObjectName(QStringLiteral("VideoOverlay"));
    auto* overlayBox = new QHBoxLayout(overlay_);
    overlayBox->setContentsMargins(kOverlayMargin, 0, kOverlayMargin, 0);
    overlayBox->setSpacing(8);

    // 只在这几个按钮外面套一层"胶囊"，且是半透明的 —— 不是整条实体背景
    auto* pill = new QWidget(overlay_);
    pill->setObjectName(QStringLiteral("VideoPill"));
    auto* pillBox = new QHBoxLayout(pill);
    pillBox->setContentsMargins(10, 6, 10, 6);
    pillBox->setSpacing(8);

    // T14：控制条改用自绘单色图标（不再依赖字体码位，彻底避开"⏸ 缺码位"那类问题）
    previous_ = makeButton(QString(), QStringLiteral("VideoCtl"), pill);
    previous_->setIcon(ui::tintedIcon(QStringLiteral("prev"), QColor(0xE6, 0xE6, 0xE6)));
    previous_->setIconSize(QSize(20, 20));
    play_ = makeButton(QString(), QStringLiteral("VideoCtlPlay"), pill);
    // 这颗是蓝底深色字 → 图标用同色，否则浅色图标压在浅蓝上几乎看不见
    play_->setIcon(ui::tintedIcon(QStringLiteral("play"), QColor(0x12, 0x14, 0x1A)));
    play_->setIconSize(QSize(22, 22));
    next_ = makeButton(QString(), QStringLiteral("VideoCtl"), pill);
    next_->setIcon(ui::tintedIcon(QStringLiteral("next"), QColor(0xE6, 0xE6, 0xE6)));
    next_->setIconSize(QSize(20, 20));
    speed_ = new QToolButton(pill);
    speed_->setObjectName(QStringLiteral("VideoSpeed"));
    speed_->setText(QStringLiteral("1.0x"));
    speed_->setIcon(ui::tintedIcon(QStringLiteral("speed"), QColor(0xD6, 0xDA, 0xE0)));
    speed_->setIconSize(QSize(18, 18));
    speed_->setPopupMode(QToolButton::InstantPopup);
    speed_->setFixedHeight(40);
    speed_->setMinimumWidth(56);
    speed_->setCursor(Qt::PointingHandCursor);
    auto* speedMenu = new QMenu(speed_);
    for (const QString& label : {QStringLiteral("0.5x"), QStringLiteral("0.75x"),
                                 QStringLiteral("1.0x"), QStringLiteral("1.5x"),
                                 QStringLiteral("2.0x"), QStringLiteral("3.0x")}) {
        QAction* action = speedMenu->addAction(label);
        action->setData(label);
        connect(action, &QAction::triggered, this, [this, label]() {
            speed_->setText(label);                       // 只改显示，真要改速率得后端支持
            triggerPlaceholder(QStringLiteral("倍速"));
        });
    }
    speed_->setMenu(speedMenu);
    fullscreen_ = makeButton(QString(), QStringLiteral("VideoCtl"), pill);
    fullscreen_->setIcon(ui::tintedIcon(QStringLiteral("fullscreen"), QColor(0xE6, 0xE6, 0xE6)));
    fullscreen_->setIconSize(QSize(20, 20));

    pillBox->addWidget(previous_);
    pillBox->addWidget(play_);
    pillBox->addWidget(next_);
    pillBox->addWidget(speed_);
    pillBox->addWidget(fullscreen_);

    overlayBox->addStretch(1);
    overlayBox->addWidget(pill);        // 验收：这几个按钮**居中**
    overlayBox->addStretch(1);

    stage_->addWidget(videoPage_);

    // ---- 播放器 ----
    player_ = new QMediaPlayer(this);
    player_->setVideoOutput(video_);
    QObject::connect(player_, QOverload<QMediaPlayer::Error>::of(&QMediaPlayer::error), this,
                     [this](QMediaPlayer::Error) {
                         if (!source_.isEmpty()) {
                             placeholder_->setText(
                                 QStringLiteral("视频源未接入（播放失败：%1）")
                                     .arg(player_->errorString()));
                             setStageVideo(false);
                         }
                     });
    connect(player_, &QMediaPlayer::stateChanged, this, [this](QMediaPlayer::State state) {
        const bool playing = (state == QMediaPlayer::PlayingState);
        // 只画符号：▶ = 点了会播；|| = 点了会停（⏸ 在板端字体缺码位，不能用）
        // 图标 + 文字各留一份：图标给"动作"，文字（1.0x 之类）保持可读
        play_->setIcon(ui::tintedIcon(playing ? QStringLiteral("pause") : QStringLiteral("play"),
                                      QColor(0x12, 0x14, 0x1A)));
        play_->setText(QString());
        emit playingChanged(playing);
    });

    auto* progressTimer = new QTimer(this);
    progressTimer->setInterval(2000);
    connect(progressTimer, &QTimer::timeout, this, [this]() {
        if (player_->state() != QMediaPlayer::StoppedState) {
            qInfo().noquote() << QStringLiteral("[video] state=%1 pos=%2ms dur=%3ms rate=%4")
                                     .arg(player_->state())
                                     .arg(player_->position())
                                     .arg(player_->duration())
                                     .arg(player_->playbackRate());
        }
    });
    progressTimer->start();

    root->addWidget(stage_, 1);

    // ---- 按钮接线 ----
    connect(previous_, &QPushButton::clicked, this,
            [this]() { triggerPlaceholder(QStringLiteral("上一集")); });
    connect(next_, &QPushButton::clicked, this, [this]() {
        triggerPlaceholder(QStringLiteral("下一集"));      // 先给"未接入"说明
        emit nextBilibiliRequested();                      // 协议那边照样发（Agent 决定切集）
    });
    connect(play_, &QPushButton::clicked, this, [this]() { togglePlayPause(); });
    connect(fullscreen_, &QPushButton::clicked, this, [this]() { setFullscreen(!fullscreen_); });

    setSource(QString());
}

VideoPanel::~VideoPanel() = default;

void VideoPanel::resizeEvent(QResizeEvent* event)
{
    QWidget::resizeEvent(event);
    relayoutStage();
}

void VideoPanel::relayoutStage()
{
    // 视频占上部；底部 kOverlayHeight 留给内嵌控制条（必须落在视频窗口之外，
    // 否则会被视频的原生窗口盖住 —— 实测过）
    const int w = videoPage_->width();
    const int h = videoPage_->height();
    const int videoH = qMax(1, h - kOverlayHeight);
    video_->setGeometry(0, 0, w, videoH);
    overlay_->setGeometry(0, videoH, w, kOverlayHeight);
}

void VideoPanel::setStageVideo(bool video)
{
    stage_->setCurrentWidget(video ? static_cast<QWidget*>(videoPage_)
                                   : static_cast<QWidget*>(placeholder_));
    if (video) {
        relayoutStage();
    }
}

void VideoPanel::setSource(const QString& fileOrUrl)
{
    source_ = fileOrUrl;
    noteText_.clear();
    if (source_.isEmpty()) {
        player_->stop();
        player_->setMedia(QMediaContent());
        placeholder_->setText(QStringLiteral("视频源未接入"));
        setStageVideo(false);
        return;
    }
    player_->setMedia(QUrl::fromLocalFile(source_));
    setStageVideo(true);
    play();
}

void VideoPanel::play()
{
    if (!source_.isEmpty()) {
        player_->play();
    }
}

void VideoPanel::pause()
{
    player_->pause();
}

void VideoPanel::togglePlayPause()
{
    if (source_.isEmpty()) {
        return;                     // 没源没啥可切的
    }
    if (player_->state() == QMediaPlayer::PlayingState) {
        player_->pause();
    } else {
        player_->play();
    }
}

bool VideoPanel::isPlaying() const
{
    return player_->state() == QMediaPlayer::PlayingState;
}

void VideoPanel::setFullscreen(bool on)
{
    if (fullscreenOn_ == on) {
        return;
    }
    fullscreenOn_ = on;
    fullscreen_->setText(QString());   // 只有图标
    fullscreen_->setToolTip(on ? QStringLiteral("退出全屏") : QStringLiteral("全屏"));
    emit fullscreenToggled(on);
}

void VideoPanel::setOverlayVisible(bool visible)
{
    overlay_->setVisible(visible);
}

bool VideoPanel::overlayVisible() const
{
    // 用 isHidden()：isVisible() 还要求整条祖先链都 visible，单测里面板从不 show
    return !overlay_->isHidden();
}

void VideoPanel::triggerPlaceholder(const QString& what)
{
    noteText_ = QStringLiteral("%1未接入（协议暂无对应命令，先占位）").arg(what);
    // ⚠ 不把说明画在视频区里：实测视频的原生窗口会盖住同区域的兄弟控件。
    //   改由 MainWindow 送到右侧对话区的提示行。
    qInfo().noquote() << "[video] 占位项被点:" << what;
    emit placeholderClicked(what);
}
