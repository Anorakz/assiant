// ============================================================================
//  gui/tests/test_schedule_panel.cpp — 日程区控件单测（控件级，跑在 offscreen 上）
//
//  控件本身不解析配置：这里大部分用例是**手搓 ScheduleResult** 直接喂进去，
//  只针对显示规则（两段、区间、变暗、截断、提示行）；最后一条用真的
//  ScheduleModel 跑一份临时配置，证明"模型 → 控件"这条链是通的。
// ============================================================================
#include <QDate>
#include <QDateTime>
#include <QDir>
#include <QFile>
#include <QLabel>
#include <QTemporaryDir>
#include <QTime>
#include <QtTest/QtTest>

#include "core/schedule_model.h"
#include "ui/schedule_panel.h"

using core::ScheduleDay;
using core::ScheduleModel;
using core::ScheduleResult;
using core::ScheduleRow;

namespace {

const QDate kToday(2026, 9, 21);
const QDateTime kNow(kToday, QTime(15, 0));

ScheduleRow makeRow(const QString& time, const QString& title,
                    const QString& end = QString(), bool past = false)
{
    ScheduleRow row;
    row.time = time;
    row.end = end;
    row.title = title;
    row.past = past;
    return row;
}

ScheduleResult makeResult(const QVector<ScheduleRow>& today,
                          const QVector<ScheduleRow>& tomorrow)
{
    ScheduleResult result;
    result.ok = true;
    ScheduleDay first;
    first.label = QStringLiteral("今天");
    first.date = kToday;
    first.rows = today;
    ScheduleDay second;
    second.label = QStringLiteral("明天");
    second.date = kToday.addDays(1);
    second.rows = tomorrow;
    result.days << first << second;
    result.totalInConfig = today.size() + tomorrow.size();
    return result;
}

} // namespace

class TestSchedulePanel : public QObject {
    Q_OBJECT

private slots:
    void emptyScheduleShowsBothSectionsAndNoRows()
    {
        SchedulePanel panel;
        panel.setSchedule(makeResult({}, {}), 6);

        QCOMPARE(panel.rowCount(), 0);
        QCOMPARE(panel.hiddenCount(), 0);
        QCOMPARE(panel.sectionLabels(),
                 QStringList() << QStringLiteral("今天") << QStringLiteral("明天"));
        QCOMPARE(panel.subtitleText(), QStringLiteral("今天没有日程"));
        QVERIFY(panel.noteText().isEmpty());          // 空 ≠ 出错
        QVERIFY(!panel.noteIsWarning());
    }

    void rowsShowTimeAndRange()
    {
        SchedulePanel panel;
        panel.setSchedule(makeResult({makeRow(QStringLiteral("09:30"), QStringLiteral("学习"),
                                              QString(), true),
                                      makeRow(QStringLiteral("14:00"), QStringLiteral("评审"),
                                              QStringLiteral("15:30"), true)},
                                     {makeRow(QStringLiteral("08:00"), QStringLiteral("体检"))}),
                         6);

        const QStringList texts = panel.rowTexts();
        QCOMPARE(texts.size(), 3);
        QCOMPARE(texts.at(0), QStringLiteral("09:30  学习"));
        QCOMPARE(texts.at(1), QStringLiteral("14:00-15:30  评审"));   // 有 end 就是区间
        QCOMPARE(texts.at(2), QStringLiteral("08:00  体检"));
        // 今天两条都已过 -> "下一条"落到明天第一条
        QCOMPARE(panel.subtitleText(), QStringLiteral("今天 · 2 项 · 下一条 明天 08:00"));

        QLabel* title = panel.findChild<QLabel*>(QStringLiteral("ScheduleTime"));
        QVERIFY(title != nullptr);                    // 控件的对象名给 QSS 用
    }

    void pastRowsAreMarked()
    {
        SchedulePanel panel;
        panel.setSchedule(makeResult({makeRow(QStringLiteral("09:30"), QStringLiteral("过了"), QString(), true),
                                      makeRow(QStringLiteral("18:00"), QStringLiteral("没到"), QString(), false)},
                                     {makeRow(QStringLiteral("09:30"), QStringLiteral("明天"), QString(), false)}),
                         6);
        QCOMPARE(panel.isRowPast(0), true);
        QCOMPARE(panel.isRowPast(1), false);
        QCOMPARE(panel.isRowPast(2), false);          // "已过"只作用于今天
        QCOMPARE(panel.isRowPast(99), false);         // 越界不炸
    }

    void subtitleShowsNextUpcomingRow()
    {
        SchedulePanel panel;
        panel.setSchedule(makeResult({makeRow(QStringLiteral("09:00"), QStringLiteral("过了"), QString(), true),
                                      makeRow(QStringLiteral("14:00"), QStringLiteral("也过了"), QString(), true),
                                      makeRow(QStringLiteral("18:00"), QStringLiteral("下一条"), QString(), false)},
                                     {}),
                         6);
        QCOMPARE(panel.subtitleText(), QStringLiteral("今天 · 3 项 · 下一条 18:00"));
    }

    void maxRowsTruncatesAcrossBothSections()
    {
        SchedulePanel panel;
        panel.setSchedule(makeResult({makeRow(QStringLiteral("09:00"), QStringLiteral("A")),
                                      makeRow(QStringLiteral("10:00"), QStringLiteral("B"))},
                                     {makeRow(QStringLiteral("11:00"), QStringLiteral("C")),
                                      makeRow(QStringLiteral("12:00"), QStringLiteral("D")),
                                      makeRow(QStringLiteral("13:00"), QStringLiteral("E"))}),
                         3);

        QCOMPARE(panel.rowCount(), 3);                // 两段合计上限
        QCOMPARE(panel.hiddenCount(), 2);
        QCOMPARE(panel.rowTexts().at(0), QStringLiteral("09:00  A"));
        QCOMPARE(panel.rowTexts().at(2), QStringLiteral("11:00  C"));   // 先今天后明天
        QVERIFY(panel.noteText().contains(QStringLiteral("还有 2 项")));
        QVERIFY(!panel.noteIsWarning());               // 截断不是错误
    }

    void maxRowsOneKeepsTodayFirst()
    {
        SchedulePanel panel;
        panel.setSchedule(makeResult({makeRow(QStringLiteral("09:00"), QStringLiteral("A")),
                                      makeRow(QStringLiteral("10:00"), QStringLiteral("B"))},
                                     {makeRow(QStringLiteral("11:00"), QStringLiteral("C"))}),
                         1);
        QCOMPARE(panel.rowCount(), 1);
        QCOMPARE(panel.hiddenCount(), 2);
        QCOMPARE(panel.rowTexts().at(0), QStringLiteral("09:00  A"));
    }

    void maxRowsZeroMeansNoLimit()
    {
        SchedulePanel panel;
        panel.setSchedule(makeResult({makeRow(QStringLiteral("09:00"), QStringLiteral("A")),
                                      makeRow(QStringLiteral("10:00"), QStringLiteral("B"))},
                                     {makeRow(QStringLiteral("11:00"), QStringLiteral("C"))}),
                         0);
        QCOMPARE(panel.rowCount(), 3);
        QCOMPARE(panel.hiddenCount(), 0);
        QVERIFY(panel.noteText().isEmpty());
    }

    void unreadableConfigShowsWarningNote()
    {
        ScheduleResult broken;
        broken.ok = false;
        broken.error = QStringLiteral("读不到配置文件：/nope/config.yaml");

        SchedulePanel panel;
        panel.setSchedule(broken, 6);

        QCOMPARE(panel.rowCount(), 0);
        QVERIFY(panel.noteIsWarning());
        QVERIFY(panel.noteText().contains(QStringLiteral("读不到配置文件")));
        QCOMPARE(panel.subtitleText(), QStringLiteral("读不到日程配置"));
        QVERIFY(panel.sectionLabels().isEmpty());     // 整份失败时不摆两段空架子
    }

    void badEntriesAreReportedButGoodOnesStay()
    {
        ScheduleResult result = makeResult({makeRow(QStringLiteral("09:00"), QStringLiteral("好的"))}, {});
        result.problems << QStringLiteral("recurring #2: start 时间要写成 HH:MM（得到 99:99）")
                        << QStringLiteral("recurring #3: 缺少 title");

        SchedulePanel panel;
        panel.setSchedule(result, 6);

        QCOMPARE(panel.rowCount(), 1);                // 好条目照常显示
        QVERIFY(panel.noteIsWarning());
        QVERIFY(panel.noteText().contains(QStringLiteral("2 条读不出来")));
    }

    void truncationAndProblemsShareOneNote()
    {
        ScheduleResult result = makeResult({makeRow(QStringLiteral("09:00"), QStringLiteral("A")),
                                            makeRow(QStringLiteral("10:00"), QStringLiteral("B"))},
                                           {});
        result.problems << QStringLiteral("recurring #9: 缺少 title");

        SchedulePanel panel;
        panel.setSchedule(result, 1);

        QVERIFY(panel.noteIsWarning());
        QVERIFY(panel.noteText().contains(QStringLiteral("还有 1 项")));
        QVERIFY(panel.noteText().contains(QStringLiteral("1 条读不出来")));
    }

    void setScheduleTwiceDoesNotAccumulate()
    {
        SchedulePanel panel;
        panel.setSchedule(makeResult({makeRow(QStringLiteral("09:00"), QStringLiteral("A")),
                                      makeRow(QStringLiteral("10:00"), QStringLiteral("B")),
                                      makeRow(QStringLiteral("11:00"), QStringLiteral("C"))},
                                     {}),
                         6);
        QCOMPARE(panel.rowCount(), 3);

        panel.setSchedule(makeResult({makeRow(QStringLiteral("09:00"), QStringLiteral("只剩一条"))}, {}), 6);
        QCOMPARE(panel.rowCount(), 1);
        QCOMPARE(panel.rowTexts().size(), 1);
        QCOMPARE(panel.rowTexts().at(0), QStringLiteral("09:00  只剩一条"));
    }

    void resultWithoutDaysIsSafe()
    {
        ScheduleResult empty;
        empty.ok = true;                              // 成功但 days 是空的（异常输入）

        SchedulePanel panel;
        panel.setSchedule(empty, 6);
        QCOMPARE(panel.rowCount(), 0);
        QCOMPARE(panel.subtitleText(), QStringLiteral("今天没有日程"));
    }

    void longTitleIsElidedButFullTextKept()
    {
        SchedulePanel panel;
        panel.resize(320, 400);
        panel.show();
        const QString longTitle = QStringLiteral("这是一个很长的日程标题需要被省略号截断");
        panel.setSchedule(makeResult({makeRow(QStringLiteral("09:00"), longTitle)}, {}), 6);

        QCOMPARE(panel.rowTexts().at(0), QStringLiteral("09:00  ") + longTitle);   // 全文保留
        const QList<QLabel*> labels = panel.findChildren<QLabel*>(QStringLiteral("ScheduleTitle"));
        QCOMPARE(labels.size(), 1);
        QVERIFY2(!labels.at(0)->text().isEmpty(), "标题不该被清空");
        panel.hide();
    }

    /// 模型 → 控件：用真的 ScheduleModel 跑一份临时配置
    void integrationWithRealModel()
    {
        QTemporaryDir dir;
        QVERIFY(dir.isValid());
        const QString path = dir.filePath(QStringLiteral("config.yaml"));
        QFile file(path);
        QVERIFY(file.open(QIODevice::WriteOnly | QIODevice::Truncate));
        const QByteArray text = QByteArray(R"(scheduler:
  recurring:
    - title: 每天喝水
      start: "10:00"
    - title: 周二站会
      days: [tue]
      start: "09:30"
      end: "09:45"
)");
        QCOMPARE(file.write(text), static_cast<qint64>(text.size()));
        file.close();

        const ScheduleResult result = ScheduleModel::loadFromConfig(path, kNow);
        QVERIFY2(result.ok, qPrintable(result.error));

        SchedulePanel panel;
        panel.setSchedule(result, 6);
        // 今天：每天喝水（1 条）；明天：周二站会 + 每天喝水（2 条）—— 一共 3 条
        QCOMPARE(panel.rowCount(), 3);
        QCOMPARE(panel.rowTexts().at(0), QStringLiteral("10:00  每天喝水"));
        QCOMPARE(panel.rowTexts().at(1), QStringLiteral("09:30-09:45  周二站会"));
        QCOMPARE(panel.rowTexts().at(2), QStringLiteral("10:00  每天喝水"));
        // 今天那条（10:00）已经过了 -> "下一条"是明天第一条（周二站会 09:30）
        QCOMPARE(panel.subtitleText(), QStringLiteral("今天 · 1 项 · 下一条 明天 09:30"));
    }
};

QTEST_MAIN(TestSchedulePanel)
#include "test_schedule_panel.moc"
