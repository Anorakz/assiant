// ============================================================================
//  gui/src/ui/settings_page.cpp — 设置页实现（T13 / T13-9）
//
//  三张卡片（T13-9）写的是 config.yaml 里**另外几段**的键：
//    · 学习监督  study.*
//    · 游戏检测  bilibili.game_watch.*（+ B 站凭据走 config/bilibili_cookie.json）
//    · 画像压缩  profile.*
//  这些段在真源里可能整个不存在（板端现在就没有 `study:`），由 ConfigStore 的
//  "缺段按模板新建"补上 —— 页面自己绝不拼 YAML，只给出"键 + 值"。
// ============================================================================
#include "ui/settings_page.h"
#include "ui/state_views.h"    // T15-16 G-D-3：加载骨架 ✓
#include "ui/theme.h"    // T15-16 G-A-1b：视觉常量（唯一来源）

#include "core/config_store.h"
#include "core/cookie_store.h"

#include <QCheckBox>
#include <QComboBox>
#include <QDebug>
#include <QDir>
#include <QDoubleSpinBox>
#include <QFile>
#include <QFileInfo>
#include <QFormLayout>
#include <QFrame>
#include <QHBoxLayout>
#include <QJsonArray>
#include <QJsonObject>
#include <QJsonValue>
#include <QLabel>
#include <QLineEdit>
#include <QListWidget>
#include <QPushButton>
#include <QScrollArea>
#include <QScrollBar>
#include <QSpinBox>
#include <QTimer>
#include <QVBoxLayout>

#include "ui/pages.h"

namespace {

QComboBox* makeModeBox(QWidget* parent)
{
    auto* box = new QComboBox(parent);
    box->addItem(QStringLiteral("活动（空闲后自动隐藏）"), QStringLiteral("active"));
    box->addItem(QStringLiteral("锁定（常显）"), QStringLiteral("locked"));
    return box;
}

void selectByData(QComboBox* box, const QString& data)
{
    const int index = box->findData(data);
    box->setCurrentIndex(index >= 0 ? index : 0);
}

QFrame* makeCard(QWidget* parent, const QString& title, QVBoxLayout** inner)
{
    auto* frame = new QFrame(parent);
    frame->setObjectName(QStringLiteral("AreaFrame"));
    auto* box = new QVBoxLayout(frame);
    box->setContentsMargins(16, 12, 16, 12);
    box->setSpacing(8);
    auto* label = new QLabel(title, frame);
    label->setObjectName(QStringLiteral("AreaTitle"));
    box->addWidget(label);
    *inner = box;
    return frame;
}

/// 卡片里的说明文字（与视频控制条那条提示同一档样式）。
QLabel* makeNote(QWidget* parent, const QString& text)
{
    auto* note = new QLabel(text, parent);
    note->setObjectName(QStringLiteral("ChatSystem"));
    note->setWordWrap(true);
    return note;
}

/// 卡片里的小标题（同一张卡里再分一组时用）。
QLabel* makeSubTitle(QWidget* parent, const QString& text)
{
    auto* label = new QLabel(text, parent);
    label->setObjectName(QStringLiteral("AreaTitle"));
    return label;
}

QSpinBox* makeMinuteSpin(QWidget* parent, int low, int high)
{
    auto* spin = new QSpinBox(parent);
    spin->setRange(low, high);
    spin->setSuffix(QStringLiteral(" 分钟"));
    return spin;
}

} // namespace

SettingsPage::SettingsPage(QWidget* parent)
    : QWidget(parent)
{
    build();
}

void SettingsPage::buildOtaCard(QVBoxLayout* root)
{
    // T15-14-a：系统升级（OTA/槽状态）—— **只展示**。
    // 数据来自 Agent 推的 topic `ota_state`（字段表见 docs/ipc-protocol.md §3）。
    // ⚠ 这里**没有**"开始升级"的按钮：升级是 root 级命令行动作，危险动作不该藏在设置页。
    QVBoxLayout* otaBox = nullptr;
    QFrame* card = makeCard(this, QStringLiteral("系统升级"), &otaBox);
    const auto line = [this, card, otaBox](QLabel** target, const QString& text) {
        *target = new QLabel(card);
        (*target)->setObjectName(QStringLiteral("SysValueSmall"));
        (*target)->setWordWrap(true);
        (*target)->setText(text);
        otaBox->addWidget(*target);
    };
    // T15-16 G-D-3：OTA 状态靠 Agent **推** topic 才来 ✓ ⇒ 这里是**真异步窗口** ✓
    //   （`sys_page` 相反 ✗ —— 构造期同步读完，挂骨架被判据证伪并已回退 ✓）
    otaSkeleton_ = new Skeleton(card);
    otaBox->addWidget(otaSkeleton_);
    otaSkeleton_->start();

    line(&otaSlot_, QStringLiteral("槽状态：还不知道（等 Agent 推 ota_state）"));
    line(&otaMisc_, QStringLiteral("misc 元数据：还不知道"));
    line(&otaLast_, QStringLiteral("最近一次 OTA：还没有记录"));
    line(&otaConfirm_, QStringLiteral("确认结果：还没有记录"));
    root->addWidget(card);
}

void SettingsPage::setOtaState(const QJsonObject& state)
{
    // G-D-3：**收到就停骨架** ✓（不管数据好坏都停 ✓ —— 只在成功路径停的话，
    //   推来坏数据时它会一直脉冲 ✗ ⇒ 白烧唤醒 ✓，见 G-B-4）
    if (otaSkeleton_ != nullptr) {
        otaSkeleton_->stop();
    }
    // ⚠ 不在这里建控件：卡片由 build() -> buildOtaCard() 建好（见头文件里的注解 ✓）。
    if (otaSlot_ == nullptr) {
        return;
    }
    const QString current = state.value(QStringLiteral("current_slot")).toString();
    // ⚠ 不显式写 `QJsonArray` / `QStringList`：那要额外 include（实测编不过 ✗）。
    //   用 `+=` 拼文本、`toArray()` 直接进 range-for，类型都由 Qt 头自己带 ✓。
    QString slotText;
    for (const QJsonValue& item : state.value(QStringLiteral("slots")).toArray()) {
        const QJsonObject row = item.toObject();
        const QString name = row.value(QStringLiteral("name")).toString();
        const QString mark = (!current.isEmpty() && name == current)
                                 ? QStringLiteral("（当前）") : QString();
        slotText += QStringLiteral("槽%1%2：prio %3 · tries %4 · successful %5 · %6\n")
                        .arg(name.isEmpty() ? QStringLiteral("?") : name, mark)
                        .arg(row.value(QStringLiteral("priority")).toInt())
                        .arg(row.value(QStringLiteral("tries_remaining")).toInt())
                        .arg(row.value(QStringLiteral("successful_boot")).toInt())
                        .arg(row.value(QStringLiteral("bootable")).toBool()
                                 ? QStringLiteral("可引导")
                                 : QStringLiteral("**判死**"));
    }
    if (slotText.isEmpty()) {
        slotText = QStringLiteral("槽状态：读不到（Agent 那边 ok=%1 reason=%2）")
                       .arg(state.value(QStringLiteral("ok")).toBool() ? QStringLiteral("true")
                                                                      : QStringLiteral("false"),
                            state.value(QStringLiteral("reason")).toString());
    }
    otaSlot_->setText(slotText.trimmed());

    const bool miscOk = state.value(QStringLiteral("misc_ok")).toBool();
    otaMisc_->setText(miscOk
        ? QStringLiteral("misc 元数据：合法 ✓（A/B 元数据两份副本都在）")
        : QStringLiteral("misc 元数据：**不合法** ✗ %1 —— 串口进 U-Boot 用备份整块写回（runbook §5.2）")
              .arg(state.value(QStringLiteral("misc_reason")).toString()));

    const QJsonObject last = state.value(QStringLiteral("last_ota")).toObject();
    otaLast_->setText(last.isEmpty()
        ? QStringLiteral("最近一次 OTA：还没有记录")
        : QStringLiteral("最近一次 OTA：%1 → 目标槽 %2（%3）")
              .arg(last.value(QStringLiteral("step")).toString(),
                   last.value(QStringLiteral("target_slot")).toString(),
                   last.value(QStringLiteral("at")).toString()));

    const QJsonObject confirm = state.value(QStringLiteral("confirm")).toObject();
    otaConfirm_->setText(confirm.isEmpty()
        ? QStringLiteral("确认结果：还没有记录（判据：Agent 与 GUI 都 active 且 IPC 通）")
        : QStringLiteral("确认结果：%1 · 耗时 %2 s · %3")
              .arg(confirm.value(QStringLiteral("ok")).toBool() ? QStringLiteral("成功 ✓")
                                                                : QStringLiteral("未通过 ✗"))
              .arg(confirm.value(QStringLiteral("elapsed_s")).toDouble())
              .arg(confirm.value(QStringLiteral("at")).toString()));
}

void SettingsPage::build()
{
    // 本页的输入控件直接挂样式：全局表里 QComboBox 的规则在这条控件树上没吃住
    // （实测下拉框仍是系统浅色主题、字看不清），就近设置最稳。
    setStyleSheet(theme::styleSheet(
        "QComboBox, QSpinBox, QDoubleSpinBox, QLineEdit {"
        "  background: @input_bg@; border: 1px solid @divider@; border-radius: 6px;"
        "  color: @text@; padding: 4px 8px; font-size: @font_sm2@px; min-height: 26px; }"
        "QComboBox::drop-down { border: none; width: 22px; }"
        "QComboBox QAbstractItemView {"
        "  background: @input_bg@; color: @text@; border: 1px solid @divider@;"
        "  selection-background-color: @divider@; }"
        "QCheckBox { color: @text@; font-size: @font_md@px; spacing: 8px; }"));

    // 内容比窗口高（多张卡 + 滚动）：整页放进 QScrollArea，避免卡片标题被裁掉
    auto* outer = new QVBoxLayout(this);
    outer->setContentsMargins(0, 0, 0, 0);
    outer->setSpacing(0);
    auto* scroll = new QScrollArea(this);
    scroll_ = scroll;
    scroll->setObjectName(QStringLiteral("SettingsScroll"));
    scroll->setWidgetResizable(true);
    scroll->setFrameShape(QFrame::NoFrame);
    // 滚动条宽度固定（AlwaysOn）：否则内容不足一屏时视口宽度会变，左右边距对不齐
    scroll->setVerticalScrollBarPolicy(Qt::ScrollBarAlwaysOn);
    scroll->setHorizontalScrollBarPolicy(Qt::ScrollBarAlwaysOff);   // 验收：不许左右滑
    auto* content = new QWidget(scroll);
    scroll->setWidget(content);
    outer->addWidget(scroll);

    // 验收：所有卡片**一列**排下来（配合只纵向滚动）
    // 验收：卡片距导航栏与右边缘**距离相等**。
    // 做法：一列铺满 + 左右对称边距；右边的滚动条占宽固定（AlwaysOn），
    // 所以右内边距比左小一个滚动条宽度（14px），视觉上左右距离才真相等。
    auto* root = new QVBoxLayout(content);
    root->setContentsMargins(28, 20, 14, 8);   // 上边距留足：滚动位置万一被带下去也不会裁到标题
    root->setSpacing(10);

    QVBoxLayout* generalBox = nullptr;
    QFrame* general = makeCard(this, QStringLiteral("通用"), &generalBox);
    auto* generalForm = new QFormLayout();
    debug_ = new QCheckBox(QStringLiteral("Debug 日志（打印每条报文与连接细节）"), general);
    // 第一个可聚焦控件会在窗口显示时抢焦点，QScrollArea 随之把它滚进视野 →
    // 首卡片标题被裁。这里让勾选框不吃焦点（它照样能点/能用键盘切换）。
    debug_->setFocusPolicy(Qt::NoFocus);
    generalForm->addRow(debug_);
    // T15-4 任务 9：「启动形态」下拉框已删 —— 它写的 `gui.fullscreen` 从来没有任何
    // 代码读（真正的全屏/窗口是启动参数 `--windowed` 决定的），改它等于改纸面配置。
    startPage_ = new QComboBox(general);
    for (const PageEntry& entry : pageEntries()) {
        startPage_->addItem(entry.label, entry.key);
    }
    generalForm->addRow(QStringLiteral("默认页"), startPage_);
    inputSource_ = new QComboBox(general);
    inputSource_->addItem(QStringLiteral("键盘（onboard）"), QStringLiteral("keyboard"));
    inputSource_->addItem(QStringLiteral("命令行"), QStringLiteral("terminal"));
    // 原第三个选项 "PC"（= 用宿主机键盘当输入源）已随主机输入方向一起移除 (Phase 6 C4)。
    generalForm->addRow(QStringLiteral("默认输入类型"), inputSource_);
    generalBox->addLayout(generalForm);
    root->addWidget(general);

    // T15-17 / T3b：通用卡片里**页面上真有**的那两项也接进"改动即预览" ✓
    //   · `debug_`         → `gui.debug` ✓（`reapplyGuiConfig()` 读它 ✓）
    //   · `inputSource_`   → `gui.input_source` ✓（同上 ✓，并且它还会立刻改输入框的输入类型 ✓）
    // ⚠ `startPage_`（`gui.start_page` ✓）**故意不挂** ✗ —— `reapplyGuiConfig()` **不读**这个键 ✓
    //   （它只在启动时决定默认页 ✓）⇒ 挂了也不会即时生效 ✓，挂上反而会让人以为"改动即预览"覆盖了它 ✗。
    // ⚠ `gui.monitor_interval_ms` 是 GUI 私有项 ✓，页面上**没有**控件 ✗ ⇒ 走"保存回执 + T2"那条路 ✓。
    {
        const auto notifyEdited = [this]() { emit guiSettingsEdited(); };
        if (debug_ != nullptr) {
            connect(debug_, &QCheckBox::toggled, this, notifyEdited);
        }
        if (inputSource_ != nullptr) {
            connect(inputSource_, QOverload<int>::of(&QComboBox::currentIndexChanged), this, notifyEdited);
        }
    }

    QVBoxLayout* wakeBox = nullptr;
    QFrame* wake = makeCard(this, QStringLiteral("四区域 活动/锁定 + 休眠时间"), &wakeBox);
    auto* wakeForm = new QFormLayout();
    top_ = makeModeBox(wake);
    wakeForm->addRow(QStringLiteral("上（顶栏）"), top_);
    bottom_ = makeModeBox(wake);
    wakeForm->addRow(QStringLiteral("下（音乐条/封面）"), bottom_);
    left_ = makeModeBox(wake);
    wakeForm->addRow(QStringLiteral("左（导航）"), left_);
    right_ = makeModeBox(wake);
    wakeForm->addRow(QStringLiteral("右（模式/对话）"), right_);
    regionIdle_ = new QSpinBox(wake);
    regionIdle_->setRange(1000, 600000);
    regionIdle_->setSingleStep(1000);
    regionIdle_->setSuffix(QStringLiteral(" ms"));
    wakeForm->addRow(QStringLiteral("四区域共用休眠时间"), regionIdle_);
    wakeBox->addLayout(wakeForm);
    root->addWidget(wake);

    QVBoxLayout* overlayBox = nullptr;
    QFrame* overlay = makeCard(this, QStringLiteral("视频控制条（内嵌）"), &overlayBox);
    auto* overlayForm = new QFormLayout();
    overlayMode_ = makeModeBox(overlay);
    overlayForm->addRow(QStringLiteral("控制条"), overlayMode_);
    overlayIdle_ = new QSpinBox(overlay);
    overlayIdle_->setRange(500, 600000);
    overlayIdle_->setSingleStep(500);
    overlayIdle_->setSuffix(QStringLiteral(" ms"));
    overlayForm->addRow(QStringLiteral("休眠时间"), overlayIdle_);
    overlayBox->addWidget(makeNote(overlay, QStringLiteral(
        "与四区域的休眠时间互不影响（分开设置），写进 config.yaml 的 gui.video_overlay.idle_ms")));
    overlayBox->addLayout(overlayForm);
    root->addWidget(overlay);

    // T15-17 / T3：**改动即预览** ✓ —— 这六个控件（四区域 4 个 + 四区域休眠 + 控制条模式/休眠 ✓）
    // 对应的键都是 `reapplyGuiConfig()` 会读的 ✓ ⇒ 一改就发信号 ⇒ MainWindow 当场套用 ✓
    // （不必点「保存」✓、更不必重启 ✓）。⚠ **只报值、不落盘** ✗ —— 写盘仍归 Agent ✓（ADR-0005 ✓）。
    {
        const auto notifyEdited = [this]() { emit guiSettingsEdited(); };
        for (QComboBox* box : {top_, bottom_, left_, right_, overlayMode_}) {
            if (box != nullptr) {
                connect(box, QOverload<int>::of(&QComboBox::currentIndexChanged), this, notifyEdited);
            }
        }
        for (QSpinBox* spin : {regionIdle_, overlayIdle_}) {
            if (spin != nullptr) {
                connect(spin, QOverload<int>::of(&QSpinBox::valueChanged), this, notifyEdited);
            }
        }
    }

    // ------------------------------------------------------- 学习监督 (T13-9) ---
    QVBoxLayout* studyBox = nullptr;
    QFrame* study = makeCard(this, QStringLiteral("学习监督"), &studyBox);
    auto* studyForm = new QFormLayout();
    studyEnabled_ = new QCheckBox(QStringLiteral("开启（在 STUDY 模式里判画面是不是学习内容）"), study);
    studyEnabled_->setFocusPolicy(Qt::NoFocus);
    studyForm->addRow(studyEnabled_);
    studyFocus_ = makeMinuteSpin(study, 1, 600);
    studyForm->addRow(QStringLiteral("学习时长（判成学习后多久再看）"), studyFocus_);
    studyRecheck_ = makeMinuteSpin(study, 1, 120);
    studyForm->addRow(QStringLiteral("复查间隔（提醒之后每多久看一次）"), studyRecheck_);
    studyFailures_ = new QSpinBox(study);
    studyFailures_->setRange(1, 20);
    studyFailures_->setSuffix(QStringLiteral(" 次"));
    studyForm->addRow(QStringLiteral("连续不通过几次弹回桌面（不含那次提醒）"), studyFailures_);
    studyCooldown_ = makeMinuteSpin(study, 1, 600);
    studyForm->addRow(QStringLiteral("弹回桌面后的冷却（冷却期只看不动）"), studyCooldown_);
    studyProbe_ = makeMinuteSpin(study, 1, 60);
    studyForm->addRow(QStringLiteral("冷却期里多久看一眼"), studyProbe_);
    studyBand_ = new QDoubleSpinBox(study);
    studyBand_->setRange(0.01, 0.20);
    studyBand_->setSingleStep(0.01);
    studyBand_->setDecimals(2);
    studyForm->addRow(QStringLiteral("起始相对阈值（|相对分| < 它 = 判不出来）"), studyBand_);
    studyBox->addLayout(studyForm);
    studyBox->addWidget(makeNote(study, QStringLiteral(
        "判据是学习锚点库的两个大类原型之差（相对分），不进 LLM；判不出来（unknown）"
        "完全中性：不提醒、不计数、不弹桌面。阈值运行期会自己挪（一次 0.01，界 0.01–0.20，"
        "只在两类各攒够样本后才动），这里写的是起始值；自适应记录在 config/study_stats.json，"
        "锚点在 config/study_anchors.jsonl（都是派生数据，已进 .gitignore）。"
        "气泡原话与两个动作开关（remind / back_to_desktop）用 assistant set study 改。")));
    root->addWidget(study);

    // ------------------------------------------- 游戏检测 + B 站凭据 (T13-9) ---
    QVBoxLayout* gameBox = nullptr;
    QFrame* game = makeCard(this, QStringLiteral("游戏检测"), &gameBox);
    auto* gameForm = new QFormLayout();
    gameEnabled_ = new QCheckBox(QStringLiteral("开启（画面锚点 + 问 PC 进程双路识别）"), game);
    gameEnabled_->setFocusPolicy(Qt::NoFocus);
    gameForm->addRow(gameEnabled_);
    gameInterval_ = new QSpinBox(game);
    gameInterval_->setRange(5, 3600);
    gameInterval_->setSuffix(QStringLiteral(" 秒"));
    gameForm->addRow(QStringLiteral("检测间隔（只在 GAME 模式跑）"), gameInterval_);
    gameScore_ = new QDoubleSpinBox(game);
    gameScore_->setRange(0.50, 1.00);
    gameScore_->setSingleStep(0.01);
    gameScore_->setDecimals(2);
    gameForm->addRow(QStringLiteral("“有把握”的分数门槛"), gameScore_);
    gameBox->addLayout(gameForm);

    // B 站凭据放进这张卡（你定的三张卡片里它是"游戏检测(含 B 站 cookie)"）：
    // 写的**不是 config.yaml**，是 `bilibili.cookie_file` 指的那个 JSON。
    gameBox->addWidget(makeSubTitle(game, QStringLiteral("B 站凭据（清晰度）")));
    auto* cookieForm = new QFormLayout();
    cookiePath_ = new QLineEdit(game);
    cookiePath_->setPlaceholderText(core::CookieStore::defaultRelativePath());
    cookieForm->addRow(QStringLiteral("凭据文件"), cookiePath_);
    sessdata_ = new QLineEdit(game);
    sessdata_->setEchoMode(QLineEdit::Password);
    sessdata_->setPlaceholderText(QStringLiteral("留空 = 不改动"));
    cookieForm->addRow(QStringLiteral("SESSDATA"), sessdata_);
    biliJct_ = new QLineEdit(game);
    biliJct_->setEchoMode(QLineEdit::Password);
    biliJct_->setPlaceholderText(QStringLiteral("留空 = 不改动"));
    cookieForm->addRow(QStringLiteral("bili_jct"), biliJct_);
    dedeUser_ = new QLineEdit(game);
    dedeUser_->setEchoMode(QLineEdit::Password);
    dedeUser_->setPlaceholderText(QStringLiteral("留空 = 不改动"));
    cookieForm->addRow(QStringLiteral("DedeUserID"), dedeUser_);
    gameBox->addLayout(cookieForm);
    cookieStatus_ = new QLabel(game);
    cookieStatus_->setObjectName(QStringLiteral("SysValueSmall"));
    cookieStatus_->setWordWrap(true);
    cookieStatus_->setTextInteractionFlags(Qt::TextSelectableByMouse);
    gameBox->addWidget(cookieStatus_);
    gameBox->addWidget(makeNote(game, QStringLiteral(
        "没有凭据 = 匿名（单文件阶梯，实测 360P~720P 视视频而定）；有 SESSDATA 可以到 1080P。"
        "上面三个框只写你这次填了的键（合并写，空着的不动），填完就清空；"
        "键名照 B 站原样、大小写敏感（SEESSDATA 那种笔误会写不进去）。"
        "凭据文件已进 .gitignore，写之前会在旁边留一份 .bak。")));
    root->addWidget(game);

    // ------------------------------------------------------- 画像压缩 (T13-9) ---
    QVBoxLayout* profileBox = nullptr;
    QFrame* profile = makeCard(this, QStringLiteral("画像压缩"), &profileBox);
    auto* profileForm = new QFormLayout();
    profileEnabled_ = new QCheckBox(QStringLiteral("开启（纯对话攒够就归纳一次）"), profile);
    profileEnabled_->setFocusPolicy(Qt::NoFocus);
    profileForm->addRow(profileEnabled_);
    profileChars_ = new QSpinBox(profile);
    profileChars_->setRange(100, 100000);
    profileChars_->setSingleStep(100);
    profileChars_->setSuffix(QStringLiteral(" 字"));
    profileForm->addRow(QStringLiteral("触发字数（自上次构建以来攒到这么多字）"), profileChars_);
    profileTurns_ = new QSpinBox(profile);
    profileTurns_->setRange(1, 200);
    profileTurns_->setSuffix(QStringLiteral(" 轮"));
    profileForm->addRow(QStringLiteral("触发轮数（短消息太多的兜底）"), profileTurns_);
    profileBox->addLayout(profileForm);
    profileBox->addWidget(makeNote(profile, QStringLiteral(
        "触发者只有 Agent 一个：纯对话攒到字数（或轮数兜底）才构建一次；"
        "用户在对话里说什么都触发不了它，模型也看不到它。结果写 config/user_profile.jsonl。")));
    root->addWidget(profile);

    // ---- T14-9：第四张卡片「网络」（WiFi）--------------------------------------
    //  为什么是卡片而不是首页一整块（你 2026-09-27 定的）：与现有三张卡片一致，
    //  不占首页版面；状态与操作都在卡片里。
    //  ⚠ 这一整张卡片**只发 IPC**：nmcli 由 Agent 调（docs/adr/0005）。
    QVBoxLayout* netBox = nullptr;
    QFrame* net = makeCard(this, QStringLiteral("网络（WiFi）"), &netBox);

    wifiStatus_ = new QLabel(net);
    wifiStatus_->setObjectName(QStringLiteral("SysValueSmall"));
    wifiStatus_->setWordWrap(true);
    netBox->addWidget(wifiStatus_);

    auto* netButtons = new QHBoxLayout();
    netButtons->setSpacing(8);
    wifiScan_ = new QPushButton(QStringLiteral("扫描"), net);
    wifiReconnect_ = new QPushButton(QStringLiteral("重连"), net);
    wifiForget_ = new QPushButton(QStringLiteral("忘记"), net);
    for (QPushButton* button : {wifiScan_, wifiReconnect_, wifiForget_}) {
        button->setObjectName(QStringLiteral("VideoCtl"));
        button->setCursor(Qt::PointingHandCursor);
        button->setMinimumHeight(44);
        netButtons->addWidget(button);
    }
    netButtons->addStretch(1);
    netBox->addLayout(netButtons);

    wifiList_ = new QListWidget(net);
    wifiList_->setObjectName(QStringLiteral("WifiList"));
    wifiList_->setMinimumHeight(150);
    wifiList_->setSelectionMode(QAbstractItemView::SingleSelection);
    netBox->addWidget(wifiList_);

    auto* netForm = new QFormLayout();
    netForm->setLabelAlignment(Qt::AlignLeft);
    wifiPassword_ = new QLineEdit(net);
    wifiPassword_->setEchoMode(QLineEdit::Password);
    wifiPassword_->setPlaceholderText(QStringLiteral("密码（开放网络留空）"));
    wifiAutoconnect_ = new QCheckBox(QStringLiteral("记住并自动连接"), net);
    wifiAutoconnect_->setChecked(true);
    wifiConnect_ = new QPushButton(QStringLiteral("连接"), net);
    wifiConnect_->setObjectName(QStringLiteral("ChatSend"));
    wifiConnect_->setCursor(Qt::PointingHandCursor);
    wifiConnect_->setMinimumHeight(44);
    netForm->addRow(QStringLiteral("密码"), wifiPassword_);
    netForm->addRow(QString(), wifiAutoconnect_);
    netBox->addLayout(netForm);
    netBox->addWidget(wifiConnect_);

    wifiResult_ = new QLabel(net);
    wifiResult_->setObjectName(QStringLiteral("ChatSystem"));
    wifiResult_->setWordWrap(true);
    netBox->addWidget(wifiResult_);
    netBox->addWidget(makeNote(net, QStringLiteral(
        "⚠ 板端只有 wlan0 这一条链路：忘记正在用的那个网络 = 板子马上失联。"
        "密码只发给 Agent（写进 NetworkManager 自己的档案，0600），不进 config.yaml。")));
    root->addWidget(net);

    QVBoxLayout* cfgBox = nullptr;
    QFrame* cfg = makeCard(this, QStringLiteral("配置"), &cfgBox);
    path_ = new QLabel(cfg);
    path_->setObjectName(QStringLiteral("SysValueSmall"));
    path_->setWordWrap(true);
    path_->setTextInteractionFlags(Qt::TextSelectableByMouse);
    cfgBox->addWidget(path_);
    auto* buttons = new QHBoxLayout();
    save_ = new QPushButton(QStringLiteral("保存"), cfg);
    save_->setObjectName(QStringLiteral("VideoCtl"));
    defaults_ = new QPushButton(QStringLiteral("恢复默认"), cfg);
    defaults_->setObjectName(QStringLiteral("VideoCtl"));
    for (QPushButton* button : {save_, defaults_}) {
        button->setCursor(Qt::PointingHandCursor);
        button->setMinimumHeight(44);
        buttons->addWidget(button);
    }
    buttons->addStretch(1);
    cfgBox->addLayout(buttons);
    // T14-3：保存结果 / 失败原话写在这一行；"没连上"时旁边出现「启动 Agent」。
    result_ = new QLabel(cfg);
    result_->setObjectName(QStringLiteral("ChatSystem"));
    result_->setWordWrap(true);
    cfgBox->addWidget(result_);
    startAgent_ = new QPushButton(QStringLiteral("启动 Agent"), cfg);
    startAgent_->setObjectName(QStringLiteral("VideoCtl"));
    startAgent_->setCursor(Qt::PointingHandCursor);
    startAgent_->setMinimumHeight(44);
    startAgent_->setVisible(false);          // 只在需要时出现
    cfgBox->addWidget(startAgent_);
    root->addWidget(cfg);
    buildOtaCard(root);                       // T15-14-a：系统升级卡（见头文件里为什么单独一个方法）
    QVBoxLayout* aboutBox = nullptr;
    QFrame* about = makeCard(this, QStringLiteral("关于"), &aboutBox);
    about_ = new QLabel(about);
    about_->setObjectName(QStringLiteral("SysValueSmall"));
    about_->setWordWrap(true);
    about_->setText(QStringLiteral("板端助手 GUI（Qt5 Widgets）\n"
                                   "运行平台：RK3568 / Ubuntu 20.04 / aarch64\n"
                                   "屏幕 1280×800 DSI · 交互：触摸\n"
                                   "协议：docs/ipc-protocol.md（Unix socket NDJSON）"));
    aboutBox->addWidget(about_);
    root->addStretch(1);

    connect(save_, &QPushButton::clicked, this, [this]() {
        // T14-3：本页**不写文件** —— 把"要改哪些键"交给 MainWindow 发 IPC，
        // 回执到了再由 onConfigResult() 显示结果（docs/adr/0005）。
        for (const QLineEdit* edit : {sessdata_, biliJct_, dedeUser_}) {
            if (edit != nullptr && (edit->text().contains(QLatin1Char('\n'))
                                    || edit->text().contains(QLatin1Char('\r')))) {
                result_->setText(QStringLiteral("凭据不能换行（一行一个键）"));
                startAgent_->setVisible(false);
                return;
            }
        }
        result_->setText(QStringLiteral("已发给 Agent，等回执…"));
        startAgent_->setVisible(false);
        emit saveRequested(buildKeys(), buildCredentials());
    });
    connect(startAgent_, &QPushButton::clicked, this, [this]() {
        result_->setText(QStringLiteral("正在请求启动 Agent…"));
        emit startAgentRequested();
    });
    connect(defaults_, &QPushButton::clicked, this, [this]() { restoreDefaults(); });

    // ---- T14-9：网络卡片的按钮 ------------------------------------------------
    //  每一个都只发 IPC（Agent 去调 nmcli）。回执到了由 onWifiResult() 显示。
    //  密码**只在 connect 那一次进 payload**，不写进任何配置文件。
    const auto askWifi = [this](const QString& action, const QJsonObject& extra) {
        emit wifiRequested(action, extra);
    };
    connect(wifiScan_, &QPushButton::clicked, this, [this, askWifi]() {
        wifiResult_->setText(QStringLiteral("正在扫描…（要几秒）"));
        askWifi(QStringLiteral("scan"), {});
    });
    connect(wifiReconnect_, &QPushButton::clicked, this, [this, askWifi]() {
        wifiResult_->setText(QStringLiteral("正在体检链路…"));
        askWifi(QStringLiteral("reconnect"), {});
    });
    connect(wifiConnect_, &QPushButton::clicked, this, [this, askWifi]() {
        const QString ssid = selectedSsid();
        if (ssid.isEmpty()) {
            wifiResult_->setText(QStringLiteral("先在上面选一个网络"));
            return;
        }
        const bool secured = wifiSecured_.value(ssid, true);
        if (secured && wifiPassword_->text().isEmpty()) {
            wifiResult_->setText(QStringLiteral("%1 需要密码").arg(ssid));
            return;
        }
        QJsonObject payload;
        payload.insert(QStringLiteral("ssid"), ssid);
        if (!wifiPassword_->text().isEmpty()) {
            payload.insert(QStringLiteral("password"), wifiPassword_->text());
        }
        payload.insert(QStringLiteral("autoconnect"), wifiAutoconnect_->isChecked());
        wifiResult_->setText(QStringLiteral("正在连 %1 …").arg(ssid));
        askWifi(QStringLiteral("connect"), payload);
    });
    connect(wifiForget_, &QPushButton::clicked, this, [this, askWifi]() {
        const QString ssid = selectedSsid();
        if (ssid.isEmpty()) {
            wifiResult_->setText(QStringLiteral("先在上面选一个网络"));
            return;
        }
        // ⚠ 板端只有这一条链路：忘记正在用的那个 = 板子失联，必须确认
        const QString active = wifiStatusData_.value(QStringLiteral("ssid")).toString();
        // T15-16 G-C-4：**不用系统模态** ✗ —— kiosk 上它挡住整屏、还不好点 ✓；
        //   改成**两步确认** ✓：第一次点只提示 ✓（**绝不发请求** ✗），再点一次才真发 ✓。
        //   （顺带：`wifiForgetButton` 在非当前网络时本就是**禁用**的 ✓ ⇒ 能点到这里的场合，
        //     恰恰都是"需要确认"的那种 ✓。）
        if (ssid == active && pendingForgetSsid_ != ssid) {
            pendingForgetSsid_ = ssid;
            wifiResult_->setText(
                QStringLiteral("「%1」正在使用中 —— 忘记它之后板子会**立刻失联**。"
                               "再点一次「忘记」确认").arg(ssid));
            return;
        }
        pendingForgetSsid_.clear();     // 第二次点（或换了网络 ✓）：真发 ✓
        QJsonObject payload;
        payload.insert(QStringLiteral("ssid"), ssid);
        askWifi(QStringLiteral("forget"), payload);
    });
    connect(wifiList_, &QListWidget::itemSelectionChanged, this, [this]() {
        pendingForgetSsid_.clear();     // G-C-4：换了选中项 ⇒ 之前的"待确认"作废 ✓
        const QString ssid = selectedSsid();
        const bool secured = wifiSecured_.value(ssid, true);
        wifiPassword_->setEnabled(ssid.isEmpty() ? true : secured);
        wifiPassword_->setPlaceholderText(
            ssid.isEmpty() ? QStringLiteral("密码（先在列表里选一个网络）")
                           : (secured ? QStringLiteral("密码") 
                                      : QStringLiteral("开放网络，不用密码")));
        // 选中的是不是当前连着的那个 → 「忘记」才有意义
        const QString active = wifiStatusData_.value(QStringLiteral("ssid")).toString();
        wifiForget_->setEnabled(!ssid.isEmpty() && ssid == active);
    });
    connect(wifiAutoconnect_, &QCheckBox::toggled, this, [this, askWifi](bool on) {
        const QString ssid = selectedSsid();
        if (ssid.isEmpty()) {
            return;                       // 没选网络时它只是个"连接时要不要记住"的勾
        }
        const QString active = wifiStatusData_.value(QStringLiteral("ssid")).toString();
        if (ssid != active) {
            return;                       // 非当前网络：等点「连接」时一起发
        }
        QJsonObject payload;
        payload.insert(QStringLiteral("ssid"), ssid);
        payload.insert(QStringLiteral("autoconnect"), on);
        askWifi(QStringLiteral("autoconnect"), payload);
    });
}

// ---------------------------------------------------------------------------
//  T14-9：网络卡片
// ---------------------------------------------------------------------------
QString SettingsPage::selectedSsid() const
{
    if (wifiList_ == nullptr) {
        return QString();
    }
    QListWidgetItem* item = wifiList_->currentItem();
    if (item == nullptr || !item->isSelected()) {
        return QString();
    }
    return item->data(Qt::UserRole).toString();
}

void SettingsPage::requestWifiStatus()
{
    if (wifiStatus_ != nullptr) {
        emit wifiRequested(QStringLiteral("status"), QJsonObject());
    }
}

void SettingsPage::requestWifiScan()
{
    if (wifiList_ != nullptr) {
        emit wifiRequested(QStringLiteral("scan"), QJsonObject());
    }
}

void SettingsPage::onWifiResult(const QJsonObject& data)
{
    const QString kind = data.value(QStringLiteral("kind")).toString();

    if (kind == QLatin1String("status")) {
        wifiStatusData_ = data;
        const QString device = data.value(QStringLiteral("device")).toString();
        const QString state = data.value(QStringLiteral("state")).toString();
        const QString ssid = data.value(QStringLiteral("ssid")).toString();
        const int signal = data.value(QStringLiteral("signal")).toInt();
        const QString ip = data.value(QStringLiteral("ip")).toString();
        const QString gateway = data.value(QStringLiteral("gateway")).toString();
        const bool connected = data.value(QStringLiteral("connected")).toBool();
        const bool autoconnect = data.value(QStringLiteral("autoconnect")).toBool();

        QString text;
        if (connected) {
            text = QStringLiteral("%1 · 已连接 %2 · 信号 %3 · IP %4 · 网关 %5 · 自动连接 %6")
                       .arg(device, ssid.isEmpty() ? QStringLiteral("(未知)") : ssid)
                       .arg(signal)
                       .arg(ip.isEmpty() ? QStringLiteral("无") : ip,
                            gateway.isEmpty() ? QStringLiteral("无") : gateway,
                            autoconnect ? QStringLiteral("开") : QStringLiteral("关"));
        } else {
            text = QStringLiteral("%1 · %2 · 没连上（点「扫描」挑一个网络）")
                       .arg(device, state.isEmpty() ? QStringLiteral("未知") : state);
        }
        // 链路体检那一段（Agent 侧 LinkGuard 的真实数字）
        const QJsonObject guard = data.value(QStringLiteral("guard")).toObject();
        if (!guard.isEmpty()) {
            const QJsonObject last = guard.value(QStringLiteral("last")).toObject();
            text += QStringLiteral("\n体检：%1 次；连续失败 %2/%3；已主动重连 %4 次")
                        .arg(guard.value(QStringLiteral("probes")).toInt())
                        .arg(guard.value(QStringLiteral("consecutive_failures")).toInt())
                        .arg(guard.value(QStringLiteral("threshold")).toInt())
                        .arg(guard.value(QStringLiteral("repairs")).toInt());
            if (!last.isEmpty()) {
                text += QStringLiteral("；网关 %1")
                            .arg(last.value(QStringLiteral("gateway_reachable")).toBool()
                                     ? QStringLiteral("可达") : QStringLiteral("不可达"));
            }
        }
        wifiStatus_->setText(text);
        if (wifiAutoconnect_ != nullptr && connected) {
            QSignalBlocker blocker(wifiAutoconnect_);      // 回填不该再触发一次请求
            wifiAutoconnect_->setChecked(autoconnect);
        }
        return;
    }

    if (kind == QLatin1String("scan")) {
        const QJsonArray points = data.value(QStringLiteral("points")).toArray();
        wifiSecured_.clear();
        if (wifiList_ != nullptr) {
            const QString previous = selectedSsid();
            wifiList_->clear();
            for (const QJsonValue& value : points) {
                const QJsonObject point = value.toObject();
                const QString ssid = point.value(QStringLiteral("ssid")).toString();
                if (ssid.isEmpty()) {
                    continue;
                }
                const bool secured = point.value(QStringLiteral("secured")).toBool();
                wifiSecured_.insert(ssid, secured);
                const bool inUse = point.value(QStringLiteral("in_use")).toBool();
                auto* item = new QListWidgetItem(
                    QStringLiteral("%1%2  %3  %4")
                        .arg(inUse ? QStringLiteral("● ") : QStringLiteral("   "), ssid,
                             QStringLiteral("信号 %1").arg(
                                 point.value(QStringLiteral("signal")).toInt()),
                             secured ? QStringLiteral("🔒") : QStringLiteral("开放")));
                item->setData(Qt::UserRole, ssid);
                if (inUse) {
                    QFont font = item->font();
                    font.setBold(true);
                    item->setFont(font);
                }
                wifiList_->addItem(item);
                if (ssid == previous) {
                    wifiList_->setCurrentItem(item);
                }
            }
            if (wifiList_->currentItem() == nullptr && wifiList_->count() > 0) {
                wifiList_->setCurrentRow(0);       // 默认选第一个（已连的排在最前）
            }
        }
        wifiResult_->setText(QStringLiteral("扫到 %1 个网络（同名只留信号最强的那个）")
                                 .arg(data.value(QStringLiteral("count")).toInt()));
        return;
    }

    if (kind == QLatin1String("ack")) {
        const bool ok = data.value(QStringLiteral("ok")).toBool();
        const QString message = data.value(QStringLiteral("message")).toString();
        const QString action = data.value(QStringLiteral("action")).toString();
        wifiResult_->setText(QStringLiteral("%1 %2")
                                 .arg(ok ? QStringLiteral("✔") : QStringLiteral("✘"), message));
        if (ok) {
            wifiPassword_->clear();               // 密码用完就丢，不留在一个可见控件里
            if (action == QLatin1String("connect") || action == QLatin1String("forget")
                || action == QLatin1String("autoconnect")) {
                // 动作会改变状态 → 立刻重新拉一次快照
                QTimer::singleShot(300, this, [this]() { requestWifiStatus(); });
            }
        }
        return;
    }
    qWarning().noquote() << "[settings] 不认识的 wifi 载荷 kind:" << kind;
}

QComboBox* SettingsPage::regionMode(const QString& region) const
{
    if (region == QLatin1String("top")) {
        return top_;
    }
    if (region == QLatin1String("bottom")) {
        return bottom_;
    }
    if (region == QLatin1String("left")) {
        return left_;
    }
    if (region == QLatin1String("right")) {
        return right_;
    }
    return nullptr;
}

void SettingsPage::showEvent(QShowEvent* event)
{
    QWidget::showEvent(event);
    // 根因：窗口显示时焦点落在第一个可聚焦控件上，QScrollArea 会把它滚进视野 →
    // 首卡片标题被裁掉半行。焦点赋值发生在 showEvent **之后**，所以这里连续几次
    // 把滚动位置钉回顶部（0ms 与 300ms 各一次）。
    const auto toTop = [this]() {
        if (scroll_ != nullptr && scroll_->verticalScrollBar() != nullptr) {
            scroll_->verticalScrollBar()->setValue(0);
        }
    };
    toTop();
    QTimer::singleShot(0, this, toTop);
    QTimer::singleShot(300, this, toTop);
    // T14-9：显示时顺手要一次 WiFi 状态（很便宜；卡片打开就该有真实数字）
    requestWifiStatus();
}

QString SettingsPage::repoRootFor(const QString& configPath)
{
    if (configPath.isEmpty()) {
        return QString();
    }
    // 与 main.cpp 的 `setRepoRoot` 同一算法：config/ 的上一级就是仓库根
    const QString root = QFileInfo(configPath).absolutePath() + QStringLiteral("/..");
    return QDir(root).absolutePath();
}

QString SettingsPage::cookieFilePath() const
{
    const QString raw = cookiePath_ != nullptr ? cookiePath_->text() : QString();
    return core::CookieStore::resolvePath(raw, repoRootFor(configPath_));
}

void SettingsPage::refreshCookieStatus()
{
    if (cookieStatus_ == nullptr) {
        return;
    }
    const QString path = cookieFilePath();
    core::CookieStore cookie;
    cookie.load(path);
    QStringList parts;
    for (const QString& key : core::CookieStore::allowedKeys()) {
        const QString value = cookie.value(key);
        parts << QStringLiteral("%1 %2")
                     .arg(key, value.isEmpty() ? QStringLiteral("（无）")
                                               : core::CookieStore::mask(value));
    }
    cookieStatus_->setText(QStringLiteral("文件%1：%2\n当前：%3")
                               .arg(QFile::exists(path) ? QStringLiteral("在") : QStringLiteral("不在（匿名）"),
                                    path, parts.join(QStringLiteral(" · "))));
}

void SettingsPage::loadCards(core::ConfigStore* store)
{
    studyEnabled_->setChecked(store->boolValue(QStringLiteral("study.enabled"), false));
    studyFocus_->setValue(store->intValue(QStringLiteral("study.focus_interval_min"), 30));
    studyRecheck_->setValue(store->intValue(QStringLiteral("study.recheck_interval_min"), 5));
    studyFailures_->setValue(store->intValue(QStringLiteral("study.max_failures"), 3));
    studyCooldown_->setValue(store->intValue(QStringLiteral("study.cooldown_min"), 30));
    studyProbe_->setValue(store->intValue(QStringLiteral("study.cooldown_probe_min"), 1));
    studyBand_->setValue(store->doubleValue(QStringLiteral("study.relative_band"), 0.05));

    gameEnabled_->setChecked(store->boolValue(QStringLiteral("bilibili.game_watch.enabled"), true));
    gameInterval_->setValue(store->intValue(QStringLiteral("bilibili.game_watch.interval_s"), 60));
    gameScore_->setValue(store->doubleValue(QStringLiteral("bilibili.game_watch.confident_score"), 0.82));

    profileEnabled_->setChecked(store->boolValue(QStringLiteral("profile.enabled"), true));
    profileChars_->setValue(store->intValue(QStringLiteral("profile.trigger_chars"), 2000));
    profileTurns_->setValue(store->intValue(QStringLiteral("profile.trigger_turns"), 12));

    const QString raw =
        store->value(QStringLiteral("bilibili.cookie_file"), core::CookieStore::defaultRelativePath());
    cookiePath_->setText(raw.isEmpty() ? core::CookieStore::defaultRelativePath() : raw);
    // 三个凭据框**故意不预填**：里面存的是账号，预填就会在下次保存时被当成"新值"写回去
    // （而且状态标签给的是掩码串，填错了真会把掩码写进凭据文件）。
    sessdata_->clear();
    biliJct_->clear();
    dedeUser_->clear();
    refreshCookieStatus();
}

void SettingsPage::loadFromConfig(const QString& configPath)
{
    configPath_ = configPath;
    if (path_ != nullptr) {
        path_->setText(QStringLiteral("配置（唯一真源）：%1").arg(configPath));
    }
    if (configPath.isEmpty()) {
        return;
    }
    core::ConfigStore store;
    QString error;
    if (!store.load(configPath, &error)) {
        qWarning().noquote() << "[settings] 读配置失败:" << error;
        return;
    }
    debug_->setChecked(store.boolValue(QStringLiteral("gui.debug"), false));
    for (const QString& region : {QStringLiteral("top"), QStringLiteral("bottom"),
                                  QStringLiteral("left"), QStringLiteral("right")}) {
        if (QComboBox* box = regionMode(region)) {
            const QString value = store.value(QStringLiteral("gui.wake.") + region,
                                              QStringLiteral("locked"));
            selectByData(box, value);
        }
    }
    regionIdle_->setValue(store.intValue(QStringLiteral("gui.wake.idle_ms"), 5000));
    selectByData(overlayMode_,
                 store.value(QStringLiteral("gui.video_overlay.mode"), QStringLiteral("active")));
    overlayIdle_->setValue(store.intValue(QStringLiteral("gui.video_overlay.idle_ms"), 3000));
    selectByData(startPage_, store.value(QStringLiteral("gui.start_page"), QStringLiteral("home")));
    selectByData(inputSource_,
                 store.value(QStringLiteral("gui.input_source"), QStringLiteral("keyboard")));
    loadCards(&store);
    if (scroll_ != nullptr && scroll_->verticalScrollBar() != nullptr) {
        // 焦点落在第一个控件上会把它"滚进视野"，首个卡片标题就被裁了 → 拉回顶部
        scroll_->verticalScrollBar()->setValue(0);
    }
}

void SettingsPage::fillCardChanges(core::ConfigStore* store) const
{
    // ⚠ 这一段就是"页面能改哪些键"的**白名单**（T13-9 定、T14-3 改成请求侧契约）。
    //    `tests/test_settings_page.cpp::requestOnlyTouchesWhitelistedKeys` 逐键核对接
    //    下来的 `buildKeys()`：除了 gui.* 与本函数列的这些键，别的一律不许出现在请求里。
    store->setBool(QStringLiteral("study.enabled"), studyEnabled_->isChecked());
    store->set(QStringLiteral("study.focus_interval_min"), QString::number(studyFocus_->value()));
    store->set(QStringLiteral("study.recheck_interval_min"), QString::number(studyRecheck_->value()));
    store->set(QStringLiteral("study.max_failures"), QString::number(studyFailures_->value()));
    store->set(QStringLiteral("study.cooldown_min"), QString::number(studyCooldown_->value()));
    store->set(QStringLiteral("study.cooldown_probe_min"), QString::number(studyProbe_->value()));
    store->set(QStringLiteral("study.relative_band"),
               QString::number(studyBand_->value(), 'f', 2));

    store->setBool(QStringLiteral("bilibili.game_watch.enabled"), gameEnabled_->isChecked());
    store->set(QStringLiteral("bilibili.game_watch.interval_s"),
               QString::number(gameInterval_->value()));
    store->set(QStringLiteral("bilibili.game_watch.confident_score"),
               QString::number(gameScore_->value(), 'f', 2));
    const QString cookiePath = cookiePath_->text().trimmed();
    if (!cookiePath.isEmpty()) {
        store->set(QStringLiteral("bilibili.cookie_file"), cookiePath);
    }

    store->setBool(QStringLiteral("profile.enabled"), profileEnabled_->isChecked());
    store->set(QStringLiteral("profile.trigger_chars"), QString::number(profileChars_->value()));
    store->set(QStringLiteral("profile.trigger_turns"), QString::number(profileTurns_->value()));
}

QJsonObject SettingsPage::buildKeys() const
{
    // 界面上的每一项都过一遍 `set()`（"想改成什么"），再整体交给 Agent。
    // ⚠ 这里**不读也不写文件**：键清单与界面无关，值是控件现值。
    core::ConfigStore store;
    store.setBool(QStringLiteral("gui.debug"), debug_->isChecked());
    for (const QString& region : {QStringLiteral("top"), QStringLiteral("bottom"),
                                  QStringLiteral("left"), QStringLiteral("right")}) {
        if (QComboBox* box = regionMode(region)) {
            store.set(QStringLiteral("gui.wake.") + region, box->currentData().toString());
        }
    }
    store.set(QStringLiteral("gui.wake.idle_ms"), QString::number(regionIdle_->value()));
    store.set(QStringLiteral("gui.video_overlay.mode"), overlayMode_->currentData().toString());
    store.set(QStringLiteral("gui.video_overlay.idle_ms"), QString::number(overlayIdle_->value()));
    store.set(QStringLiteral("gui.start_page"), startPage_->currentData().toString());
    store.set(QStringLiteral("gui.input_source"), inputSource_->currentData().toString());
    fillCardChanges(&store);

    QJsonObject out;
    const QStringList keys = store.pendingKeys();     // 顺序 = 加入顺序（便于日志比对）
    const QHash<QString, QString> values = store.pendingValues();
    for (const QString& key : keys) {
        out.insert(key, values.value(key));
    }
    return out;
}

QJsonObject SettingsPage::buildCredentials() const
{
    QJsonObject out;
    const auto put = [&out](const QLineEdit* edit, const char* key) {
        if (edit == nullptr) {
            return;
        }
        const QString text = edit->text().trimmed();
        if (!text.isEmpty()) {                 // 空框 = 不改动那个键（Agent 侧同口径）
            out.insert(QString::fromLatin1(key), text);
        }
    };
    put(sessdata_, "SESSDATA");
    put(biliJct_, "bili_jct");
    put(dedeUser_, "DedeUserID");
    return out;
}

void SettingsPage::onConfigResult(const QJsonObject& result)
{
    const bool ok = result.value(QStringLiteral("ok")).toBool(false);
    if (ok) {
        const int changed = result.value(QStringLiteral("changed")).toInt(0);
        const QJsonObject env = result.value(QStringLiteral("llm_env")).toObject();
        QString text = changed > 0
                           ? QStringLiteral("Agent 已写入 config.yaml（改了 %1 行，旁留 .bak）")
                                 .arg(changed)
                           : QStringLiteral("Agent 说：没有要改的行（值本来就一样）");
        if (!env.isEmpty() && !env.value(QStringLiteral("ok")).toBool(true)) {
            text += QStringLiteral("；但派生 llm.env 失败：%1")
                        .arg(env.value(QStringLiteral("error")).toString());
        }
        result_->setText(text);
        startAgent_->setVisible(false);
        qInfo().noquote() << "[settings]" << text;
        // 填过的凭据框清掉 + 状态标签刷新（凭据是 Agent 写的，重新读一遍才有新掩码）
        sessdata_->clear();
        biliJct_->clear();
        dedeUser_->clear();
        if (!configPath_.isEmpty()) {
            loadFromConfig(configPath_);       // 回读：界面以真源为准
        }
        return;
    }

    const QString error = result.value(QStringLiteral("error")).toString();
    result_->setText(QStringLiteral("没能写入：%1").arg(error.isEmpty()
                                                       ? QStringLiteral("（Agent 没给原因）")
                                                       : error));
    qWarning().noquote() << "[settings] 保存失败:" << error;
    // "没连上"时把「启动 Agent」露出来（T14-7 的 systemd 单元就位后真能起）
    const bool offline = error.contains(QStringLiteral("没连上"))
                         || error.contains(QStringLiteral("未连接"))
                         || error.contains(QStringLiteral("连不上"));
    startAgent_->setVisible(offline);
}

void SettingsPage::restoreDefaults()
{
    // 只把界面改回默认值（不动文件，等用户点保存）——避免"点错了直接改掉生产配置"
    debug_->setChecked(false);
    for (const QString& region : {QStringLiteral("top"), QStringLiteral("bottom"),
                                  QStringLiteral("left"), QStringLiteral("right")}) {
        if (QComboBox* box = regionMode(region)) {
            selectByData(box, region == QLatin1String("top") || region == QLatin1String("right")
                                 ? QStringLiteral("locked")
                                 : QStringLiteral("active"));
        }
    }
    regionIdle_->setValue(5000);
    selectByData(overlayMode_, QStringLiteral("active"));
    overlayIdle_->setValue(3000);
    selectByData(startPage_, QStringLiteral("home"));
    selectByData(inputSource_, QStringLiteral("keyboard"));

    // 三张卡片的默认值（与 config.example.yaml / Agent 的默认值同一口径）
    studyEnabled_->setChecked(false);
    studyFocus_->setValue(30);
    studyRecheck_->setValue(5);
    studyFailures_->setValue(3);
    studyCooldown_->setValue(30);
    studyProbe_->setValue(1);
    studyBand_->setValue(0.05);
    gameEnabled_->setChecked(true);
    gameInterval_->setValue(60);
    gameScore_->setValue(0.82);
    profileEnabled_->setChecked(true);
    profileChars_->setValue(2000);
    profileTurns_->setValue(12);
    cookiePath_->setText(core::CookieStore::defaultRelativePath());
    sessdata_->clear();
    biliJct_->clear();
    dedeUser_->clear();
    refreshCookieStatus();
    qInfo().noquote() << "[settings] 界面已恢复默认值（尚未保存）";
}
