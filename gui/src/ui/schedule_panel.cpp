// ============================================================================
//  gui/src/ui/schedule_panel.cpp — 日程区控件
//
//  纯显示层：数据全部来自 core::ScheduleModel（本文件不解析 YAML、不读文件）。
//  样式靠对象名（AreaTitle / AreaHint）复用主窗口那套 QSS；"已过"的变暗与
//  警告提示色用内联样式 —— 与 music_bar 里"占位说明用琥珀色"是同一套做法。
// ============================================================================
#include "ui/schedule_panel.h"
#include "ui/theme.h"    // T15-16 G-A-1b：视觉常量（唯一来源）

#include <QFontMetrics>
#include <QHBoxLayout>
#include <QLabel>
#include <QResizeEvent>
#include <QVBoxLayout>

namespace {

/// 琥珀色：与 music_bar / main_hint 的警示色一致
const QString kWarnColor =
    QStringLiteral("color:%1; background:transparent;").arg(QLatin1String(theme::kWarn));
/// "已过"的行用 AreaHint 那档灰
const QString kPastColor =
    QStringLiteral("color:%1; background:transparent;").arg(QLatin1String(theme::kTextFaint));
/// 时间列宽度：足够放下 "00:00"
constexpr int kTimeWidth = 92;

QString rowTextOf(const core::ScheduleRow& row)
{
    // T12-4: 一行 = **时间 + 状态**（日程的内容就是那几个状态之一：SLEEP/IDLE/STUDY/GAME）。
    // ⚠ 与 CLI 的 `row_text` 同一格式（`HH:MM  STATE`）—— 这样"CLI 的日程"与
    //   "界面上的日程"可以直接逐行 diff。
    return QStringLiteral("%1  %2").arg(row.time, row.state.toUpper());
}

} // namespace

SchedulePanel::SchedulePanel(QWidget* parent)
    : QWidget(parent)
{
    auto* root = new QVBoxLayout(this);
    root->setContentsMargins(0, 0, 0, 0);
    root->setSpacing(4);

    title_ = new QLabel(QStringLiteral("日程"), this);
    title_->setObjectName(QStringLiteral("AreaTitle"));
    root->addWidget(title_);

    subtitle_ = new QLabel(this);
    subtitle_->setObjectName(QStringLiteral("AreaHint"));
    subtitle_->setWordWrap(true);
    root->addWidget(subtitle_);

    sections_ = new QVBoxLayout();
    sections_->setContentsMargins(0, 0, 0, 0);
    sections_->setSpacing(2);
    root->addLayout(sections_);

    note_ = new QLabel(this);
    note_->setObjectName(QStringLiteral("AreaHint"));
    note_->setWordWrap(true);
    note_->hide();
    root->addWidget(note_);

    root->addStretch(1);
}

void SchedulePanel::setSchedule(const core::ScheduleResult& result, int maxRows)
{
    clearRows();
    sectionLabels_.clear();
    subtitleText_.clear();
    hiddenCount_ = 0;
    setNote(QString(), false);

    if (!result.ok) {
        // 整份读不出来：一行错误说明；两段仍然显示（结构不跳变）
        subtitleText_ = QStringLiteral("读不到日程配置");
        subtitle_->setText(subtitleText_);
        setNote(result.error, true);
        return;
    }

    const int budget = (maxRows > 0) ? maxRows : -1;   // -1 = 不限
    int left = budget;
    for (const core::ScheduleDay& day : result.days) {
        addSection(day.label, day, &left);
    }

    // 截掉的行数：配置里今天+明天一共有多少行，减去渲染出来的
    int available = 0;
    for (const core::ScheduleDay& day : result.days) {
        available += day.rows.size();
    }
    hiddenCount_ = (budget > 0) ? qMax(0, available - rows_.size()) : 0;

    // ---- 副标题：窗口 + 合计几项 + 下一条（R 系列）----
    // 日程区现在显示的是"接下来 N 小时"（窗口），不再是"今天整天 / 明天整天"：
    // 所以副标题先讲清窗口（含终点 —— 第二段标题仍写"明天"，被窗口截断这件事
    // 只在这里说明），再说几项、下一条。
    int total = 0;
    for (const core::ScheduleDay& day : result.days) {
        total += day.rows.size();
    }

    QString next;
    for (const core::ScheduleDay& day : result.days) {
        for (const core::ScheduleRow& row : day.rows) {
            // ⚠ 经过窗口的行不会有 past（窗口只往前看）；这里仍然跳过 past 行是为了
            //    对"手工喂进来的、带已过行的结果"保持老行为（单测就是这么喂的）
            if (row.past) {
                continue;
            }
            next = row.time;
            if (day.label != QStringLiteral("今天")) {
                next = QStringLiteral("%1 %2").arg(day.label, next);
            }
            break;
        }
        if (!next.isEmpty()) {
            break;
        }
    }

    subtitleText_ = QStringLiteral("接下来 %1 小时").arg(result.windowHours);
    if (!result.windowEndText.isEmpty()) {
        subtitleText_ += QStringLiteral(" · 到 %1").arg(result.windowEndText);
    }
    if (total == 0) {
        subtitleText_ += QStringLiteral("里没有日程");
    } else {
        subtitleText_ += QStringLiteral(" · %1 项").arg(total);
        if (!next.isEmpty()) {
            subtitleText_ += QStringLiteral(" · 下一条 %1").arg(next);
        }
    }
    subtitle_->setText(subtitleText_);

    // ---- 尾部提示 ----
    if (hiddenCount_ > 0) {
        setNote(QStringLiteral("还有 %1 项").arg(hiddenCount_), false);
    }
    if (!result.problems.isEmpty()) {
        const QString text = QStringLiteral("配置里有 %1 条读不出来")
                                 .arg(result.problems.size());
        if (noteText_.isEmpty()) {
            setNote(text, true);
        } else {
            setNote(noteText_ + QStringLiteral("；") + text, true);
        }
    }
}

void SchedulePanel::clearRows()
{
    for (const RowWidgets& row : rows_) {
        delete row.host;
    }
    rows_.clear();
    sectionLabels_.clear();
    sectionHeaders_.clear();

    // 段容器里的标题 / "无" 行也一起清掉
    while (QLayoutItem* item = sections_->takeAt(0)) {
        if (QWidget* widget = item->widget()) {
            delete widget;
        }
        delete item;
    }
}

void SchedulePanel::addSection(const QString& label, const core::ScheduleDay& day, int* budget)
{
    sectionLabels_ << label;

    const QString headerText = QStringLiteral("%1 · %2 项").arg(label).arg(day.rows.size());
    sectionHeaders_ << headerText;
    auto* header = new QLabel(headerText, this);
    header->setObjectName(QStringLiteral("AreaHint"));
    sections_->addWidget(header);

    if (day.rows.isEmpty()) {
        auto* none = new QLabel(QStringLiteral("无"), this);
        none->setObjectName(QStringLiteral("AreaHint"));
        none->setStyleSheet(kPastColor);
        sections_->addWidget(none);
        return;
    }

    for (const core::ScheduleRow& row : day.rows) {
        if (*budget == 0) {
            continue;                     // 额度用完：只记行数（hiddenCount_ 在调用方算）
        }
        addRow(row);
        if (*budget > 0) {
            --(*budget);
        }
    }
}

void SchedulePanel::addRow(const core::ScheduleRow& row)
{
    auto* host = new QWidget(this);
    auto* box = new QHBoxLayout(host);
    box->setContentsMargins(0, 0, 0, 0);
    box->setSpacing(6);

    RowWidgets widgets;
    widgets.host = host;
    widgets.past = row.past;
    widgets.fullText = rowTextOf(row);

    widgets.time = new QLabel(host);
    widgets.time->setObjectName(QStringLiteral("ScheduleTime"));
    widgets.time->setMinimumWidth(kTimeWidth);
    widgets.time->setText(row.time);                  // T12-4: 只有时刻（没有时段了）
    box->addWidget(widgets.time);

    // 控件名仍旧叫 ScheduleTitle（QSS `#ScheduleTitle` 那档样式照用），
    // 但它现在显示的是**状态**（日程的内容就是那几个状态之一）。
    const QString state = row.state.toUpper();
    widgets.state = new QLabel(host);
    widgets.state->setObjectName(QStringLiteral("ScheduleTitle"));
    widgets.state->setText(state);
    widgets.state->setToolTip(state);
    box->addWidget(widgets.state, 1);

    if (row.past) {
        widgets.time->setStyleSheet(kPastColor);
        widgets.state->setStyleSheet(kPastColor);
    }

    sections_->addWidget(host);
    rows_.append(widgets);
}

void SchedulePanel::elideRows()
{
    for (const RowWidgets& row : rows_) {
        if (row.state == nullptr) {
            continue;
        }
        const int width = row.state->width();
        if (width <= 0) {
            row.state->setText(row.state->toolTip());     // 宽度未定：原样
            continue;
        }
        const QFontMetrics metrics(row.state->font());
        row.state->setText(metrics.elidedText(row.state->toolTip(), Qt::ElideRight, width));
    }
}

void SchedulePanel::resizeEvent(QResizeEvent* event)
{
    QWidget::resizeEvent(event);
    elideRows();
}

void SchedulePanel::setNote(const QString& text, bool warning)
{
    noteText_ = text;
    noteIsWarning_ = warning;
    note_->setText(text);
    note_->setStyleSheet(warning ? kWarnColor : QString());
    note_->setVisible(!text.isEmpty());
}

QStringList SchedulePanel::rowTexts() const
{
    QStringList out;
    for (const RowWidgets& row : rows_) {
        out << row.fullText;
    }
    return out;
}

QStringList SchedulePanel::sectionLabels() const
{
    return sectionLabels_;
}

QString SchedulePanel::subtitleText() const
{
    return subtitleText_;
}

QString SchedulePanel::noteText() const
{
    return noteText_;
}

bool SchedulePanel::isRowPast(int index) const
{
    if (index < 0 || index >= rows_.size()) {
        return false;
    }
    return rows_.at(index).past;
}
