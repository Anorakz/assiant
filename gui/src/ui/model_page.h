// ============================================================================
//  gui/src/ui/model_page.h — 模型测试页（T11）
//
//  三块：
//    1) 推理位置三选：禁用 / 本地(GGUF) / 云端 —— 决定 llm.mode
//    2) 本地块：模型下拉（扫当前模型所在目录的 *.gguf）+ ctx/batch/threads/端口/温度
//       云端块：base / model / api_key
//    3) SigLIP 固定块：**只读**（siglip_full.rknn 是固定的，方案 D2/§7 明确不给开关）
//
//  写入链路：界面 → config.yaml（唯一真源）→ ConfigSyncer 同步到 llm/config/llm.env
//  与 config/config.yaml（T2 已实现，这里复用）。
//  服务控制：直接驱动仓库里现成的 llm/scripts/{start,stop,restart,status}.sh，
//  输出实时显示在下方日志区（不自己另写一套启停逻辑）。
// ============================================================================
#pragma once

#include <QJsonObject>
#include <QString>
#include <QWidget>

class QComboBox;
class QLabel;
class Skeleton;   // T15-16 G-D-3：跑分/扫描的加载骨架（前向声明 ✓）
class QLineEdit;
class QPlainTextEdit;
class QProcess;
class QPushButton;
class QRadioButton;
class QScrollArea;
class QSpinBox;
class QDoubleSpinBox;
class QStackedWidget;
class QTimer;

class ModelPage : public QWidget {
    Q_OBJECT

public:
    explicit ModelPage(QWidget* parent = nullptr);
    ~ModelPage() override;

    /// config.yaml 路径 + 仓库根（用来找 llm/scripts 与 llm.env）
    void setPaths(const QString& configPath, const QString& repoRoot);

    /// 按配置把界面刷成当前配置
    void loadFromConfig();

    /// 保存：把 llm.* 的现值交给 Agent（T14-3；本页**不写文件**，等回执）。
    bool saveAndSync();
    /// Agent 的 `config_result`（MainWindow 转过来）：结果写进日志区。
    void onConfigResult(const QJsonObject& result);
    /// Agent 的 `service_result`（启停 `llm/scripts/*.sh` 的结果）：写进日志区。
    void onServiceResult(const QJsonObject& result);

    // 供单测/验收
    QRadioButton* modeButton(const QString& mode) const;
    QComboBox* modelBox() const { return modelBox_; }
    QSpinBox* ctxSpin() const { return ctxSpin_; }
    QSpinBox* threadsSpin() const { return threadsSpin_; }
    QDoubleSpinBox* temperatureSpin() const { return temperatureSpin_; }
    QLineEdit* cloudBaseEdit() const { return cloudBase_; }
    QLineEdit* cloudKeyEdit() const { return cloudKey_; }
    QWidget* localBlock() const { return localBlock_; }
    QWidget* cloudBlock() const { return cloudBlock_; }
    QLabel* siglipLabel() const { return siglip_; }
    QPushButton* saveButton() const { return save_; }
    QPushButton* startButton() const { return start_; }
    QPushButton* stopButton() const { return stop_; }
    QPushButton* statusButton() const { return status_; }
    QPlainTextEdit* logView() const { return log_; }
    /// T14-7b：整页所在的滚动区（本页内容比 1280×800 的屏幕高，靠它兜住，见 .cpp 顶部注释）
    QScrollArea* scrollArea() const { return scroll_; }
    QString statusText() const;
    QString currentMode() const { return mode_; }

    // ---- T12：基准测试执行器 ----
    QPushButton* qwenPrecheckButton() const { return qwenPrecheck_; }
    QPushButton* qwenFullButton() const { return qwenFull_; }
    QPushButton* multimodalButton() const { return multimodal_; }
    QPushButton* stopBenchButton() const { return stopBench_; }
    QPushButton* reportButton() const { return report_; }
    QLabel* benchBanner() const { return benchBanner_; }
    bool benchRunning() const;
    void startBenchmark(const QString& which);   ///< qwen_precheck | qwen_full | multimodal
    void stopBenchmark();
    /// 找最新的基准报告（<root>/llm/qwen3.5_bench、multimodal_bench 下的 *.md）并显示
    bool showLatestReport();

    Skeleton* skeleton_ = nullptr;   ///< G-D-3：跑分/扫描时的加载条 ✓（判据用 findChild 拿 ✓）

private:
    void build();
    void applyModeToUi(const QString& mode);
    void scanModels();
    /// 请 Agent 跑 `llm/scripts/<script>`（T14-3；本页不再自己 QProcess 跑脚本）
    void runScript(const QString& script);
    void appendLog(const QString& text);

    QString configPath_;
    QString repoRoot_;
    QString mode_ = QStringLiteral("disabled");

    QRadioButton* modeDisabled_ = nullptr;
    QRadioButton* modeLocal_ = nullptr;
    QRadioButton* modeCloud_ = nullptr;

    QWidget* localBlock_ = nullptr;
    QComboBox* modelBox_ = nullptr;
    QSpinBox* ctxSpin_ = nullptr;
    QSpinBox* batchSpin_ = nullptr;
    QSpinBox* threadsSpin_ = nullptr;
    QSpinBox* portSpin_ = nullptr;
    QDoubleSpinBox* temperatureSpin_ = nullptr;

    QWidget* cloudBlock_ = nullptr;
    QLineEdit* cloudBase_ = nullptr;
    QLineEdit* cloudModel_ = nullptr;
    QLineEdit* cloudKey_ = nullptr;

    QLabel* siglip_ = nullptr;
    QPushButton* save_ = nullptr;
    QPushButton* start_ = nullptr;
    QPushButton* stop_ = nullptr;
    QPushButton* status_ = nullptr;
    QPlainTextEdit* log_ = nullptr;
    QLabel* state_ = nullptr;
    /// T14-7b：整页的滚动区（内容比屏幕高时靠它，别让 QStackedWidget 把窗口撑过屏幕）
    QScrollArea* scroll_ = nullptr;

    // T12
    QPushButton* qwenPrecheck_ = nullptr;
    QPushButton* qwenFull_ = nullptr;
    QPushButton* multimodal_ = nullptr;
    QPushButton* stopBench_ = nullptr;
    QPushButton* report_ = nullptr;
    QLabel* benchBanner_ = nullptr;
    QProcess* bench_ = nullptr;
    QTimer* benchTimer_ = nullptr;
    qint64 benchStartedMs_ = 0;

signals:
    /// 请 MainWindow 把 llm.* 交给 Agent（T14-3；本页不写文件）
    void configSaveRequested(QJsonObject keys, QJsonObject credentials);
    /// 请 MainWindow 让 Agent 跑 `llm/scripts/<action>.sh`（start / stop / status）
    void serviceRequested(QString action);
};
