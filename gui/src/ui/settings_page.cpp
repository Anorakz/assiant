// ============================================================================
//  gui/src/ui/settings_page.cpp — 设置页实现（T13）
// ============================================================================
#include "ui/settings_page.h"

#include "core/config_store.h"

#include <QCheckBox>
#include <QComboBox>
#include <QDebug>
#include <QFormLayout>
#include <QFrame>
#include <QHBoxLayout>
#include <QLabel>
#include <QPushButton>
#include <QScrollArea>
#include <QScrollBar>
#include <QTimer>
#include <QSpinBox>
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

} // namespace

SettingsPage::SettingsPage(QWidget* parent)
    : QWidget(parent)
{
    build();
}

void SettingsPage::build()
{
    // 本页的输入控件直接挂样式：全局表里 QComboBox 的规则在这条控件树上没吃住
    // （实测下拉框仍是系统浅色主题、字看不清），就近设置最稳。
    setStyleSheet(QStringLiteral(
        "QComboBox, QSpinBox {"
        "  background: #232428; border: 1px solid #3A3D42; border-radius: 6px;"
        "  color: #E6E6E6; padding: 4px 8px; font-size: 15px; min-height: 26px; }"
        "QComboBox::drop-down { border: none; width: 22px; }"
        "QComboBox QAbstractItemView {"
        "  background: #232428; color: #E6E6E6; border: 1px solid #3A3D42;"
        "  selection-background-color: #3A3D42; }"
        "QCheckBox { color: #E6E6E6; font-size: 16px; spacing: 8px; }"));

    // 内容比窗口高（三张卡 + 滚动）：整页放进 QScrollArea，避免卡片标题被裁掉
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
    fullscreen_ = new QComboBox(general);
    fullscreen_->addItem(QStringLiteral("全屏 kiosk"), true);
    fullscreen_->addItem(QStringLiteral("窗口"), false);
    generalForm->addRow(QStringLiteral("启动形态"), fullscreen_);
    startPage_ = new QComboBox(general);
    for (const PageEntry& entry : pageEntries()) {
        startPage_->addItem(entry.label, entry.key);
    }
    generalForm->addRow(QStringLiteral("默认页"), startPage_);
    inputSource_ = new QComboBox(general);
    inputSource_->addItem(QStringLiteral("键盘（onboard）"), QStringLiteral("keyboard"));
    inputSource_->addItem(QStringLiteral("PC"), QStringLiteral("pc"));
    inputSource_->addItem(QStringLiteral("命令行"), QStringLiteral("terminal"));
    generalForm->addRow(QStringLiteral("默认输入类型"), inputSource_);
    generalBox->addLayout(generalForm);
    root->addWidget(general);

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
    auto* note = new QLabel(QStringLiteral("与四区域的休眠时间互不影响（分开设置），"
                                           "写进 gui.yaml 的 video_overlay.idle_ms"), overlay);
    note->setObjectName(QStringLiteral("ChatSystem"));
    note->setWordWrap(true);
    overlayBox->addWidget(note);
    overlayBox->addLayout(overlayForm);
    root->addWidget(overlay);

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
        button->setMinimumHeight(40);
        buttons->addWidget(button);
    }
    buttons->addStretch(1);
    cfgBox->addLayout(buttons);
    root->addWidget(cfg);

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
        QString error;
        if (saveToConfig(&error)) {
            qInfo().noquote() << "[settings] 配置已保存";
            emit configSaved();
        } else {
            qWarning().noquote() << "[settings] 保存失败:" << error;
        }
    });
    connect(defaults_, &QPushButton::clicked, this, [this]() { restoreDefaults(); });
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
    debug_->setChecked(store.boolValue(QStringLiteral("debug"), false));
    for (const QString& region : {QStringLiteral("top"), QStringLiteral("bottom"),
                                  QStringLiteral("left"), QStringLiteral("right")}) {
        if (QComboBox* box = regionMode(region)) {
            const QString value = store.value(QStringLiteral("wake.") + region,
                                              QStringLiteral("locked"));
            selectByData(box, value);
        }
    }
    regionIdle_->setValue(store.intValue(QStringLiteral("wake.idle_ms"), 5000));
    selectByData(overlayMode_,
                 store.value(QStringLiteral("video_overlay.mode"), QStringLiteral("active")));
    overlayIdle_->setValue(store.intValue(QStringLiteral("video_overlay.idle_ms"), 3000));
    fullscreen_->setCurrentIndex(store.boolValue(QStringLiteral("fullscreen"), true) ? 0 : 1);
    selectByData(startPage_, store.value(QStringLiteral("start_page"), QStringLiteral("home")));
    selectByData(inputSource_,
                 store.value(QStringLiteral("input_source"), QStringLiteral("keyboard")));
    if (scroll_ != nullptr && scroll_->verticalScrollBar() != nullptr) {
        // 焦点落在第一个控件上会把它"滚进视野"，首个卡片标题就被裁了 → 拉回顶部
        scroll_->verticalScrollBar()->setValue(0);
    }
}

bool SettingsPage::saveToConfig(QString* error)
{
    if (configPath_.isEmpty()) {
        if (error != nullptr) {
            *error = QStringLiteral("没有配置路径");
        }
        return false;
    }
    core::ConfigStore store;
    if (!store.load(configPath_, error)) {
        return false;
    }
    store.setBool(QStringLiteral("debug"), debug_->isChecked());
    for (const QString& region : {QStringLiteral("top"), QStringLiteral("bottom"),
                                  QStringLiteral("left"), QStringLiteral("right")}) {
        if (QComboBox* box = regionMode(region)) {
            store.set(QStringLiteral("wake.") + region, box->currentData().toString());
        }
    }
    store.set(QStringLiteral("wake.idle_ms"), QString::number(regionIdle_->value()));
    store.set(QStringLiteral("video_overlay.mode"), overlayMode_->currentData().toString());
    store.set(QStringLiteral("video_overlay.idle_ms"), QString::number(overlayIdle_->value()));
    store.setBool(QStringLiteral("fullscreen"), fullscreen_->currentIndex() == 0);
    store.set(QStringLiteral("start_page"), startPage_->currentData().toString());
    store.set(QStringLiteral("input_source"), inputSource_->currentData().toString());
    return store.save(error);
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
    fullscreen_->setCurrentIndex(0);
    selectByData(startPage_, QStringLiteral("home"));
    selectByData(inputSource_, QStringLiteral("keyboard"));
    qInfo().noquote() << "[settings] 界面已恢复默认值（尚未保存）";
}
