// ============================================================================
//  gui/src/ui/model_page.cpp — 模型测试页实现（T11）
// ============================================================================
#include "ui/model_page.h"

#include "core/config_store.h"
// ⚠ 这里原来还有一条 `#include "core/config_sync.h"` —— T14-3 删掉了 config_sync.*，
//    却漏了这行 include。板端 `cmake --build` 因此直接失败在
//    `fatal error: core/config_sync.h: No such file or directory`，
//    而旧的可执行文件与旧测试二进制都还在，于是"看起来一切正常"
//    （ctest 还能跑出 22/22，里面混着已经退役的 test_config_sync）。
//    T14-7b 起有 tests/test_gui_includes.py 专门守这类"悬空 include"。

#include <QComboBox>
#include <QDebug>
#include <QScrollArea>
#include <QDoubleSpinBox>
#include <QFileInfo>
#include <QFormLayout>
#include <QFrame>
#include <QHBoxLayout>
#include <QLabel>
#include <QLineEdit>
#include <QPlainTextEdit>
#include <QProcess>
#include <QPushButton>
#include <QRadioButton>
#include <QSpinBox>
#include <QVBoxLayout>
#include <QDateTime>
#include <QDir>
#include <QDirIterator>
#include <QTimer>

namespace {

/// GUI 与 Agent 共用 config.yaml 之后，mode 只有一种规范词汇：
/// edge（板端 llama.cpp）/ cloud / disabled。
/// 旧配置里可能还写着 local / board（那是 Agent 也认的 edge 别名），这里统一归一，
/// 这样界面、文件、Agent 三处看到的是同一个值。
QString canonicalMode(const QString& mode)
{
    if (mode == QLatin1String("edge") || mode == QLatin1String("local")
        || mode == QLatin1String("board")) {
        return QStringLiteral("edge");
    }
    if (mode == QLatin1String("cloud")) {
        return QStringLiteral("cloud");
    }
    return QStringLiteral("disabled");
}

QFrame* makeBlock(QWidget* parent, const QString& title, QVBoxLayout** innerOut)
{
    auto* frame = new QFrame(parent);
    frame->setObjectName(QStringLiteral("AreaFrame"));
    auto* box = new QVBoxLayout(frame);
    box->setContentsMargins(16, 12, 16, 12);
    box->setSpacing(8);
    auto* label = new QLabel(title, frame);
    label->setObjectName(QStringLiteral("AreaTitle"));
    box->addWidget(label);
    if (innerOut != nullptr) {
        *innerOut = box;
    }
    return frame;
}

} // namespace

ModelPage::ModelPage(QWidget* parent)
    : QWidget(parent)
{
    build();
}

ModelPage::~ModelPage()
{
    // ⚠ 退出时必须先收掉子进程：否则 QProcess 带着运行中的进程一起被析构，
    //   信号（readyRead/finished）打到正在销毁的对象上 → 实测会段错误（T12 取证时崩过一次）。
    const auto stopProcess = [](QProcess* process) {
        if (process == nullptr || process->state() == QProcess::NotRunning) {
            return;
        }
        // 先断开所有连接（readyRead/finished 的 lambda 捕获了 this，
        // 析构过程中被调用会打到半销毁对象上）→ 再收进程
        process->disconnect();
        process->blockSignals(true);
        process->terminate();
        if (!process->waitForFinished(2000)) {
            process->kill();
            process->waitForFinished(1000);
        }
    };
    stopProcess(bench_);
    // ⚠ T14-3 起起停脚本由 Agent 跑（本页不再持有 script_ 那个 QProcess）
}

void ModelPage::build()
{
    // ---- T14-7b：整页放进 QScrollArea（与设置页同一套路）----
    //  为什么必须这么做：`QStackedWidget` 的最小尺寸是**所有页**（含当前隐藏的页）的
    //  最大值，而本页内容要 811px；1280×800 的面板只给页面 72..800 这 728px。
    //  于是窗口被撑到 883 —— 全屏请求照样发出去了，但底部 83px 永远在屏幕外
    //  （板端 `xprop WM_NORMAL_HINTS` 原文：`program specified minimum size: 935 by 883`）。
    //  放进滚动区之后本页最小尺寸降到 ~68px，"内容比一屏高"由滚动条兜住，
    //  不再由隐藏页决定整个窗口的高度。
    auto* outer = new QVBoxLayout(this);
    outer->setContentsMargins(0, 0, 0, 0);
    outer->setSpacing(0);
    scroll_ = new QScrollArea(this);
    scroll_->setObjectName(QStringLiteral("ModelScroll"));
    scroll_->setWidgetResizable(true);
    scroll_->setFrameShape(QFrame::NoFrame);
    scroll_->setVerticalScrollBarPolicy(Qt::ScrollBarAsNeeded);
    scroll_->setHorizontalScrollBarPolicy(Qt::ScrollBarAlwaysOff);
    auto* content = new QWidget(scroll_);
    scroll_->setWidget(content);
    outer->addWidget(scroll_);

    // 下面所有卡片都挂这个布局：它的宿主是滚动区里的 content（不再是本页自己）
    auto* root = new QVBoxLayout(content);
    root->setContentsMargins(0, 0, 0, 0);
    root->setSpacing(10);

    // ---------------- 顶部：推理位置三选 ----------------
    QVBoxLayout* modeBox = nullptr;
    QFrame* modeFrame = makeBlock(this, QStringLiteral("推理位置"), &modeBox);
    auto* modeRow = new QHBoxLayout();
    modeRow->setSpacing(18);
    modeDisabled_ = new QRadioButton(QStringLiteral("禁用"), modeFrame);
    modeLocal_ = new QRadioButton(QStringLiteral("本地（GGUF / llama.cpp）"), modeFrame);
    modeCloud_ = new QRadioButton(QStringLiteral("云端"), modeFrame);
    for (QRadioButton* button : {modeDisabled_, modeLocal_, modeCloud_}) {
        button->setObjectName(QStringLiteral("ModeRadio"));
        modeRow->addWidget(button);
    }
    modeRow->addStretch(1);
    modeBox->addLayout(modeRow);
    state_ = new QLabel(modeFrame);
    state_->setObjectName(QStringLiteral("ChatSystem"));
    modeBox->addWidget(state_);
    root->addWidget(modeFrame);

    const auto wireMode = [this](QRadioButton* button, const QString& mode) {
        connect(button, &QRadioButton::toggled, this, [this, mode](bool on) {
            if (on) {
                applyModeToUi(mode);   // 内部会 mode_ = canonicalMode(mode)
            }
        });
    };
    wireMode(modeDisabled_, QStringLiteral("disabled"));
    // GUI 直接用 Agent 的规范值 edge（旧配置里的 local/board 由 canonicalMode 归一）
    wireMode(modeLocal_, QStringLiteral("edge"));
    wireMode(modeCloud_, QStringLiteral("cloud"));

    // ---------------- 本地块 ----------------
    QVBoxLayout* localBox = nullptr;
    localBlock_ = makeBlock(this, QStringLiteral("本地模型 / llama.cpp 参数"), &localBox);
    auto* localForm = new QFormLayout();
    localForm->setLabelAlignment(Qt::AlignLeft);
    modelBox_ = new QComboBox(localBlock_);
    modelBox_->setObjectName(QStringLiteral("ModelCombo"));
    modelBox_->setEditable(true);
    modelBox_->setMinimumWidth(420);
    localForm->addRow(QStringLiteral("模型文件"), modelBox_);
    ctxSpin_ = new QSpinBox(localBlock_);
    ctxSpin_->setRange(128, 32768);
    localForm->addRow(QStringLiteral("上下文 ctx"), ctxSpin_);
    batchSpin_ = new QSpinBox(localBlock_);
    batchSpin_->setRange(16, 4096);
    localForm->addRow(QStringLiteral("批 batch"), batchSpin_);
    threadsSpin_ = new QSpinBox(localBlock_);
    threadsSpin_->setRange(1, 8);
    localForm->addRow(QStringLiteral("线程"), threadsSpin_);
    portSpin_ = new QSpinBox(localBlock_);
    portSpin_->setRange(1024, 65535);
    localForm->addRow(QStringLiteral("端口"), portSpin_);
    temperatureSpin_ = new QDoubleSpinBox(localBlock_);
    temperatureSpin_->setRange(0.0, 2.0);
    temperatureSpin_->setSingleStep(0.1);
    localForm->addRow(QStringLiteral("温度"), temperatureSpin_);
    localBox->addLayout(localForm);
    root->addWidget(localBlock_);

    // ---------------- 云端块 ----------------
    QVBoxLayout* cloudBox = nullptr;
    cloudBlock_ = makeBlock(this, QStringLiteral("云端参数"), &cloudBox);
    auto* cloudForm = new QFormLayout();
    cloudBase_ = new QLineEdit(cloudBlock_);
    cloudBase_->setMinimumWidth(420);
    cloudForm->addRow(QStringLiteral("api_base"), cloudBase_);
    cloudModel_ = new QLineEdit(cloudBlock_);
    cloudForm->addRow(QStringLiteral("model"), cloudModel_);
    cloudKey_ = new QLineEdit(cloudBlock_);
    cloudKey_->setEchoMode(QLineEdit::Password);
    cloudForm->addRow(QStringLiteral("api_key"), cloudKey_);
    cloudBox->addLayout(cloudForm);
    root->addWidget(cloudBlock_);

    // ---------------- SigLIP 固定块（只读）----------------
    QVBoxLayout* siglipBox = nullptr;
    QFrame* siglipFrame = makeBlock(this, QStringLiteral("SigLIP 视觉（固定）"), &siglipBox);
    siglip_ = new QLabel(siglipFrame);
    siglip_->setObjectName(QStringLiteral("SysValueSmall"));
    siglip_->setWordWrap(true);
    siglip_->setText(QStringLiteral("固定使用 siglip_full.rknn（板端 NPU 推理）—— 本页不提供任何开关，\n"
                                    "改动需直接改仓库文件（方案 D2 已定案）。"));
    siglipBox->addWidget(siglip_);
    root->addWidget(siglipFrame);

    // ---------------- T12：基准测试执行器 ----------------
    QVBoxLayout* benchBox = nullptr;
    QFrame* benchFrame = makeBlock(this, QStringLiteral("基准测试（跑仓库现用脚本）"), &benchBox);
    benchBanner_ = new QLabel(benchFrame);
    benchBanner_->setObjectName(QStringLiteral("BenchBanner"));
    benchBanner_->setWordWrap(true);
    benchBanner_->setVisible(false);
    benchBox->addWidget(benchBanner_);

    auto* benchRow = new QHBoxLayout();
    benchRow->setSpacing(10);
    qwenPrecheck_ = new QPushButton(QStringLiteral("Qwen 预检（快）"), benchFrame);
    qwenFull_ = new QPushButton(QStringLiteral("Qwen 全量基准"), benchFrame);
    multimodal_ = new QPushButton(QStringLiteral("多模态基准"), benchFrame);
    stopBench_ = new QPushButton(QStringLiteral("停止测试"), benchFrame);
    report_ = new QPushButton(QStringLiteral("查看最新报告"), benchFrame);
    for (QPushButton* button : {qwenPrecheck_, qwenFull_, multimodal_, stopBench_, report_}) {
        button->setObjectName(QStringLiteral("VideoCtl"));
        button->setCursor(Qt::PointingHandCursor);
        button->setMinimumHeight(40);
        benchRow->addWidget(button);
    }
    benchRow->addStretch(1);
    benchBox->addLayout(benchRow);
    root->addWidget(benchFrame);

    connect(qwenPrecheck_, &QPushButton::clicked, this,
            [this]() { startBenchmark(QStringLiteral("qwen_precheck")); });
    connect(qwenFull_, &QPushButton::clicked, this,
            [this]() { startBenchmark(QStringLiteral("qwen_full")); });
    connect(multimodal_, &QPushButton::clicked, this,
            [this]() { startBenchmark(QStringLiteral("multimodal")); });
    connect(stopBench_, &QPushButton::clicked, this, [this]() { stopBenchmark(); });
    connect(report_, &QPushButton::clicked, this, [this]() { showLatestReport(); });
    stopBench_->setEnabled(false);

    benchTimer_ = new QTimer(this);
    benchTimer_->setInterval(1000);
    connect(benchTimer_, &QTimer::timeout, this, [this]() {
        if (!benchRunning()) {
            return;
        }
        const qint64 seconds = (QDateTime::currentMSecsSinceEpoch() - benchStartedMs_) / 1000;
        benchBanner_->setText(QStringLiteral(
            "基准测试运行中 %1:%2 —— llama-server 被测试脚本接管，"
            "「启动/停止服务」暂不可用").arg(seconds / 60, 2, 10, QLatin1Char('0'))
                .arg(seconds % 60, 2, 10, QLatin1Char('0')));
    });

    // ---------------- 操作行 + 日志 ----------------
    auto* actions = new QHBoxLayout();
    actions->setSpacing(10);
    save_ = new QPushButton(QStringLiteral("保存并同步配置"), this);
    save_->setObjectName(QStringLiteral("VideoCtl"));
    start_ = new QPushButton(QStringLiteral("启动服务"), this);
    start_->setObjectName(QStringLiteral("VideoCtl"));
    stop_ = new QPushButton(QStringLiteral("停止服务"), this);
    stop_->setObjectName(QStringLiteral("VideoCtl"));
    status_ = new QPushButton(QStringLiteral("查看状态"), this);
    status_->setObjectName(QStringLiteral("VideoCtl"));
    for (QPushButton* button : {save_, start_, stop_, status_}) {
        button->setCursor(Qt::PointingHandCursor);
        button->setMinimumHeight(40);
        actions->addWidget(button);
    }
    actions->addStretch(1);
    root->addLayout(actions);

    log_ = new QPlainTextEdit(this);
    log_->setObjectName(QStringLiteral("ModelLog"));
    log_->setReadOnly(true);
    log_->setMinimumHeight(120);
    root->addWidget(log_, 1);

    connect(save_, &QPushButton::clicked, this, [this]() { saveAndSync(); });
    connect(start_, &QPushButton::clicked, this,
            [this]() { runScript(QStringLiteral("start.sh")); });
    connect(stop_, &QPushButton::clicked, this,
            [this]() { runScript(QStringLiteral("stop.sh")); });
    connect(status_, &QPushButton::clicked, this,
            [this]() { runScript(QStringLiteral("status.sh")); });

    applyModeToUi(QStringLiteral("disabled"));
}

QRadioButton* ModelPage::modeButton(const QString& mode) const
{
    const QString canonical = canonicalMode(mode);
    if (canonical == QLatin1String("edge")) {
        return modeLocal_;
    }
    if (canonical == QLatin1String("cloud")) {
        return modeCloud_;
    }
    return modeDisabled_;
}

void ModelPage::applyModeToUi(const QString& mode)
{
    mode_ = canonicalMode(mode);
    const bool isLocal = (mode_ == QLatin1String("edge"));
    const bool isCloud = (mode_ == QLatin1String("cloud"));
    // 禁用时两块都不显示（参数还在配置里，只是不生效）
    localBlock_->setVisible(isLocal);
    cloudBlock_->setVisible(isCloud);
    if (state_ != nullptr) {
        state_->setText(isLocal ? QStringLiteral("本地推理：由 llm/scripts/start.sh 拉起 llama-server")
                                : (isCloud ? QStringLiteral("云端推理：api_key 会写进 config/config.yaml")
                                           : QStringLiteral("已禁用：Agent 不做本地/云端推理")));
    }
}

void ModelPage::setPaths(const QString& configPath, const QString& repoRoot)
{
    configPath_ = configPath;
    repoRoot_ = repoRoot;
    scanModels();
    loadFromConfig();
}

void ModelPage::scanModels()
{
    if (modelBox_ == nullptr) {
        return;
    }
    const QString current = modelBox_->currentText();
    modelBox_->clear();

    QStringList dirs;
    if (!configPath_.isEmpty()) {
        // 从配置里读当前模型路径，扫它所在目录
        core::ConfigStore store;
        QString error;
        if (store.load(configPath_, &error)) {
            const QString path = store.value(QStringLiteral("llm.model_path"));
            if (!path.isEmpty()) {
                const QString dir = QFileInfo(path).absolutePath();
                if (!dirs.contains(dir)) {
                    dirs << dir;
                }
            }
        }
    }
    if (!repoRoot_.isEmpty()) {
        dirs << (repoRoot_ + QStringLiteral("/llm/models"));
    }
    int found = 0;
    for (const QString& dir : dirs) {
        QDir d(dir);
        const QStringList files = d.entryList({QStringLiteral("*.gguf")}, QDir::Files, QDir::Name);
        for (const QString& file : files) {
            modelBox_->addItem(d.filePath(file));
            ++found;
        }
    }
    if (!current.isEmpty() && modelBox_->findText(current) < 0) {
        modelBox_->addItem(current);
    }
    qInfo().noquote() << QStringLiteral("[model] 扫到 %1 个 GGUF（目录：%2）")
                             .arg(found)
                             .arg(dirs.join(QStringLiteral(", ")));
}

void ModelPage::loadFromConfig()
{
    if (configPath_.isEmpty()) {
        return;
    }
    core::ConfigStore store;
    QString error;
    if (!store.load(configPath_, &error)) {
        appendLog(QStringLiteral("读配置失败：%1").arg(error));
        return;
    }
    mode_ = canonicalMode(store.value(QStringLiteral("llm.mode"), QStringLiteral("disabled")));
    if (QRadioButton* button = modeButton(mode_)) {
        button->setChecked(true);
    }
    applyModeToUi(mode_);

    const QString model = store.value(QStringLiteral("llm.model_path"));
    if (!model.isEmpty() && modelBox_->findText(model) < 0) {
        modelBox_->addItem(model);
    }
    modelBox_->setCurrentText(model);
    ctxSpin_->setValue(store.intValue(QStringLiteral("llm.ctx_size"), 2048));
    batchSpin_->setValue(store.intValue(QStringLiteral("llm.batch_size"), 256));
    threadsSpin_->setValue(store.intValue(QStringLiteral("llm.threads"), 4));
    portSpin_->setValue(store.intValue(QStringLiteral("llm.port"), 9000));
    temperatureSpin_->setValue(store.value(QStringLiteral("llm.temperature")).toDouble());
    cloudBase_->setText(store.value(QStringLiteral("llm.api_base")));
    cloudModel_->setText(store.value(QStringLiteral("llm.model")));
    cloudKey_->setText(store.value(QStringLiteral("llm.api_key")));
    appendLog(QStringLiteral("已载入配置：mode=%1").arg(mode_));
}

bool ModelPage::saveAndSync()
{
    if (configPath_.isEmpty()) {
        appendLog(QStringLiteral("没设置 config.yaml 路径，无法保存"));
        return false;
    }
    // T14-3：本页**不写文件**。把 llm.* 的现值交给 MainWindow 发 IPC（`set_config`），
    // 落盘 + 派生 llm.env 都由 Agent 做（`docs/adr/0005`），回执到了再写日志。
    core::ConfigStore store;
    store.set(QStringLiteral("llm.mode"), mode_);
    store.set(QStringLiteral("llm.model_path"), modelBox_->currentText());
    store.set(QStringLiteral("llm.ctx_size"), QString::number(ctxSpin_->value()));
    store.set(QStringLiteral("llm.batch_size"), QString::number(batchSpin_->value()));
    store.set(QStringLiteral("llm.threads"), QString::number(threadsSpin_->value()));
    store.set(QStringLiteral("llm.port"), QString::number(portSpin_->value()));
    store.set(QStringLiteral("llm.temperature"),
              QString::number(temperatureSpin_->value(), 'g', 4));
    store.set(QStringLiteral("llm.api_base"), cloudBase_->text());
    store.set(QStringLiteral("llm.model"), cloudModel_->text());
    store.set(QStringLiteral("llm.api_key"), cloudKey_->text());

    QJsonObject keys;
    const QHash<QString, QString> values = store.pendingValues();
    for (const QString& key : store.pendingKeys()) {
        keys.insert(key, values.value(key));
    }
    appendLog(QStringLiteral("已把 llm.* 交给 Agent（等回执；llm.env 也由它派生）"));
    emit configSaveRequested(keys, QJsonObject());
    return true;
}

void ModelPage::onConfigResult(const QJsonObject& result)
{
    if (result.value(QStringLiteral("ok")).toBool(false)) {
        const int changed = result.value(QStringLiteral("changed")).toInt(0);
        const QJsonObject env = result.value(QStringLiteral("llm_env")).toObject();
        appendLog(QStringLiteral("Agent 已写入 config.yaml（改了 %1 行，mode=%2）")
                      .arg(changed)
                      .arg(mode_));
        if (!env.isEmpty()) {
            if (env.value(QStringLiteral("ok")).toBool(true)) {
                appendLog(QStringLiteral("已由 Agent 派生 llm.env（改了 %1 行）")
                              .arg(env.value(QStringLiteral("changed")).toInt(0)));
            } else {
                appendLog(QStringLiteral("派生 llm.env 失败：%1")
                              .arg(env.value(QStringLiteral("error")).toString()));
            }
        }
        return;
    }
    appendLog(QStringLiteral("保存失败：%1")
                  .arg(result.value(QStringLiteral("error")).toString()));
}

void ModelPage::runScript(const QString& script)
{
    // T14-3：起停脚本改由 **Agent** 跑（GUI 不做系统动作，见 docs/adr/0005）。
    // ⚠ 只认这三个名字 —— 它们是 `llm/scripts/` 下的三个入口。
    const QString action = script.section(QLatin1Char('.'), 0, 0);   // start.sh -> start
    if (action != QLatin1String("start") && action != QLatin1String("stop")
        && action != QLatin1String("status")) {
        appendLog(QStringLiteral("不认识的脚本：%1（只支持 start/stop/status）").arg(script));
        return;
    }
    appendLog(QStringLiteral("--- 请 Agent 跑 %1").arg(script));
    emit serviceRequested(action);
}

void ModelPage::onServiceResult(const QJsonObject& result)
{
    const QString action = result.value(QStringLiteral("action")).toString();
    const bool ok = result.value(QStringLiteral("ok")).toBool(false);
    const QString message = result.value(QStringLiteral("message")).toString();
    appendLog(QStringLiteral("[llm/scripts/%1.sh] %2%3")
                  .arg(action, ok ? QStringLiteral("成功：") : QStringLiteral("失败："),
                       message));
}

void ModelPage::appendLog(const QString& text)
{
    if (log_ != nullptr && !text.isEmpty()) {
        log_->appendPlainText(text);
    }
}


bool ModelPage::benchRunning() const
{
    return bench_ != nullptr && bench_->state() != QProcess::NotRunning;
}

void ModelPage::startBenchmark(const QString& which)
{
    if (repoRoot_.isEmpty()) {
        appendLog(QStringLiteral("没设置仓库根，无法跑基准测试"));
        return;
    }
    if (benchRunning()) {
        appendLog(QStringLiteral("已有基准测试在跑，先「停止测试」"));
        return;
    }
    QString script;
    QStringList args;
    if (which == QLatin1String("qwen_precheck")) {
        script = repoRoot_ + QStringLiteral("/llm/bench_qwen35.py");
        args << QStringLiteral("--precheck");
    } else if (which == QLatin1String("qwen_full")) {
        script = repoRoot_ + QStringLiteral("/llm/bench_qwen35.py");
    } else {
        script = repoRoot_ + QStringLiteral("/llm/bench_multimodal.py");
    }
    if (!QFileInfo::exists(script)) {
        appendLog(QStringLiteral("脚本不存在：%1").arg(script));
        return;
    }

    if (bench_ == nullptr) {
        bench_ = new QProcess(this);
        bench_->setProcessChannelMode(QProcess::MergedChannels);
        connect(bench_, &QProcess::readyReadStandardOutput, this, [this]() {
            appendLog(QString::fromLocal8Bit(bench_->readAllStandardOutput()).trimmed());
        });
        connect(bench_, QOverload<int, QProcess::ExitStatus>::of(&QProcess::finished), this,
                [this](int code, QProcess::ExitStatus) {
                    appendLog(QStringLiteral("=== 基准测试结束，退出码 %1 ===").arg(code));
                    benchTimer_->stop();
                    benchBanner_->setVisible(false);
                    stopBench_->setEnabled(false);
                    for (QPushButton* button : {qwenPrecheck_, qwenFull_, multimodal_, start_,
                                                stop_, status_}) {
                        button->setEnabled(true);
                    }
                });
    }
    benchStartedMs_ = QDateTime::currentMSecsSinceEpoch();
    benchBanner_->setVisible(true);
    benchBanner_->setText(QStringLiteral("基准测试运行中 —— llama-server 被测试脚本接管"));
    stopBench_->setEnabled(true);
    // 服务控制与其它基准按钮在测试期间禁用（llama-server 被接管，别去抢端口）
    for (QPushButton* button : {qwenPrecheck_, qwenFull_, multimodal_, start_, stop_, status_}) {
        button->setEnabled(false);
    }
    benchTimer_->start();
    appendLog(QStringLiteral("=== 开始 %1：%2 %3").arg(which, script, args.join(QLatin1Char(' '))));
    bench_->setWorkingDirectory(repoRoot_);
    bench_->start(QStringLiteral("python3"), QStringList{script} + args);
    qInfo().noquote() << "[model] 启动基准测试:" << which << script;
}

void ModelPage::stopBenchmark()
{
    if (!benchRunning()) {
        appendLog(QStringLiteral("当前没有在跑的基准测试"));
        return;
    }
    appendLog(QStringLiteral("停止测试（终止子进程）…"));
    bench_->terminate();
    if (!bench_->waitForFinished(3000)) {
        bench_->kill();
        bench_->waitForFinished(2000);
    }
    qInfo().noquote() << "[model] 已停止基准测试";
}

bool ModelPage::showLatestReport()
{
    if (repoRoot_.isEmpty()) {
        appendLog(QStringLiteral("没设置仓库根，找不到报告"));
        return false;
    }
    // 报告落在 llm/qwen3.5_bench 与 llm/multimodal_bench 下（*.md），取最新那个
    QString newest;
    qint64 newestMs = -1;
    for (const QString& dir : {repoRoot_ + QStringLiteral("/llm/qwen3.5_bench"),
                               repoRoot_ + QStringLiteral("/llm/multimodal_bench")}) {
        QDir d(dir);
        const QFileInfoList files =
            d.entryInfoList({QStringLiteral("*.md")}, QDir::Files, QDir::Time);
        if (!files.isEmpty() && files.first().lastModified().toMSecsSinceEpoch() > newestMs) {
            newestMs = files.first().lastModified().toMSecsSinceEpoch();
            newest = files.first().absoluteFilePath();
        }
    }
    if (newest.isEmpty()) {
        appendLog(QStringLiteral("还没找到基准报告（先跑一次测试）"));
        return false;
    }
    QFile file(newest);
    if (!file.open(QIODevice::ReadOnly)) {
        appendLog(QStringLiteral("报告打不开：%1").arg(newest));
        return false;
    }
    const QStringList lines = QString::fromUtf8(file.readAll()).split(QLatin1Char('\n'));
    const int show = qMin(40, lines.size());
    appendLog(QStringLiteral("=== 最新报告 %1（前 %2 行 / 共 %3 行）===")
                  .arg(newest)
                  .arg(show)
                  .arg(lines.size()));
    for (int i = 0; i < show; ++i) {
        appendLog(lines.at(i));
    }
    return true;
}

QString ModelPage::statusText() const
{
    return state_ != nullptr ? state_->text() : QString();
}
