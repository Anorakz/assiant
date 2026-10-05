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
#include "ui/theme.h"    // T15-16 G-A-1b：视觉常量（唯一来源）

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

    // ⚠ T7-3：右下角那个「下一张」按钮**删掉了**（连同 nextWallpaperRequested 信号
    //    与 --next-wallpaper-demo）。换壁纸只走对话 —— 按钮只能按文件名翻下一张，
    //    而标签化之后"换成什么样"该由自然语言说。布局上原来那一行只剩 stretch，
    //    所以整行一起去掉。

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
    modeFrame_ = modeFrame;
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
    rightBox_ = rightBox;

    root->addWidget(middle, 1);
    root->addWidget(rightRegion_);
}

void MainPage::setKeyboardInset(int px)
{
    if (chatFrame_ == nullptr || px == keyboardInset_) {
        return;                     // 值没变就别折腾布局（键盘矩形会连着报好几次）
    }
    keyboardInset_ = px;
    ++insetLayoutPasses_;          // G-B-6：走过防护才计数 ⇒ 数的是"真改了布局" ✓
    if (px <= 0) {                                          // 收起键盘：一切复原
        chatFrame_->setMaximumHeight(QWIDGETSIZE_MAX);
        if (modeFrame_ != nullptr) {
            modeFrame_->setMaximumHeight(QWIDGETSIZE_MAX);
            modeFrame_->setVisible(true);
        }
        if (scheduleFrame_ != nullptr) {
            scheduleFrame_->setMaximumHeight(QWIDGETSIZE_MAX);
            scheduleFrame_->setVisible(true);
        }
        if (tailSpacer_ != nullptr && rightBox_ != nullptr) {
            rightBox_->removeItem(tailSpacer_);
            delete tailSpacer_;
            tailSpacer_ = nullptr;
        }
        return;
    }
    // 键盘一出来就占掉下半屏（QVK 默认样式：高 = 屏宽 × 800/2560 = 400px @1280）。
    // 右区域整列（模式卡 + 对话卡 + 日程卡）的最小高度 ≈ 663px，塞不进"键盘以上"的
    // 那点高度，所以打字时这一列只留**对话卡**：
    //   · 模式卡/日程卡先收起来（它们本来也放不下、或正好在键盘底下）
    //   · 对话卡高度**压**到键盘上沿以内 —— 输入行在对话卡底部，于是被顶到键盘上方
    // ⚠ 别用"给布局加下边距"：那会把布局的**最小高度**一起撑大（板端实测：窗口从
    //   800 涨到 1063，输入行反而更往里掉）。上限/隐藏都不影响最小高度。
    if (modeFrame_ != nullptr) {
        // ⚠ 只 setVisible(false) 不够：**隐藏的控件在布局里仍然占着它那块地方**
        //   （板端实测：模式卡藏了，对话卡还是被压在 y=283）。高度上限压到 0 才真让开。
        modeFrame_->setMaximumHeight(0);
        modeFrame_->setVisible(false);
    }
    if (scheduleFrame_ != nullptr) {
        scheduleFrame_->setMaximumHeight(0);
        scheduleFrame_->setVisible(false);
    }
    const int limit = qMax(0, height() - px);               // 键盘上沿（本页坐标）
    // ⚠ top 不能用 chatFrame_->y()：那一刻布局还没重排（模式卡刚藏起来，对话卡还停在
    //   旧位置），算出来的上限会偏小得离谱（实测算成 18 -> 反而把输入行推到键盘下面）。
    //   用**右区域那一列**的顶边（它的几何不随子控件隐藏而变）当基准。
    const QWidget* column = rightBox_ != nullptr ? rightBox_->parentWidget() : nullptr;
    const int columnTop = column != nullptr ? column->mapTo(this, QPoint(0, 0)).y()
                                            : chatFrame_->mapTo(this, QPoint(0, 0)).y();
    chatFrame_->setMaximumHeight(qMax(120, limit - columnTop - 6));
    // ⚠ 光压上限还不够：列里没有别的"能长大"的东西时，QBoxLayout 会把多出来的空间
    //   往两头分（板端实测：对话卡被摆到 y=283 而不是列顶 y=88）。加一根弹簧把多余
    //   空间全部吸到最下面，对话卡就真贴到列顶了。
    if (tailSpacer_ == nullptr && rightBox_ != nullptr) {
        tailSpacer_ = new QSpacerItem(0, 0, QSizePolicy::Minimum, QSizePolicy::Expanding);
        rightBox_->addItem(tailSpacer_);
    }
}

void MainPage::setMainHint(const QString& text, bool warn)
{
    if (mainHint_ == nullptr) {
        return;
    }
    mainHint_->setText(text);
    // 出错（例如壁纸读不到）要看得出来，别用那套"占位灰"
    mainHint_->setStyleSheet(warn ? QStringLiteral("color:%1; background:transparent;").arg(QLatin1String(theme::kWarn))
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
