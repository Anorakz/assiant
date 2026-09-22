// ============================================================================
//  gui/src/ui/pages.cpp — 页面实现的 T1 骨架
//
//  布局取自方案 §2（v5 定稿版）：
//
//      ┌───────────────────────────────────────────────────────────┐
//      │ 上区域·通栏 (MainWindow)                                   │
//      ├────────┬─────────────────────────────────┬───────────────┤
//      │ 左区域 │ 中部: 主区                      │ 右区域 320px  │
//      │ 导航   │       + 下区域 96px(不通栏)      │  模式按钮区   │
//      │ 96px   │                                 │  对话区       │
//      └────────┴─────────────────────────────────┴───────────────┘
//
//  T1 只保证"骨架与截图"，四个区域都是带标题的占位框。
// ============================================================================
#include "ui/pages.h"

#include "ui/bottom_bar.h"
#include "ui/chat_panel.h"
#include "ui/mode_panel.h"
#include "ui/model_page.h"
#include "ui/region_host.h"
#include "ui/schedule_panel.h"
#include "ui/settings_page.h"
#include "ui/sys_page.h"
#include "ui/video_panel.h"

#include <QFrame>
#include <QHBoxLayout>
#include <QLabel>
#include <QPushButton>
#include <QSizePolicy>
#include <QStackedWidget>
#include <QVBoxLayout>

namespace {

/// 区域标题 + 说明的小卡片。后续任务把内容塞进同一个 QFrame 即可。
QLabel* makeTitle(const QString& text, const QString& objectName)
{
    auto* label = new QLabel(text);
    label->setObjectName(objectName);
    label->setWordWrap(true);
    return label;
}

} // namespace

QWidget* makeArea(const QString& title, const QString& hint, QWidget* parent, bool bare)
{
    auto* frame = new QFrame(parent);
    // bare = 主区：不画背景/边框，完全留给全局壁纸（方案 §3.2）
    frame->setObjectName(bare ? QStringLiteral("AreaFrameBare") : QStringLiteral("AreaFrame"));
    frame->setFrameShape(QFrame::NoFrame);

    auto* box = new QVBoxLayout(frame);
    box->setContentsMargins(bare ? 0 : 16, bare ? 0 : 14, bare ? 0 : 16, bare ? 0 : 14);
    box->setSpacing(8);

    box->addWidget(makeTitle(title, bare ? QStringLiteral("AreaTitleBare")
                                         : QStringLiteral("AreaTitle")));
    if (!hint.isEmpty()) {
        auto* sub = new QLabel(hint);
        sub->setObjectName(bare ? QStringLiteral("AreaHintBare") : QStringLiteral("AreaHint"));
        sub->setWordWrap(true);
        box->addWidget(sub);
    }
    box->addStretch(1);
    return frame;
}

const QVector<PageEntry>& pageEntries()
{
    // 顺序 = 左区域从上到下；设置由导航栏用分隔线下沉（见 MainWindow）
    static const QVector<PageEntry> kEntries = {
        {QStringLiteral("home"), QStringLiteral("主页面")},
        {QStringLiteral("model"), QStringLiteral("模型测试")},
        {QStringLiteral("system"), QStringLiteral("系统")},
        {QStringLiteral("settings"), QStringLiteral("设置")},
    };
    return kEntries;
}

PlaceholderPage::PlaceholderPage(const QString& title, const QString& hint, QWidget* parent)
    : QWidget(parent)
{
    auto* box = new QVBoxLayout(this);
    box->setContentsMargins(16, 16, 16, 16);
    box->setSpacing(12);
    box->addWidget(makeArea(title, hint, this), 1);
}

MainPage::MainPage(QWidget* parent)
    : QWidget(parent)
{
    auto* root = new QHBoxLayout(this);
    root->setContentsMargins(16, 16, 16, 16);
    root->setSpacing(16);

    // ---- 中部：主区（伸展）+ 下区域（固定 96，不通栏，可折叠）----
    auto* middle = new QWidget(this);
    auto* midBox = new QVBoxLayout(middle);
    midBox->setContentsMargins(0, 0, 0, 0);
    midBox->setSpacing(12);

    // ---- 主区（T8/T9）：页0 = bare 留给壁纸；页1 = 视频区（游戏模式）----
    mainStack_ = new QStackedWidget(middle);
    mainStack_->setObjectName(QStringLiteral("MainAreaStack"));

    auto* mainArea = new QFrame(mainStack_);
    mainArea->setObjectName(QStringLiteral("AreaFrameBare"));
    auto* mainBox = new QVBoxLayout(mainArea);
    mainBox->setContentsMargins(4, 4, 4, 4);
    mainBox->setSpacing(6);

    mainHint_ = new QLabel(mainArea);
    mainHint_->setObjectName(QStringLiteral("AreaHintBare"));
    mainHint_->setWordWrap(true);
    mainHint_->setText(QStringLiteral("T9：游戏模式 = 真视频（QVideoWidget + 播放控制）\n"
                                      "T8：非游戏模式 = 不画背景与边框，完全留给全局壁纸"));
    mainBox->addWidget(mainHint_, 0, Qt::AlignTop | Qt::AlignLeft);
    mainBox->addStretch(1);

    auto* cornerRow = new QHBoxLayout();
    cornerRow->setContentsMargins(0, 0, 0, 0);
    cornerRow->addStretch(1);
    nextWallpaper_ = new QPushButton(QStringLiteral("下一张"), mainArea);
    nextWallpaper_->setObjectName(QStringLiteral("NextWallpaper"));
    nextWallpaper_->setCursor(Qt::PointingHandCursor);
    connect(nextWallpaper_, &QPushButton::clicked, this,
            [this]() { emit nextWallpaperRequested(); });
    cornerRow->addWidget(nextWallpaper_);
    mainBox->addLayout(cornerRow);

    mainStack_->addWidget(mainArea);

    videoPanel_ = new VideoPanel(mainStack_);
    mainStack_->addWidget(videoPanel_);

    mainStack_->setCurrentIndex(0);
    midBox->addWidget(mainStack_, 1);

    QWidget* bottomContent = new QFrame(middle);
    bottomContent->setObjectName(QStringLiteral("AreaFrame"));
    {
        auto* bottomBox = new QVBoxLayout(bottomContent);
        bottomBox->setContentsMargins(16, 10, 16, 10);
        bottomBar_ = new BottomBar(bottomContent);
        bottomBox->addWidget(bottomBar_);
    }
    // 下区域高度 = 96 × 2（验收要求）
    bottomContent->setFixedHeight(192);
    bottomRegion_ = new RegionHost(RegionHost::Edge::Bottom, bottomContent, middle);
    midBox->addWidget(bottomRegion_);

    // ---- 右区域：模式切换按钮区（T5）+ 对话区（T5，含输入行）----
    auto* rightContent = new QWidget(this);
    rightContent->setFixedWidth(320);
    auto* rightBox = new QVBoxLayout(rightContent);
    rightBox->setContentsMargins(0, 0, 0, 0);
    rightBox->setSpacing(12);

    auto* modeFrame = new QFrame(rightContent);
    modeFrame->setObjectName(QStringLiteral("AreaFrame"));
    auto* modeBox = new QVBoxLayout(modeFrame);
    modeBox->setContentsMargins(14, 10, 14, 10);
    modePanel_ = new ModePanel(modeFrame);
    modeBox->addWidget(modePanel_);
    // 高度**跟着内容走**（按钮 3 个 / 1 个会不一样），不写死，免得又太高
    modeFrame->setSizePolicy(QSizePolicy::Preferred, QSizePolicy::Fixed);
    rightBox->addWidget(modeFrame);

    chatFrame_ = new QFrame(rightContent);
    // ⚠ 三块都用 "AreaFrame"：卡片背景/边框由 QSS `QFrame#AreaFrame` 提供，
    //   改名就等于把卡片样式丢了。要区分它们用 MainPage 的访问器。
    chatFrame_->setObjectName(QStringLiteral("AreaFrame"));
    auto* chatBox = new QVBoxLayout(chatFrame_);
    chatBox->setContentsMargins(16, 14, 16, 14);
    chatPanel_ = new ChatPanel(chatFrame_);
    chatBox->addWidget(chatPanel_);

    // ---- 日程区（S5）：数据由 MainWindow::reloadSchedule() 灌进来 ----
    //  它自己**不读配置**（解析在 core::ScheduleModel，装配在 MainWindow），
    //  这样控件级单测不需要任何配置文件。
    scheduleFrame_ = new QFrame(rightContent);
    scheduleFrame_->setObjectName(QStringLiteral("AreaFrame"));
    auto* scheduleBox = new QVBoxLayout(scheduleFrame_);
    scheduleBox->setContentsMargins(16, 14, 16, 14);
    schedulePanel_ = new SchedulePanel(scheduleFrame_);
    scheduleBox->addWidget(schedulePanel_);

    // 对话区 : 日程区 = 3 : 2（方案 S5）。不用可拖拽分隔条：触摸屏上抓手难按，
    // 而且会和 RegionHost 的折叠动画/固定几何互相干扰。
    rightBox->addWidget(chatFrame_, 3);
    rightBox->addWidget(scheduleFrame_, 2);

    rightRegion_ = new RegionHost(RegionHost::Edge::Right, rightContent, this);

    root->addWidget(middle, 1);
    root->addWidget(rightRegion_);
}

void MainPage::setMainHint(const QString& text, bool warn)
{
    if (mainHint_ == nullptr) {
        return;
    }
    mainHint_->setText(text);
    // 出错（例如壁纸读不到）要看得出来，别用那套"占位灰"
    mainHint_->setStyleSheet(warn ? QStringLiteral("color:#F59E0B; background:transparent;")
                                  : QString());
    // T3：空字符串 = 藏起来。壁纸一到位，主区那两行开发占位文字就该让位
    //     （否则它会压在壁纸上）。
    mainHint_->setVisible(!text.isEmpty() && !isGameMode_);
}

void MainPage::setGameMode(bool game)
{
    isGameMode_ = game;
    // 游戏模式主区换成 T9 的视频区；非游戏模式回到"完全留给壁纸"的那页
    if (mainStack_ != nullptr) {
        mainStack_->setCurrentIndex(game ? 1 : 0);
    }
    if (nextWallpaper_ != nullptr) {
        nextWallpaper_->setVisible(!game);
    }
    if (mainHint_ != nullptr) {
        // 空提示在切回非游戏模式时也不该冒出来（见 setMainHint）
        mainHint_->setVisible(!game && !mainHint_->text().isEmpty());
    }
    if (!game && videoPanel_ != nullptr) {
        videoPanel_->pause();      // 离开游戏模式就别在后台继续解码
    }
}

QWidget* createPage(const QString& key, QWidget* parent)
{
    if (key == QLatin1String("home")) {
        return new MainPage(parent);
    }
    if (key == QLatin1String("model")) {
        return new ModelPage(parent);        // T11：模型测试（配置/同步/服务控制）
    }
    if (key == QLatin1String("system")) {
        return new SysPage(parent);            // T10：系统与网络
    }
    if (key == QLatin1String("settings")) {
        return new SettingsPage(parent);    // T13：设置
    }
    return nullptr;
}
