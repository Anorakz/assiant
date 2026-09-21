// ============================================================================
//  gui/src/ui/schedule_panel.h — 右区域下段·日程区（方案 S4）
//
//  显示的是 core::ScheduleModel 读出来的结果，本身**不碰文件、不解析 YAML**：
//
//      ┌──────────────────────────────┐
//      日程                            ← AreaTitle
//      今天 · 3 项 · 下一条 14:00       ← AreaHint（副标题）
//      14:00  学习                     ← 行（已过的行变暗）
//      18:00–19:00  晚饭               ← 有 end 时显示区间
//      明天 · 1 项
//      09:30  站会
//      还有 2 项                       ← 被 gui.schedule.max_rows 截掉时的提示
//      ⚠ 配置里有 1 条读不出来          ← 逐条坏日程（琥珀色）
//      └──────────────────────────────┘
//
//  约定
//  ---------------------------------------------------------------------------
//    · 两段的标题**总是**显示；某段没有日程时该段下面显示一行灰字"无"，
//      这样"今天没有"和"整份没读出来"看起来不一样（后者只显示副标题 + 一行错误说明，
//      不摆两段空架子）
//    · `max_rows` 是**两段合计**的上限（先今天后明天），截掉多少会在末尾说清
//    · "已过"只作用于今天段，而且是**纯时间比较** —— 不代表 Agent 一定触发过
//      （Agent 那边还有 window_min / late_grace_min），界面上只是变暗、不写字
// ============================================================================
#pragma once

#include <QString>
#include <QStringList>
#include <QVector>
#include <QWidget>

#include "core/schedule_model.h"

class QLabel;
class QVBoxLayout;

class SchedulePanel : public QWidget {
    Q_OBJECT

public:
    explicit SchedulePanel(QWidget* parent = nullptr);

    /// 应用一份解析结果。
    /// @param result  core::ScheduleModel 的结果（problems / error 都会显示出来）
    /// @param maxRows 两段合计最多渲染几行；<= 0 表示不限制
    void setSchedule(const core::ScheduleResult& result, int maxRows);

    // ---- 供单测 / 验收 ----
    /// 真正渲染出来的行数（不含"还有 N 项"和"无"）
    int rowCount() const { return rows_.size(); }
    /// 被 maxRows 截掉的行数
    int hiddenCount() const { return hiddenCount_; }
    /// 每行 "HH:MM[-HH:MM] 标题"（完整标题，不带省略号）
    QStringList rowTexts() const;
    /// 两段的标题（固定两行：今天 / 明天）
    QStringList sectionLabels() const;
    /// 该行是否显示为"已过"（变暗）
    bool isRowPast(int index) const;
    QString subtitleText() const;
    /// 底部的提示行（空状态 / 坏条目 / 整份失败）；空 = 不显示
    QString noteText() const;
    bool noteIsWarning() const { return noteIsWarning_; }

    QLabel* titleLabel() const { return title_; }
    QLabel* subtitleLabel() const { return subtitle_; }
    QLabel* noteLabel() const { return note_; }

protected:
    /// 标题过长时按宽度加省略号（宽度还没定就先原样显示）
    void resizeEvent(QResizeEvent* event) override;

private:
    struct RowWidgets {
        QWidget* host = nullptr;
        QLabel* time = nullptr;
        QLabel* title = nullptr;
        QString fullText;      ///< "HH:MM[-HH:MM] 标题"（单测/省略号都用它）
        bool past = false;
    };

    void clearRows();
    void addSection(const QString& label, const core::ScheduleDay& day, int* budget);
    void addRow(const core::ScheduleRow& row);
    void elideRows();
    void setNote(const QString& text, bool warning);

    QLabel* title_ = nullptr;
    QLabel* subtitle_ = nullptr;
    QVBoxLayout* sections_ = nullptr;   ///< 两段的容器
    QLabel* note_ = nullptr;

    QVector<RowWidgets> rows_;
    QStringList sectionLabels_;
    QString subtitleText_;      ///< 副标题文本（控件是 subtitle_）
    QString noteText_;
    bool noteIsWarning_ = false;
    int hiddenCount_ = 0;
};
