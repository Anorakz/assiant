// ============================================================================
//  gui/src/ui/settings_page.h — 设置页（T13）
//
//  内容：
//    · Debug 日志开关
//    · 四区域 活动/锁定 + **四个区域共用的**休眠时间（wake.*）
//    · 视频内嵌控制条 活动/锁定 + **它自己单独的**休眠时间（video_overlay.*）
//      —— 按验收要求：控制条的时间**不与区域活动共享**，单独设置
//    · 启动形态（全屏/窗口）、默认页、默认输入类型
//    · 配置路径 / 恢复默认 / 关于（版本、各路径）
//
//  写入目标仍是唯一的 config.yaml：保存后由 MainWindow 重新 applyConfig()，
//  界面效果（四区域折叠策略/休眠时间/输入源）立即生效。
// ============================================================================
#pragma once

#include <QString>
#include <QWidget>

class QCheckBox;
class QComboBox;
class QLabel;
class QPushButton;
class QScrollArea;
class QSpinBox;

class SettingsPage : public QWidget {
    Q_OBJECT

public:
    explicit SettingsPage(QWidget* parent = nullptr);

    /// 从 config.yaml 的 gui: 段载入界面（configPath 为空则只显示不可用）
    void loadFromConfig(const QString& configPath);
    /// 写回 config.yaml 的 gui: 段；返回是否成功
    bool saveToConfig(QString* error = nullptr);
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

signals:
    /// 保存成功后发出（MainWindow 据此重新 applyConfig 让设置立即生效）
    void configSaved();

protected:
    /// 每次显示都把滚动拉回顶部：焦点落在第一个控件上会被 QScrollArea 滚进视野，
    /// 首卡片标题因此被裁（实测）——这是机制层面的修法，不是靠边距遮。
    void showEvent(QShowEvent* event) override;

private:
    void build();

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
    QPushButton* save_ = nullptr;
    QPushButton* defaults_ = nullptr;
    QLabel* about_ = nullptr;
    QLabel* path_ = nullptr;
    QScrollArea* scroll_ = nullptr;
};
