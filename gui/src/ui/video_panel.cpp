// ============================================================================
//  gui/src/ui/video_panel.cpp — 视频区实现（画面 + 控制条 + 预览栏 + 真全屏）
// ============================================================================
#include "ui/video_panel.h"
#include "ui/theme.h"    // T15-16 G-C-1：视觉常量（唯一来源）

#include "ui/bilibili_preview.h"
#include "ui/icons.h"

#include <QColor>

#include <QDebug>
#include <QHBoxLayout>
#include <QJsonObject>
#include <QJsonValue>
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
#include "gst_video_widget.h"   // dmabuf 零拷贝主路径（失败回退 QMediaPlayer ✓）

#include <functional>

namespace {

/// **画面那一块**自己的 resize 也要触发排版（只在 VideoPanel 上挂 resizeEvent 不够 ——
/// 那个块的尺寸由布局决定，面板不一定收到 resize，实测视频窗口会停在很小的一块）
class ScreenHost : public QWidget {
public:
    explicit ScreenHost(QWidget* parent = nullptr)
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
    button->setFixedHeight(44);
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

    // ---- 页 1：画面页 = [画面块][预览栏] ----
    videoPage_ = new QWidget(stage_);
    videoPage_->setObjectName(QStringLiteral("VideoPage"));
    auto* pageBox = new QVBoxLayout(videoPage_);
    pageBox->setContentsMargins(0, 0, 0, 0);
    pageBox->setSpacing(6);

    auto* screen = new ScreenHost(videoPage_);
    screen->setObjectName(QStringLiteral("VideoScreen"));
    screen->onResize = [this]() { relayoutStage(); };
    screen_ = screen;

    video_ = new QVideoWidget(screen_);
    video_->setObjectName(QStringLiteral("VideoSurface"));
    video_->setMinimumSize(0, 0);        // 尺寸交给 relayoutStage() 算

    // ---- 主路径：dmabuf 零拷贝渲染 ----
    //   ★ 实测教训（见文档 §6）：挂 `screen_`(页1 里的控件) 上时，只要 QStackedWidget 停在
    //   **占位页**，整条链就 invisible ⇒ `paintGL` 永远不被调用（CPU 收益有、画面没有 ✗）。
    //   ⇒ 所以把它挂到 **VideoPanel 自己**身上（不在 stage_ 的页里 ✓），页切换与它无关 ✓。
    gst_ = new GstVideoWidget(screen_);
    //: ★ 改回直接作为 QWidget 子控件（`QOpenGLWindow` ＋ `createWindowContainer` 在 eglfs 上崩 ✗，已判死 ✓）
    //:   `gstHost_` 仍保留为"当前生效的承载控件"指针 ✓ ⇒ relayoutStage 里统一作用于它 ✓
    gstHost_ = gst_;
    gstHost_->setObjectName(QStringLiteral("GstVideoSurface"));
    gstHost_->setMinimumSize(0, 0);
    gstHost_->hide();                    // 默认不显示；setSource 成功后由 relayoutStage 决定
    connect(gst_, &GstVideoWidget::playingChanged, this, [this](bool playing) {
        play_->setIcon(ui::tintedIcon(playing ? QStringLiteral("pause") : QStringLiteral("play"),
                                      QColor(theme::kOnAccent)));
        play_->setText(QString());
        emit playingChanged(playing);
    });
    connect(gst_, &GstVideoWidget::endOfMedia, this, [this]() { notifyEndOfMedia(); });
    connect(gst_, &GstVideoWidget::failedOver, this, [this]() {
        // 零拷贝这条路失败 ⇒ 回退（若当前正用它）
        if (usingGst_) {
            qWarning() << "[video] 零拷贝路径失败 ⇒ 回退 QMediaPlayer";
            usingGst_ = false;
            gstHost_->hide();
            video_->show();
            if (!source_.isEmpty()) player_->setMedia(QMediaContent(QUrl(source_)));
            relayoutStage();
        }
    });

    overlay_ = new QWidget(screen_);
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
    previous_->setIcon(ui::tintedIcon(QStringLiteral("prev"), QColor(theme::kText)));
    previous_->setIconSize(QSize(20, 20));
    previous_->setToolTip(QStringLiteral("上一集"));
    play_ = makeButton(QString(), QStringLiteral("VideoCtlPlay"), pill);
    // 这颗是蓝底深色字 → 图标用同色，否则浅色图标压在浅蓝上几乎看不见
    play_->setIcon(ui::tintedIcon(QStringLiteral("play"), QColor(theme::kOnAccent)));
    play_->setIconSize(QSize(22, 22));
    next_ = makeButton(QString(), QStringLiteral("VideoCtl"), pill);
    next_->setIcon(ui::tintedIcon(QStringLiteral("next"), QColor(theme::kText)));
    next_->setIconSize(QSize(20, 20));
    next_->setToolTip(QStringLiteral("下一集"));
    speed_ = new QToolButton(pill);
    speed_->setObjectName(QStringLiteral("VideoSpeed"));
    speed_->setText(QStringLiteral("1.0x"));
    speed_->setIcon(ui::tintedIcon(QStringLiteral("speed"), QColor(theme::kBtnText2)));
    speed_->setIconSize(QSize(18, 18));
    speed_->setPopupMode(QToolButton::InstantPopup);
    speed_->setFixedHeight(44);
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
    fullscreen_->setIcon(ui::tintedIcon(QStringLiteral("fullscreen"), QColor(theme::kText)));
    fullscreen_->setIconSize(QSize(20, 20));

    pillBox->addWidget(previous_);
    pillBox->addWidget(play_);
    pillBox->addWidget(next_);
    pillBox->addWidget(speed_);
    pillBox->addWidget(fullscreen_);

    overlayBox->addStretch(1);
    overlayBox->addWidget(pill);        // 验收：这几个按钮**居中**
    overlayBox->addStretch(1);

    pageBox->addWidget(screen_, 1);

    // 预览栏 + 地址栏（T11-7）—— ⚠ 放在 **stage 之外**（VideoPanel 的根布局里）：
    //   放 stage 里的话，"还没开始放"（占位页）时整条预览栏会被藏起来 ——
    //   板端实测截图就是这个后果：队列有了、预览栏却看不见。
    preview_ = new BilibiliPreview(this);

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
                                      QColor(theme::kOnAccent)));
        play_->setText(QString());
        emit playingChanged(playing);
    });
    // 「这一条放完了」：真机靠这条信号（Agent 收到 eof 会自动下一集）
    connect(player_, &QMediaPlayer::mediaStatusChanged, this, [this](QMediaPlayer::MediaStatus s) {
        if (s == QMediaPlayer::EndOfMedia) {
            notifyEndOfMedia();
        }
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
        // 每 2 秒如实回报一次进度：Agent 靠它知道"在放 / 暂停了"（暂停就多预取）
        reportVideoState();
    });
    progressTimer->start();

    root->addWidget(stage_, 1);
    root->addWidget(preview_, 0);       // 预览栏贴在视频区底下（占位态也看得见）

    // ---- 按钮接线 ----
    // T11-7：上一集/下一集都**转正**了（协议里有 prev_bilibili / next_bilibili）。
    //   给出的说明改由 **Agent** 回（队列空了它会推一句聊天气泡）——GUI 不再假报"未接入"。
    connect(previous_, &QPushButton::clicked, this, [this]() {
        qInfo().noquote() << QStringLiteral("[bilibili] 点了上一集 -> prev_bilibili");
        emit prevBilibiliRequested();
    });
    connect(next_, &QPushButton::clicked, this, [this]() {
        qInfo().noquote() << QStringLiteral("[bilibili] 点了下一集 -> next_bilibili");
        emit nextBilibiliRequested();
    });
    connect(play_, &QPushButton::clicked, this, [this]() { togglePlayPause(); });
    connect(fullscreen_, &QPushButton::clicked, this, [this]() { setFullscreen(!fullscreen_); });

    // 预览栏的两条：挑片 / 格数变了要上报
    connect(preview_, &BilibiliPreview::pickRequested, this, &VideoPanel::pickBilibiliRequested);
    connect(preview_, &BilibiliPreview::viewportChanged, this, &VideoPanel::viewportChanged);

    setSource(QString());
}

VideoPanel::~VideoPanel() = default;

void VideoPanel::setCoverLoader(CoverLoader* loader)
{
    if (preview_ != nullptr) {
        preview_->setCoverLoader(loader);
    }
}

void VideoPanel::setBilibili(const QJsonObject& data)
{
    preview_->setData(data);

    // T11-10f：Agent 让**播放器**播放/暂停（CLI `assistant video play|pause|toggle` 走这条）。
    //   `control` 只在"真的下了命令"的那条推送里出现 —— 补推里没有它，所以刚连上的
    //   GUI 不会把上一条老命令重放一遍。`seq` 只是再上一道保险（重复推不动手）。
    const QJsonValue control = data.value(QStringLiteral("control"));
    if (control.isObject()) {
        const QJsonObject cmd = control.toObject();
        handleControl(cmd.value(QStringLiteral("action")).toString(),
                      cmd.value(QStringLiteral("seq")).toInt(0));
    }

    // `stream` 是 Agent 那边缓冲好之后给的本机 http 地址（T11-10 起；老方案是 FIFO 路径）。
    const QString stream = data.value(QStringLiteral("stream")).toString();
    if (stream.isEmpty()) {
        if (!source_.isEmpty()) {
            qInfo().noquote() << QStringLiteral("[bilibili] 缓冲没了（换条/清空/放完）-> 清屏");
            setSource(QString());
        }
        const int count = data.value(QStringLiteral("count")).toInt(0);
        if (count > 0) {
            placeholder_->setText(QStringLiteral("队列 %1 条 —— 点一下预览图就开始放").arg(count));
        }
        return;
    }
    if (stream == source_) {
        return;                       // 同一路流，别重开（重开会跳回 0 秒）
    }
    qInfo().noquote() << QStringLiteral("[bilibili] 开始放 %1").arg(stream);
    setSource(stream);
}

void VideoPanel::resizeEvent(QResizeEvent* event)
{
    QWidget::resizeEvent(event);
    relayoutStage();
}

void VideoPanel::relayoutStage()
{
    // 视频占画面块的上部；底部 kOverlayHeight 留给内嵌控制条（必须落在视频窗口之外，
    // 否则会被视频的原生窗口盖住 —— 实测过）
    QWidget* host = (screen_ != nullptr) ? screen_ : videoPage_;
    const int w = host->width();
    const int h = host->height();
    const int videoH = qMax(1, h - kOverlayHeight);
    video_->setGeometry(0, 0, w, videoH);
    // 零拷贝控件与 video_ **同一几何**；只显示当前生效的那条路（不黑屏 ✓）
    if (gst_ != nullptr) {
        // `gst_` 的父是 VideoPanel 自己 ⇒ 几何要按 screen_ 在 VideoPanel 里的位置换算 ✓
        // `gst_` 的父是 VideoPanel 自己 ⇒ 几何要按 screen_ 在 VideoPanel 里的位置换算 ✓
        //: ★ 实测坑：布局完成前 `screen_->width()` 只有 **100** ✗ ⇒ 画面被压成 `100x1` 的细线 ✗
        //:   ⇒ 所以只要 `screen_` 还没拿到合理尺寸，就**退回用 VideoPanel 自己的矩形** ✓✓
        const QPoint origin = (screen_ != nullptr) ? screen_->mapTo(this, QPoint(0, 0)) : QPoint(0, 0);
        //: ★★ 判别实验（2026-10-07，临时 ✓）：`DSH_GST_SMALL=1` ⇒ 把视频区缩到 320x180 ✓
        //:   用来分清 `paintGL` 只有 2fps 的真因 ✓：
        //:     ① 若缩小后 **fps 明显上升** ⇒ 是"每帧整窗 FBO 合成/blit 的成本" ✗
        //:     ② 若 fps **不变** ⇒ 是"paint 事件投递被合并" ✗（那就要绕开 Qt 合成 ✓）
        if (qEnvironmentVariableIsSet("DSH_GST_SMALL")) {
            gstHost_->setGeometry(0, 0, 320, 180);
            if (overlay_ != nullptr) overlay_->setGeometry(0, videoH, w, kOverlayHeight);
            return;
        }
        const bool hostReady = (screen_ != nullptr && screen_->width() > 200 && screen_->height() > 200);
        if (hostReady) {
            gstHost_->setGeometry(origin.x(), origin.y(), w, videoH);
        } else {
            gstHost_->setGeometry(0, 0, width(), qMax(1, height() - kOverlayHeight));
        }
        if (usingGst_) {
            //: ★★ 实测真因（心跳日志）：**父可见而 `gst_` 仍 isVisible=false** ✗
            //:   ⇒ `VideoPanel` 自己被隐藏了（`stage_->setCurrentWidget` 只管面板**内部** ✗）
            //:   ⇒ `gst_` 是 VideoPanel 的**直接子控件** ⇒ 父不 show，子永远不可见 ✗
            //:   修法：把面板与整条链都显式 show 出来 ✓（幂等 ✓）
            show();
            if (videoPage_ != nullptr) videoPage_->show();
            //:   原因是 `video_`(QVideoWidget = **原生窗口** ✗) 与普通控件同层时会把可视链弄断
            //:   （video_panel.h 里早写过"原生窗口会盖住兄弟控件" ✓）。
            //:   ⇒ 对策：把原生窗口**整个从父链摘掉**（不是只 hide ✗），并把可视链显式叫醒 ✓。
            //: ★★ 教训（2026-10-07 板端事故）：这里**绝不能再 `video_->setParent(nullptr)`** ✗
            //:   —— 摘掉原生窗口后，如果某个时序没挂回去，就会出现"**位置在涨、屏幕没画面**" ✗
            //:   （`QMediaPlayer` 照常解码，但输出到了一个不可见的顶层窗口 ✓）。
            //:   正确做法：**只 hide()，永不动父子关系** ✓；让 `gst_` 与 `video_` 并列即可 ✓。
            video_->hide();
            if (screen_ != nullptr) screen_->show();
            if (videoPage_ != nullptr) videoPage_->show();
            gstHost_->show();
            gstHost_->raise();
            gst_->update();
        } else {
            if (video_->parentWidget() != screen_) video_->setParent(screen_);   // 回退时挂回来 ✓
            gstHost_->hide();
            video_->setGeometry(0, 0, w, videoH);
            video_->show();
        }
    }
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
    eofSent_ = false;                 // 换了源 = 新的一条，eof 要能再报一次
    if (source_.isEmpty()) {
        usingGst_ = false;
        if (gst_ != nullptr) { gst_->setSource(QString()); gstHost_->hide(); }
        player_->stop();
        player_->setMedia(QMediaContent());
        placeholder_->setText(QStringLiteral("视频源未接入"));
        setStageVideo(false);
        return;
    }
    // ★ 主路径：本机 http 流走 dmabuf 零拷贝（起不来就回退到下面的 QMediaPlayer ✓）
    //   ⚠ 现状（2026-10-07）：零拷贝的 CPU 收益已实测（vqueue 0.82 核 ⇒ ≈0 ✓），但**画面还没画出来** ✗
    //   —— 心跳日志证实父控件 `VideoScreen` 一直 invisible ✗（不是 GL 的问题 ✓）。
    //   ⇒ 默认**关**（保证屏幕正常 ✗ 不回退体验 ✓），要用 `DSH_GST_VIDEO=1` 显式打开来继续调 ✓。
    const bool gstEnabled = qEnvironmentVariableIsSet("DSH_GST_VIDEO")
                            && qEnvironmentVariable("DSH_GST_VIDEO") != QLatin1String("0");
    if (gstEnabled && gst_ != nullptr && (source_.startsWith(QLatin1String("http://"))
                                          || source_.startsWith(QLatin1String("https://")))) {
        if (gst_->setSource(source_) && !gst_->lastOpFailed()) {
            usingGst_ = true;
            qInfo().noquote() << QStringLiteral("[video] 走零拷贝主路径（dmabuf ⇒ Qt EGL）");
        } else {
            usingGst_ = false;
            qWarning().noquote() << QStringLiteral("[video] 零拷贝起不来 ⇒ 回退 QMediaPlayer");
        }
    } else {
        usingGst_ = false;            // 本地文件（--video 验收）仍走 QMediaPlayer ✓
    }
    // T11-10：Agent 现在给的是**本机 http URL**（内存窗口 + chunked 边下边喂；板端实测
    //   `playbin` 对"不可 seek 的文件/FIFO"起不来，http 才行）。本地文件（`--video` 验收）
    //   照旧走 `fromLocalFile` —— 两种都得认，别把 `http://…` 当成路径拼成 `file:///…/http:/…`。
    const bool isUrl = source_.startsWith(QLatin1String("http://"))
                       || source_.startsWith(QLatin1String("https://"))
                       || source_.startsWith(QLatin1String("rtsp://"))
                       || source_.startsWith(QLatin1String("file://"));
    const QUrl media = isUrl ? QUrl(source_) : QUrl::fromLocalFile(source_);
    qInfo().noquote() << QStringLiteral("[video] 换源(%1): %2")
                             .arg(isUrl ? QStringLiteral("URL") : QStringLiteral("本地文件"),
                                  media.toString());
    //: ★★★ 2fps 疑似元凶（2026-10-07 判别实验：**缩小视频区 fps 完全不变** ✗ ⇒ 不是合成成本 ✗，
    //:   而是 **Qt 事件循环被饿死** ✗）：走零拷贝时**绝不能再把源交给 `player_`** ✗ ——
    //:   那会让 `playbin` 也起一条完整管线（连着同一个流 ✓、也在解码取流 ✓）⇒
    //:   ① `vqueue` 多一个空转实例 ✗ ② **事件循环被拖住** ⇒ `update()` 排不进 ⇒ 2fps ✗✓
    //:   ⇒ 只在"确实要回退"（`!usingGst_`）时才 `setMedia` ✓
    if (!usingGst_) {
        player_->setMedia(media);
    }
    setStageVideo(true);
    relayoutStage();                  // 让 gst_/video_ 的显隐生效
    play();
}

void VideoPanel::play()
{
    if (source_.isEmpty()) {
        return;
    }
    if (usingGst_ && gst_ != nullptr) {
        gst_->play();
        return;
    }
    player_->play();
}

void VideoPanel::pause()
{
    if (usingGst_ && gst_ != nullptr) {
        gst_->pause();
        return;
    }
    player_->pause();
}

void VideoPanel::togglePlayPause()
{
    if (source_.isEmpty()) {
        return;                     // 没源没啥可切的
    }
    if (usingGst_ && gst_ != nullptr) {
        gst_->togglePlayPause();
        return;
    }
    if (player_->state() == QMediaPlayer::PlayingState) {
        player_->pause();
    } else {
        player_->play();
    }
}

bool VideoPanel::isPlaying() const
{
    if (usingGst_ && gst_ != nullptr) {
        return gst_->isPlaying();
    }
    return player_->state() == QMediaPlayer::PlayingState;
}

bool VideoPanel::handleControl(const QString& action, int seq)
{
    // ⚠ `seq` 去重：Agent 那边只在"下了命令"时推一次，但万一同一条被重放，
    //   也**不能**再按一次（`toggle` 按两次就转回去了）。
    if (seq > 0 && seq <= lastControlSeq_) {
        qInfo().noquote() << QStringLiteral("[bilibili] 播放控制 seq=%1 不比上一条(%2)新 -> 不动手")
                                 .arg(seq).arg(lastControlSeq_);
        return false;
    }
    if (source_.isEmpty()) {
        // 没源 = 没东西可放；这是**如实忽略**，不是出错（Agent 那边也会先拦一道）。
        // ⚠ 忽略时**不记序号** —— 所以"序号没前进"就是"这条命令没被执行"的可测事实。
        qInfo().noquote() << QStringLiteral("[bilibili] 没在放，Agent 的播放控制 %1 忽略（seq=%2）")
                                 .arg(action).arg(seq);
        return false;
    }
    if (seq > 0) {
        lastControlSeq_ = seq;
    }
    qInfo().noquote() << QStringLiteral("[bilibili] Agent 让播放器 %1（seq=%2）")
                             .arg(action).arg(seq);
    if (action == QLatin1String("play")) {
        play();
    } else if (action == QLatin1String("pause")) {
        pause();
    } else if (action == QLatin1String("toggle")) {
        togglePlayPause();
    } else {
        qInfo().noquote() << QStringLiteral("[bilibili] 不认识的播放控制 %1（忽略）").arg(action);
        return false;
    }
    ++controlsApplied_;
    // 立刻回报一次进度/状态：Agent（和 CLI 的 --wait-player）不用等 2 秒定时器。
    // ⚠ 播放器的状态切换是异步的，所以晚 200 ms 再报 —— 报的是**它真实的**样子。
    QTimer::singleShot(200, this, [this]() { reportVideoState(); });
    return true;
}

void VideoPanel::reportVideoState()
{
    if (source_.isEmpty()) {
        return;                     // 没在放就不回报（别让 Agent 以为"0 秒在放"）
    }
    // 位置/时长从**当前生效的那条路**取（零拷贝走 gst_，回退走 player_）
    if (usingGst_ && gst_ != nullptr) {
        emit videoStateReported(gst_->positionMs(), gst_->durationMs(), gst_->isPlaying(), false);
        return;
    }
    emit videoStateReported(player_->position(), player_->duration(), isPlaying(), false);
}

void VideoPanel::notifyEndOfMedia()
{
    if (source_.isEmpty() || eofSent_) {
        return;
    }
    eofSent_ = true;                // 只报一次：接着 Agent 会推下一条的 stream
    qInfo().noquote() << QStringLiteral("[bilibili] 这一条放完了 -> 回报 eof（Agent 自动下一集）");
    emit videoStateReported(player_->position(), player_->duration(), false, true);
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
