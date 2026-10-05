// ============================================================================
//  gui/tests/test_schedule_panel.cpp — 日程区控件单测（控件级，跑在 offscreen 上）
//
//  控件本身不解析配置：这里大部分用例是**手搓 ScheduleResult** 直接喂进去，
//  只针对显示规则（两段、变暗、截断、提示行）；最后一条用真的
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

ScheduleRow makeRow(const QString& time, const QString& state, bool past = false)
{
    ScheduleRow row;
    row.time = time;
    row.state = state;
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
    /// T15-16 G-B-3：**输入指纹没变就不重建控件** ✓（改前每 60 s 一次全量 delete+new ✗）
    void identicalInputDoesNotRebuildRows()
    {
        SchedulePanel panel;
        const ScheduleResult result =
            makeResult({makeRow(QStringLiteral("09:00"), QStringLiteral("study")),
                        makeRow(QStringLiteral("19:00"), QStringLiteral("game"))},
                       {makeRow(QStringLiteral("09:30"), QStringLiteral("sleep"))});

        panel.setSchedule(result, 6);
        const int buildsAfterFirst = panel.rowRebuilds();
        QCOMPARE(buildsAfterFirst, 3);          // 三行 ⇒ 建三次 ✓（顺便自证计数口径 ✓）
        const QStringList texts = panel.rowTexts();
        const QString subtitle = panel.subtitleText();
        const int hidden = panel.hiddenCount();

        panel.setSchedule(result, 6);           // 同一份输入 ⇒ 一行都不该重建 ✓
        QCOMPARE(panel.rowRebuilds(), buildsAfterFirst);
        QCOMPARE(panel.refreshSkips(), 1);
        QCOMPARE(panel.rowTexts(), texts);      // 跳过 ≠ 界面错 ✓
        QCOMPARE(panel.subtitleText(), subtitle);
        QCOMPARE(panel.hiddenCount(), hidden);
    }

    /// 指纹一变就必须重建 ✓（否则界面停在旧数据上 ✗）—— 用**最细的一处变化**试：`past` 翻转
    void changedInputRebuildsRows()
    {
        SchedulePanel panel;
        panel.setSchedule(
            makeResult({makeRow(QStringLiteral("09:00"), QStringLiteral("study"))}, {}), 6);
        const int before = panel.rowRebuilds();

        panel.setSchedule(
            makeResult({makeRow(QStringLiteral("09:00"), QStringLiteral("study"), true)}, {}), 6);
        QVERIFY2(panel.rowRebuilds() > before,
                 "past 变了却没重建 —— 指纹漏字段了 ✗");
        QCOMPARE(panel.refreshSkips(), 0);
    }

    void emptyScheduleShowsBothSectionsAndNoRows()
    {
        SchedulePanel panel;
        panel.setSchedule(makeResult({}, {}), 6);

        QCOMPARE(panel.rowCount(), 0);
        QCOMPARE(panel.hiddenCount(), 0);
        QCOMPARE(panel.sectionLabels(),
                 QStringList() << QStringLiteral("今天") << QStringLiteral("明天"));
        QCOMPARE(panel.subtitleText(), QStringLiteral("接下来 24 小时里没有日程"));
        QVERIFY(panel.noteText().isEmpty());          // 空 ≠ 出错
        QVERIFY(!panel.noteIsWarning());
    }

    void rowsShowTimeAndState()
    {
        SchedulePanel panel;
        panel.setSchedule(makeResult({makeRow(QStringLiteral("09:30"), QStringLiteral("study"), true),
                                      makeRow(QStringLiteral("14:00"), QStringLiteral("game"), true)},
                                     {makeRow(QStringLiteral("08:00"), QStringLiteral("idle"))}),
                         6);

        const QStringList texts = panel.rowTexts();
        QCOMPARE(texts.size(), 3);
        // T12-4: 一行就是 "时刻  状态"（大写），没有标题、也没有时段
        QCOMPARE(texts.at(0), QStringLiteral("09:30  STUDY"));
        QCOMPARE(texts.at(1), QStringLiteral("14:00  GAME"));
        QCOMPARE(texts.at(2), QStringLiteral("08:00  IDLE"));
        // 今天两条都已过 -> "下一条"落到明天第一条（手工喂的结果没有窗口终点，
        // 所以副标题里没有"到 …"那一段）
        QCOMPARE(panel.subtitleText(),
                 QStringLiteral("接下来 24 小时 · 3 项 · 下一条 明天 08:00"));

        QLabel* time = panel.findChild<QLabel*>(QStringLiteral("ScheduleTime"));
        QVERIFY(time != nullptr);                     // 控件的对象名给 QSS 用
        QCOMPARE(time->text(), QStringLiteral("09:30"));   // 时刻那格只有时刻
        QLabel* state = panel.findChild<QLabel*>(QStringLiteral("ScheduleTitle"));
        QVERIFY(state != nullptr);
        QCOMPARE(state->text(), QStringLiteral("STUDY"));  // 名字没改，内容是状态
    }

    void pastRowsAreMarked()
    {
        SchedulePanel panel;
        panel.setSchedule(makeResult({makeRow(QStringLiteral("09:30"), QStringLiteral("sleep"), true),
                                      makeRow(QStringLiteral("18:00"), QStringLiteral("study"), false)},
                                     {makeRow(QStringLiteral("09:30"), QStringLiteral("game"), false)}),
                         6);
        QCOMPARE(panel.isRowPast(0), true);
        QCOMPARE(panel.isRowPast(1), false);
        QCOMPARE(panel.isRowPast(2), false);          // "已过"只作用于今天
        QCOMPARE(panel.isRowPast(99), false);         // 越界不炸
    }

    void subtitleShowsNextUpcomingRow()
    {
        SchedulePanel panel;
        panel.setSchedule(makeResult({makeRow(QStringLiteral("09:00"), QStringLiteral("sleep"), true),
                                      makeRow(QStringLiteral("14:00"), QStringLiteral("idle"), true),
                                      makeRow(QStringLiteral("18:00"), QStringLiteral("study"), false)},
                                     {}),
                         6);
        QCOMPARE(panel.subtitleText(),
                 QStringLiteral("接下来 24 小时 · 3 项 · 下一条 18:00"));
    }

    void maxRowsTruncatesAcrossBothSections()
    {
        SchedulePanel panel;
        panel.setSchedule(makeResult({makeRow(QStringLiteral("09:00"), QStringLiteral("sleep")),
                                      makeRow(QStringLiteral("10:00"), QStringLiteral("idle"))},
                                     {makeRow(QStringLiteral("11:00"), QStringLiteral("study")),
                                      makeRow(QStringLiteral("12:00"), QStringLiteral("game")),
                                      makeRow(QStringLiteral("13:00"), QStringLiteral("sleep"))}),
                         3);

        QCOMPARE(panel.rowCount(), 3);                // 两段合计上限
        QCOMPARE(panel.hiddenCount(), 2);
        QCOMPARE(panel.rowTexts().at(0), QStringLiteral("09:00  SLEEP"));
        QCOMPARE(panel.rowTexts().at(2), QStringLiteral("11:00  STUDY"));   // 先今天后明天
        QVERIFY(panel.noteText().contains(QStringLiteral("还有 2 项")));
        QVERIFY(!panel.noteIsWarning());               // 截断不是错误
    }

    void maxRowsOneKeepsTodayFirst()
    {
        SchedulePanel panel;
        panel.setSchedule(makeResult({makeRow(QStringLiteral("09:00"), QStringLiteral("sleep")),
                                      makeRow(QStringLiteral("10:00"), QStringLiteral("idle"))},
                                     {makeRow(QStringLiteral("11:00"), QStringLiteral("study"))}),
                         1);
        QCOMPARE(panel.rowCount(), 1);
        QCOMPARE(panel.hiddenCount(), 2);
        QCOMPARE(panel.rowTexts().at(0), QStringLiteral("09:00  SLEEP"));
    }

    void maxRowsZeroMeansNoLimit()
    {
        SchedulePanel panel;
        panel.setSchedule(makeResult({makeRow(QStringLiteral("09:00"), QStringLiteral("sleep")),
                                      makeRow(QStringLiteral("10:00"), QStringLiteral("idle"))},
                                     {makeRow(QStringLiteral("11:00"), QStringLiteral("study"))}),
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
        ScheduleResult result = makeResult({makeRow(QStringLiteral("09:00"), QStringLiteral("study"))}, {});
        result.problems << QStringLiteral("recurring #2: start 时间要写成 HH:MM（得到 99:99）")
                        << QStringLiteral("recurring #3: 没有 state —— 日程只有「时间 + 状态」");

        SchedulePanel panel;
        panel.setSchedule(result, 6);

        QCOMPARE(panel.rowCount(), 1);                // 好条目照常显示
        QVERIFY(panel.noteIsWarning());
        QVERIFY(panel.noteText().contains(QStringLiteral("2 条读不出来")));
    }

    void truncationAndProblemsShareOneNote()
    {
        ScheduleResult result = makeResult({makeRow(QStringLiteral("09:00"), QStringLiteral("study")),
                                            makeRow(QStringLiteral("10:00"), QStringLiteral("game"))},
                                           {});
        result.problems << QStringLiteral("recurring #9: 没有 state —— 日程只有「时间 + 状态」");

        SchedulePanel panel;
        panel.setSchedule(result, 1);

        QVERIFY(panel.noteIsWarning());
        QVERIFY(panel.noteText().contains(QStringLiteral("还有 1 项")));
        QVERIFY(panel.noteText().contains(QStringLiteral("1 条读不出来")));
    }

    void setScheduleTwiceDoesNotAccumulate()
    {
        SchedulePanel panel;
        panel.setSchedule(makeResult({makeRow(QStringLiteral("09:00"), QStringLiteral("sleep")),
                                      makeRow(QStringLiteral("10:00"), QStringLiteral("idle")),
                                      makeRow(QStringLiteral("11:00"), QStringLiteral("study"))},
                                     {}),
                         6);
        QCOMPARE(panel.rowCount(), 3);

        panel.setSchedule(makeResult({makeRow(QStringLiteral("09:00"), QStringLiteral("game"))}, {}), 6);
        QCOMPARE(panel.rowCount(), 1);
        QCOMPARE(panel.rowTexts().size(), 1);
        QCOMPARE(panel.rowTexts().at(0), QStringLiteral("09:00  GAME"));
    }

    void resultWithoutDaysIsSafe()
    {
        ScheduleResult empty;
        empty.ok = true;                              // 成功但 days 是空的（异常输入）

        SchedulePanel panel;
        panel.setSchedule(empty, 6);
        QCOMPARE(panel.rowCount(), 0);
        QCOMPARE(panel.subtitleText(), QStringLiteral("接下来 24 小时里没有日程"));
    }

    void overflowingStateIsElidedButFullTextKept()
    {
        // 状态名只有 sleep/idle/study/game 五个字母，正常宽度下走不到省略号；
        // 这条是**防御性**用例：万一以后状态名变长，截断这条路得还在。
        SchedulePanel panel;
        panel.resize(320, 400);
        panel.show();
        const QString longState = QStringLiteral("这是一个很长的状态名需要被省略号截断");
        panel.setSchedule(makeResult({makeRow(QStringLiteral("09:00"), longState)}, {}), 6);

        QCOMPARE(panel.rowTexts().at(0), QStringLiteral("09:00  ") + longState);   // 全文保留
        const QList<QLabel*> labels = panel.findChildren<QLabel*>(QStringLiteral("ScheduleTitle"));
        QCOMPARE(labels.size(), 1);
        QVERIFY2(!labels.at(0)->text().isEmpty(), "状态那格不该被清空");
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
    - state: study
      start: "10:00"
    - state: game
      days: [tue]
      start: "09:30"
)");
        QCOMPARE(file.write(text), static_cast<qint64>(text.size()));
        file.close();

        // R 系列起走**窗口**入口：kNow = 周一 15:00 -> 窗口 [15:00, 周二 15:00)
        const ScheduleResult result = ScheduleModel::loadWindowed(path, kNow);
        QVERIFY2(result.ok, qPrintable(result.error));
        QCOMPARE(result.problems.size(), 0);
        QCOMPARE(result.windowEndText, QStringLiteral("明天 15:00"));

        SchedulePanel panel;
        panel.setSchedule(result, 6);
        // 今天那条（10:00）已经出了窗口 -> 只剩明天两条：game 09:30 排在 study 10:00 前面
        QCOMPARE(panel.rowCount(), 2);
        QCOMPARE(panel.rowTexts().at(0), QStringLiteral("09:30  GAME"));
        QCOMPARE(panel.rowTexts().at(1), QStringLiteral("10:00  STUDY"));
        // 窗口只往前看 -> 界面上不会再有"已过"的行（变暗那条路径留着, 但走不到）
        QCOMPARE(panel.isRowPast(0), false);
        QCOMPARE(panel.isRowPast(1), false);
        QVERIFY(!panel.sectionLabels().isEmpty());
        // 段表头也带上窗口后的真实条数（今天 0 项 / 明天 2 项）
        QCOMPARE(panel.sectionHeaders().size(), 2);
        QCOMPARE(panel.sectionHeaders().at(0), QStringLiteral("今天 · 0 项"));
        QCOMPARE(panel.sectionHeaders().at(1), QStringLiteral("明天 · 2 项"));
        QCOMPARE(panel.subtitleText(),
                 QStringLiteral("接下来 24 小时 · 到 明天 15:00 · 2 项 · 下一条 明天 09:30"));
    }
};

QTEST_MAIN(TestSchedulePanel)
#include "test_schedule_panel.moc"
