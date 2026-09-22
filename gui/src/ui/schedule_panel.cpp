// ============================================================================
//  gui/src/ui/schedule_panel.cpp — 日程区控件
//
//  纯显示层：数据全部来自 core::ScheduleModel（本文件不解析 YAML、不读文件）。
//  样式靠对象名（AreaTitle / AreaHint）复用主窗口那套 QSS；"已过"的变暗与
//  警告提示色用内联样式 —— 与 music_bar 里"占位说明用琥珀色"是同一套做法。
// ============================================================================
#include "ui/schedule_panel.h"

#include <QFontMetrics>
#include <QHBoxLayout>
#include <QLabel>
#include <QResizeEvent>
#include <QVBoxLayout>

namespace {

/// 琥珀色：与 music_bar / main_hint 的警示色一致
const char* const kWarnColor = "color:#F59E0B; background:transparent;";
/// "已过"的行用 AreaHint 那档灰
const char* const kPastColor = "color:#6F757C; background:transparent;";
/// 时间列宽度：足够放下 "00:00-00:00"
constexpr int kTimeWidth = 92;

QString rowTextOf(const core::ScheduleRow& row)
{
    const QString time = row.end.isEmpty() ? row.time
                                           : QStringLiteral("%1-%2").arg(row.time, row.end);
    return QStringLiteral("%1  %2").arg(time, row.title);
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
        none->setStyleSheet(QLatin1String(kPastColor));
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
    widgets.time->setText(row.end.isEmpty()
                              ? row.time
                              : QStringLiteral("%1-%2").arg(row.time, row.end));
    box->addWidget(widgets.time);

    widgets.title = new QLabel(host);
    widgets.title->setObjectName(QStringLiteral("ScheduleTitle"));
    widgets.title->setText(row.title);
    widgets.title->setToolTip(row.title);
    box->addWidget(widgets.title, 1);

    if (row.past) {
        widgets.time->setStyleSheet(QLatin1String(kPastColor));
        widgets.title->setStyleSheet(QLatin1String(kPastColor));
    }

    sections_->addWidget(host);
    rows_.append(widgets);
}

void SchedulePanel::elideRows()
{
    for (const RowWidgets& row : rows_) {
        if (row.title == nullptr) {
            continue;
        }
        const int width = row.title->width();
        if (width <= 0) {
            row.title->setText(row.title->toolTip());     // 宽度未定：原样
            continue;
        }
        const QFontMetrics metrics(row.title->font());
        row.title->setText(metrics.elidedText(row.title->toolTip(), Qt::ElideRight, width));
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
    note_->setStyleSheet(warning ? QLatin1String(kWarnColor) : QString());
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
