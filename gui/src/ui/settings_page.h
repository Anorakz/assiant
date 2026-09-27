// ============================================================================
//  gui/src/ui/settings_page.h — 设置页（T13 / T13-9）
//
//  内容：
//    · Debug 日志开关
//    · 四区域 活动/锁定 + **四个区域共用的**休眠时间（wake.*）
//    · 视频内嵌控制条 活动/锁定 + **它自己单独的**休眠时间（video_overlay.*）
//      —— 按验收要求：控制条的时间**不与区域活动共享**，单独设置
//    · 启动形态（全屏/窗口）、默认页、默认输入类型
//    · **学习监督**（study.*：开关 + 时间参数 + 起始相对阈值）
//    · **游戏检测**（bilibili.game_watch.*：开关/间隔/有把握分数）+ **B 站凭据**
//      （bilibili.cookie_file + 写进凭据文件的那三个键）
//    · **画像压缩**（profile.*：开关 + 触发字数 / 触发轮数）
//    · 配置路径 / 恢复默认 / 关于（版本、各路径）
//
//  写入目标仍是唯一的 config.yaml：保存后由 MainWindow 重新 applyConfig()，
//  界面效果（四区域折叠策略/休眠时间/输入源）立即生效。
//
//  ⚠ T13-9 的两条边界
//  ---------------------------------------------------------------------------
//    · 页面只写**白名单里的键**：`gui.*`（原有）+ 三张卡片那几个键（见
//      `saveToConfig` 里逐条列出的键）。别段的注释与顺序一个字节都不许动 ——
//      `tests/test_settings_page.cpp::saveOnlyTouchesWhitelistedKeys` 拿仓库里那份
//      `config.example.yaml` 当真源逐行钉住这件事。
//    · 三张卡片的段/键在真源里**可能整个不存在**（板端现在就没有 `study:` 段）：
//      由 `core::ConfigStore::loadTemplate()` 按 `config.example.yaml` 新建
//      （与 `assistant set` 同一套约定），页面自己不拼 YAML。
// ============================================================================
#pragma once

#include <QJsonObject>
#include <QString>
#include <QWidget>

class QCheckBox;
class QComboBox;
class QDoubleSpinBox;
class QLabel;
class QLineEdit;
class QPushButton;
class QScrollArea;
class QSpinBox;

namespace core {
class ConfigStore;   ///< 只在私有方法签名里用到，所以前向声明（不必把 core 头拉进来）
} // namespace core

class SettingsPage : public QWidget {
    Q_OBJECT

public:
    explicit SettingsPage(QWidget* parent = nullptr);

    /// 从 config.yaml 载入界面（configPath 为空则只显示不可用）
    void loadFromConfig(const QString& configPath);
    /// **把"想改成什么"交给 Agent**（T14-3）：本页只发请求，不写任何文件。
    /// @return 请求内容 —— `{点号路径: 新值}`；调用方（MainWindow）负责发 IPC。
    QJsonObject buildKeys() const;
    /// 凭据三个框里**真填了**的键（空框 = 不改动那个键）。
    QJsonObject buildCredentials() const;
    /// Agent 的回执（MainWindow 收到 `config_result` 后调它）。
    /// ok=true -> 提示"已写入"并重新读一遍配置；false -> 把 Agent 的原话显示出来。
    void onConfigResult(const QJsonObject& result);

    /// 把界面恢复成本页的默认值（不写文件，等用户点保存）
    void restoreDefaults();

    // 供单测/验收
    QCheckBox* debugCheck() const { return debug_; }
    QComboBox* regionMode(const QString& region) const;
    QSpinBox* regionIdleSpin() const { return regionIdle_; }
    QComboBox* overlayMode() const { return overlayMode_; }
    QSpinBox* overlayIdleSpin() const { return overlayIdle_; }
    QComboBox* fullscreenBox() const { return fullscreen_; }
    QComboBox* startPageBox() const { return startPage_; }
    QComboBox* inputSourceBox() const { return inputSource_; }
    QPushButton* saveButton() const { return save_; }
    QPushButton* defaultsButton() const { return defaults_; }
    QLabel* aboutLabel() const { return about_; }
    QScrollArea* scrollArea() const { return scroll_; }
    QLabel* pathLabel() const { return path_; }
    /// 保存结果那行字（T14-3：成功/失败都写在这里；失败时里面是 Agent 的原话）
    QLabel* resultLabel() const { return result_; }
    /// 「启动 Agent」按钮（只在"没连上"时显示；T14-7 的 systemd 单元就位后真能起）
    QPushButton* startAgentButton() const { return startAgent_; }

    // ---- T13-9：三张卡片 ----
    QCheckBox* studyEnabledCheck() const { return studyEnabled_; }
    QSpinBox* studyFocusSpin() const { return studyFocus_; }
    QSpinBox* studyRecheckSpin() const { return studyRecheck_; }
    QSpinBox* studyFailuresSpin() const { return studyFailures_; }
    QSpinBox* studyCooldownSpin() const { return studyCooldown_; }
    QSpinBox* studyProbeSpin() const { return studyProbe_; }
    QDoubleSpinBox* studyBandSpin() const { return studyBand_; }
    QCheckBox* gameEnabledCheck() const { return gameEnabled_; }
    QSpinBox* gameIntervalSpin() const { return gameInterval_; }
    QDoubleSpinBox* gameScoreSpin() const { return gameScore_; }
    QLineEdit* cookiePathEdit() const { return cookiePath_; }
    QLineEdit* sessdataEdit() const { return sessdata_; }
    QLineEdit* biliJctEdit() const { return biliJct_; }
    QLineEdit* dedeUserEdit() const { return dedeUser_; }
    QLabel* cookieStatusLabel() const { return cookieStatus_; }
    QCheckBox* profileEnabledCheck() const { return profileEnabled_; }
    QSpinBox* profileCharsSpin() const { return profileChars_; }
    QSpinBox* profileTurnsSpin() const { return profileTurns_; }

signals:
    /// 用户点了「保存」：把"要改哪些键 / 哪些凭据"交给 MainWindow 发 IPC（T14-3）。
    /// ⚠ 本页**不写文件**：落盘由 Agent 做（docs/adr/0005）。
    void saveRequested(QJsonObject keys, QJsonObject credentials);
    /// 用户点了「启动 Agent」（只在"没连上"时出现）。MainWindow 去 systemctl start。
    void startAgentRequested();

protected:
    /// 每次显示都把滚动拉回顶部：焦点落在第一个控件上会被 QScrollArea 滚进视野，
    /// 首卡片标题因此被裁（实测）——这是机制层面的修法，不是靠边距遮。
    void showEvent(QShowEvent* event) override;

private:
    void build();

    /// 载入 / 写出三张卡片（`study.*` / `bilibili.game_watch.*` + 凭据 / `profile.*`）。
    void loadCards(core::ConfigStore* store);
    /// 把三张卡片的当前值塞进 `store` 的待改列表（**不落盘**；就是发给 Agent 的 keys）。
    void fillCardChanges(core::ConfigStore* store) const;
    /// 仓库根 = `config/` 的上一级（相对 cookie_file 按它解析，与 Agent 一致）。
    static QString repoRootFor(const QString& configPath);
    /// 凭据文件的绝对路径（页面上的路径框 + 仓库根）。
    QString cookieFilePath() const;
    /// 把凭据文件里已存的三个键刷到界面/状态标签上。
    void refreshCookieStatus();

    QString configPath_;
    QCheckBox* debug_ = nullptr;
    QComboBox* top_ = nullptr;
    QComboBox* bottom_ = nullptr;
    QComboBox* left_ = nullptr;
    QComboBox* right_ = nullptr;
    QSpinBox* regionIdle_ = nullptr;
    QComboBox* overlayMode_ = nullptr;
    QSpinBox* overlayIdle_ = nullptr;
    QComboBox* fullscreen_ = nullptr;
    QComboBox* startPage_ = nullptr;
    QComboBox* inputSource_ = nullptr;

    // ---- 学习监督 ----
    QCheckBox* studyEnabled_ = nullptr;
    QSpinBox* studyFocus_ = nullptr;
    QSpinBox* studyRecheck_ = nullptr;
    QSpinBox* studyFailures_ = nullptr;
    QSpinBox* studyCooldown_ = nullptr;
    QSpinBox* studyProbe_ = nullptr;
    QDoubleSpinBox* studyBand_ = nullptr;

    // ---- 游戏检测 + B 站凭据 ----
    QCheckBox* gameEnabled_ = nullptr;
    QSpinBox* gameInterval_ = nullptr;
    QDoubleSpinBox* gameScore_ = nullptr;
    QLineEdit* cookiePath_ = nullptr;
    QLineEdit* sessdata_ = nullptr;
    QLineEdit* biliJct_ = nullptr;
    QLineEdit* dedeUser_ = nullptr;
    QLabel* cookieStatus_ = nullptr;

    // ---- 画像压缩 ----
    QCheckBox* profileEnabled_ = nullptr;
    QSpinBox* profileChars_ = nullptr;
    QSpinBox* profileTurns_ = nullptr;

    QPushButton* save_ = nullptr;
    QPushButton* defaults_ = nullptr;
    QLabel* about_ = nullptr;
    QLabel* path_ = nullptr;
    QLabel* result_ = nullptr;        ///< 保存结果/失败原话（T14-3）
    QPushButton* startAgent_ = nullptr;   ///< 只在"没连上 Agent"时出现
    QScrollArea* scroll_ = nullptr;
};
