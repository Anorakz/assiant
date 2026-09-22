// ============================================================================
//  gui/tests/test_schedule_model.cpp — 日程解析/展开单测（core 层，不需要界面）
//
//  固定"现在" = 2026-09-21 15:00（**周一**），所以：
//    · days 用 mon / 0  -> 今天；tue / 1 -> 明天
//    · start 09:30 已过（past=true）；18:00 未到（past=false）
//  注入 now 是为了确定性：不注入就得让测试跟着系统时间跑，那种用例没法断言。
//
//  覆盖：位置(逐键回落) / 三种时间写法+全角 / 星期各种写法 / date 互斥 /
//        end 区间 / 排序 / past 只作用于今天 / 坏条目只坏它自己 / 整份失败
// ============================================================================
#include <QDate>
#include <QDateTime>
#include <QDir>
#include <QFile>
#include <QJsonArray>
#include <QJsonDocument>
#include <QJsonObject>
#include <QJsonValue>
#include <QStringList>
#include <QTemporaryDir>
#include <QTime>
#include <QtTest/QtTest>

#include "core/schedule_model.h"

using core::ScheduleDay;
using core::ScheduleModel;
using core::ScheduleResult;
using core::ScheduleRow;

namespace {

/// 2026-09-21 是周一（dayOfWeek()==1）；下面有断言守着这个前提，
/// 谁改了它，测试会直接说清为什么后面的用例会跟着错。
const QDate kToday(2026, 9, 21);
const QDateTime kNow(kToday, QTime(15, 0));

ScheduleResult parseIt(const QString& yaml)
{
    return ScheduleModel::parse(yaml, kNow);
}

/// 取某一段的行数（0 = 今天，1 = 明天）。
int rowsOn(const ScheduleResult& result, int index)
{
    if (index < 0 || index >= result.days.size()) {
        return -1;
    }
    return result.days.at(index).rows.size();
}

QString titleOn(const ScheduleResult& result, int dayIndex, int rowIndex)
{
    return result.days.at(dayIndex).rows.at(rowIndex).title;
}

/// 把一段行渲染成与期望同形的紧凑串（`time|end|title|past`），整段比对用。
QString renderRows(const QVector<ScheduleRow>& rows)
{
    QStringList out;
    for (const ScheduleRow& row : rows) {
        out << QStringLiteral("%1|%2|%3|%4")
                   .arg(row.time, row.end, row.title,
                        row.past ? QStringLiteral("1") : QStringLiteral("0"));
    }
    return out.join(QStringLiteral("\n"));
}

QString renderJsonRows(const QJsonArray& rows)
{
    QStringList out;
    for (const QJsonValue& value : rows) {
        const QJsonObject row = value.toObject();
        out << QStringLiteral("%1|%2|%3|%4")
                   .arg(row.value(QStringLiteral("time")).toString(),
                        row.value(QStringLiteral("end")).toString(),
                        row.value(QStringLiteral("title")).toString(),
                        row.value(QStringLiteral("past")).toBool() ? QStringLiteral("1")
                                                                   : QStringLiteral("0"));
    }
    return out.join(QStringLiteral("\n"));
}

} // namespace

class TestScheduleModel : public QObject {
    Q_OBJECT

private slots:
    // ---------------------------------------------------------------- 前提 ---
    void fixtureIsMonday()
    {
        QCOMPARE(kToday.dayOfWeek(), 1);                 // 周一
        QCOMPARE(kToday.addDays(1).dayOfWeek(), 2);      // 明天周二
        QCOMPARE(kNow.time(), QTime(15, 0));
    }

    // ---------------------------------------------------------------- 基本 ---
    void emptyConfigIsOkWithTwoEmptyDays()
    {
        const ScheduleResult result = parseIt(QStringLiteral("llm:\n  mode: disabled\n"));
        QVERIFY2(result.ok, qPrintable(result.error));
        QVERIFY(result.error.isEmpty());
        QCOMPARE(result.days.size(), 2);
        QCOMPARE(result.days.at(0).label, QStringLiteral("今天"));
        QCOMPARE(result.days.at(1).label, QStringLiteral("明天"));
        QCOMPARE(result.totalRows(), 0);
        QCOMPARE(result.totalInConfig, 0);
        QCOMPARE(result.problems.size(), 0);
    }

    void recurringWithoutDaysHappensEveryDay()
    {
        const ScheduleResult result = parseIt(QStringLiteral(R"(scheduler:
  recurring:
    - title: 喝水
      start: "10:00"
)"));
        QVERIFY2(result.ok, qPrintable(result.error));
        QCOMPARE(rowsOn(result, 0), 1);
        QCOMPARE(rowsOn(result, 1), 1);
        QCOMPARE(titleOn(result, 0, 0), QStringLiteral("喝水"));
    }

    void recurringOnlyOnItsWeekdays()
    {
        const ScheduleResult result = parseIt(QStringLiteral(R"(scheduler:
  recurring:
    - title: 周一学习
      days: [mon]
      start: "09:30"
    - title: 周二站会
      days: [tue]
      start: "09:30"
)"));
        QVERIFY2(result.ok, qPrintable(result.error));
        QCOMPARE(rowsOn(result, 0), 1);
        QCOMPARE(titleOn(result, 0, 0), QStringLiteral("周一学习"));
        QCOMPARE(rowsOn(result, 1), 1);
        QCOMPARE(titleOn(result, 1, 0), QStringLiteral("周二站会"));
    }

    void weekdayAcceptsIntShortFullAndCase()
    {
        const ScheduleResult result = parseIt(QStringLiteral(R"(scheduler:
  recurring:
    - title: 整数0
      days: [0]
      start: "08:00"
    - title: 大写MON
      days: [MON]
      start: "08:01"
    - title: 全名monday
      days: [monday]
      start: "08:02"
    - title: 整数1是周二
      days: [1]
      start: "08:03"
    - title: 中文周一不认
      days: [周一]
      start: "08:04"
)"));
        QVERIFY2(result.ok, qPrintable(result.error));
        QCOMPARE(rowsOn(result, 0), 3);          // 整数0 / MON / monday 都在今天
        QCOMPARE(rowsOn(result, 1), 1);          // 1 = 周二
        QCOMPARE(result.problems.size(), 1);     // 中文星期按 Python 的规则要报错
        QVERIFY(result.problems.at(0).contains(QStringLiteral("不认识的星期")));
    }

    void weekdayEmptyListMeansEveryDay()
    {
        const ScheduleResult result = parseIt(QStringLiteral(R"(scheduler:
  recurring:
    - title: 每天
      days: []
      start: "10:00"
)"));
        QVERIFY2(result.ok, qPrintable(result.error));
        QCOMPARE(rowsOn(result, 0), 1);
        QCOMPARE(rowsOn(result, 1), 1);
    }

    void oneoffOnlyOnItsDate()
    {
        const ScheduleResult result = parseIt(QStringLiteral(R"(scheduler:
  oneoff:
    - title: 今天评审
      date: 2026-09-21
      start: "14:00"
    - title: 明天体检
      date: 2026-09-22
      start: "08:00"
    - title: 昨天的事
      date: 2026-09-20
      start: "09:00"
    - title: 下周的事
      date: 2026-09-28
      start: "09:00"
)"));
        QVERIFY2(result.ok, qPrintable(result.error));
        QCOMPARE(rowsOn(result, 0), 1);
        QCOMPARE(titleOn(result, 0, 0), QStringLiteral("今天评审"));
        QCOMPARE(rowsOn(result, 1), 1);
        QCOMPARE(titleOn(result, 1, 0), QStringLiteral("明天体检"));
        QCOMPARE(result.totalInConfig, 4);       // 过去的/下周的也算解析成功
    }

    // ---------------------------------------------------------------- 时间 ---
    void clockFormsAreAllAccepted()
    {
        const ScheduleResult result = parseIt(QStringLiteral(R"(scheduler:
  recurring:
    - title: 一位小时
      start: "9:30"
    - title: 两位小时
      start: "09:30"
    - title: 紧凑写法
      start: "0930"
    - title: 未加引号的紧凑写法
      start: 0930
    - title: 全角冒号
      start: "09：30"
)"));
        QVERIFY2(result.ok, qPrintable(result.error));
        QCOMPARE(result.problems.size(), 0);
        QCOMPARE(rowsOn(result, 0), 5);
        for (const ScheduleRow& row : result.days.at(0).rows) {
            QCOMPARE(row.time, QStringLiteral("09:30"));   // 统一规范化成两位
        }
    }

    void endIsShownAsRangeAndChecked()
    {
        const ScheduleResult result = parseIt(QStringLiteral(R"(scheduler:
  recurring:
    - title: 有区间
      start: "14:00"
      end: "15:30"
    - title: 没区间
      start: "16:00"
)"));
        QVERIFY2(result.ok, qPrintable(result.error));
        QCOMPARE(rowsOn(result, 0), 2);
        QCOMPARE(result.days.at(0).rows.at(0).end, QStringLiteral("15:30"));
        QCOMPARE(result.days.at(0).rows.at(1).end, QString());
    }

    void endBeforeStartIsAProblem()
    {
        const ScheduleResult result = parseIt(QStringLiteral(R"(scheduler:
  recurring:
    - title: 反了
      start: "15:00"
      end: "14:00"
)"));
        QVERIFY2(result.ok, qPrintable(result.error));                      // 逐条坏 ≠ 整份失败
        QCOMPARE(rowsOn(result, 0), 0);
        QCOMPARE(result.problems.size(), 1);
        QVERIFY(result.problems.at(0).contains(QStringLiteral("end 早于 start")));
    }

    // ---------------------------------------------------------------- 展示 ---
    void rowsAreSortedByStartThenTitle()
    {
        const ScheduleResult result = parseIt(QStringLiteral(R"(scheduler:
  recurring:
    - title: 晚上
      start: "21:00"
    - title: 早上B
      start: "09:00"
    - title: 早上A
      start: "09:00"
)"));
        QVERIFY2(result.ok, qPrintable(result.error));
        QCOMPARE(rowsOn(result, 0), 3);
        QCOMPARE(titleOn(result, 0, 0), QStringLiteral("早上A"));
        QCOMPARE(titleOn(result, 0, 1), QStringLiteral("早上B"));
        QCOMPARE(titleOn(result, 0, 2), QStringLiteral("晚上"));
    }

    void pastOnlyAppliesToToday()
    {
        const ScheduleResult result = parseIt(QStringLiteral(R"(scheduler:
  recurring:
    - title: 上午的
      start: "09:30"
    - title: 晚上的
      start: "18:00"
)"));
        QVERIFY2(result.ok, qPrintable(result.error));
        QCOMPARE(result.days.at(0).rows.at(0).past, true);    // 09:30 < 15:00
        QCOMPARE(result.days.at(0).rows.at(1).past, false);   // 18:00 > 15:00
        QCOMPARE(result.days.at(1).rows.at(0).past, false);   // 明天永远不是"已过"
        QCOMPARE(result.days.at(1).rows.at(1).past, false);
    }

    void exactlyNowIsNotPast()
    {
        const ScheduleResult result = parseIt(QStringLiteral(R"(scheduler:
  recurring:
    - title: 正好现在
      start: "15:00"
)"));
        QVERIFY2(result.ok, qPrintable(result.error));
        QCOMPARE(result.days.at(0).rows.at(0).past, false);
    }

    // ---------------------------------------------------------------- 窗口（R 系列）---
    // 展开层（parse）不带窗口；窗口是独立一层 applyWindow。

    /// 展开层不该有窗口痕迹（否则 parity 夹具/其它用例会跟着漂）。
    void parseLeavesTheResultUnwindowed()
    {
        const ScheduleResult result = parseIt(QStringLiteral(R"(scheduler:
  recurring:
    - title: 上午的
      start: "09:30"
)"));
        QVERIFY(result.windowEndText.isEmpty());
        QCOMPARE(result.windowEnd.isValid(), false);
        QCOMPARE(result.windowHours, core::kWindowHours);
        QCOMPARE(rowsOn(result, 0), 1);               // 09:30 还在（没被窗口裁掉）
    }

    /// 窗口 = [now, now + hours)：已过的丢掉，超过终点的丢掉，窗口内的留着。
    void applyWindowKeepsOnlyTheNext24Hours()
    {
        // ⚠ 四条全用 **oneoff**（指定日期）：没写 days 的 recurring 是**每天** ——
        //    这个坑我在 CLI 侧踩了两次、这里又踩了一次，所以干脆别在窗口用例里用它。
        const ScheduleResult expanded = parseIt(QStringLiteral(R"(scheduler:
  oneoff:
    - title: 今天上午
      date: 2026-09-21
      start: "09:30"
    - title: 今天晚上
      date: 2026-09-21
      start: "18:00"
    - title: 明天上午
      date: 2026-09-22
      start: "10:00"
    - title: 明天傍晚
      date: 2026-09-22
      start: "20:00"
)"));
        QCOMPARE(rowsOn(expanded, 0), 2);             // 展开层: 今天两条都在
        const ScheduleResult result = ScheduleModel::applyWindow(expanded, kNow);

        QCOMPARE(result.windowEndText, QStringLiteral("明天 15:00"));
        QCOMPARE(result.windowEnd, QDateTime(kToday.addDays(1), QTime(15, 0)));
        // 今天：09:30 已过 -> 掉；18:00 在窗口里 -> 留
        QCOMPARE(rowsOn(result, 0), 1);
        QCOMPARE(titleOn(result, 0, 0), QStringLiteral("今天晚上"));
        // 明天：10:00 在窗口里（< 15:00）-> 留；20:00 超过终点 -> 掉
        QCOMPARE(rowsOn(result, 1), 1);
        QCOMPARE(titleOn(result, 1, 0), QStringLiteral("明天上午"));
    }

    /// 分钟粒度：now 有秒时，把 now 截到分钟再比 —— "现在这一分钟"的那条要留着。
    void applyWindowComparesAtMinuteGranularity()
    {
        const ScheduleResult expanded = parseIt(QStringLiteral(R"(scheduler:
  recurring:
    - title: 正好这一分钟
      start: "15:00"
)"));
        const QDateTime withSeconds(kToday, QTime(15, 0, 40));
        const ScheduleResult result = ScheduleModel::applyWindow(expanded, withSeconds);
        QCOMPARE(rowsOn(result, 0), 1);
        QCOMPARE(titleOn(result, 0, 0), QStringLiteral("正好这一分钟"));
    }

    /// 窗口时长有**上限**：展开层只有今天/明天两段，更宽的窗口拿不到行 —— 所以
    /// hours 会被收敛到 24（副标题不能写着 48 小时却只显示两天）。
    void applyWindowClampsToTheExpansionRange()
    {
        const ScheduleResult expanded = parseIt(QStringLiteral(R"(scheduler:
  recurring:
    - title: 明天傍晚
      start: "20:00"
)"));
        const ScheduleResult wide = ScheduleModel::applyWindow(expanded, kNow, 48);
        QCOMPARE(wide.windowHours, core::kWindowHours);
        QCOMPARE(wide.windowEndText, QStringLiteral("明天 15:00"));
        QCOMPARE(rowsOn(wide, 1), 0);                 // 20:00 仍在外（窗口确实只有 24 小时）
    }

    /// 整份失败 / 逐条坏日程在窗口层原样保留（窗口不负责报错）。
    void applyWindowKeepsErrorsAndProblems()
    {
        ScheduleResult broken;
        broken.ok = false;
        broken.error = QStringLiteral("配置读不出来（YAML）：x");
        const ScheduleResult stillBroken = ScheduleModel::applyWindow(broken, kNow);
        QCOMPARE(stillBroken.ok, false);
        QCOMPARE(stillBroken.error, broken.error);
        QCOMPARE(stillBroken.windowEndText, QStringLiteral("明天 15:00"));

        const ScheduleResult withProblem = parseIt(QStringLiteral(R"(scheduler:
  recurring:
    - title: 好的
      start: "18:00"
    - title: 坏的
      start: "99:99"
)"));
        QCOMPARE(withProblem.problems.size(), 1);
        const ScheduleResult windowed = ScheduleModel::applyWindow(withProblem, kNow);
        QCOMPARE(windowed.problems.size(), 1);
        QCOMPARE(windowed.totalInConfig, 1);
        QCOMPARE(rowsOn(windowed, 0), 1);
    }

    /// 窗口内的行永远不是"已过"（窗口只往前看）—— 这条把界面上"看不到变暗"钉成事实。
    void windowedRowsAreNeverPast()
    {
        const ScheduleResult expanded = parseIt(QStringLiteral(R"(scheduler:
  recurring:
    - title: 上午的
      start: "09:30"
    - title: 晚上的
      start: "18:00"
)"));
        const ScheduleResult result = ScheduleModel::applyWindow(expanded, kNow);
        QCOMPARE(result.days.at(0).rows.at(0).past, false);
        QCOMPARE(result.days.at(1).rows.at(0).past, false);
    }

    // ---------------------------------------------------------------- 坏数据 ---
    void oneBadEntryDoesNotKillTheOthers()
    {
        const ScheduleResult result = parseIt(QStringLiteral(R"(scheduler:
  recurring:
    - title: 好的
      start: "09:00"
    - title: 坏的
      start: "99:99"
    - title: 也好的
      start: "10:00"
)"));
        QVERIFY2(result.ok, qPrintable(result.error));
        QCOMPARE(rowsOn(result, 0), 2);
        QCOMPARE(result.problems.size(), 1);
        QVERIFY(result.problems.at(0).contains(QStringLiteral("recurring #2")));
        QCOMPARE(result.totalInConfig, 2);
    }

    void badShapesAreEachReported_data()
    {
        QTest::addColumn<QString>("name");
        QTest::addColumn<QString>("body");

        QTest::newRow("缺 title")        << "no-title"   << "    - start: \"09:00\"\n";
        QTest::newRow("空 title")        << "empty-title" << "    - title: \"  \"\n      start: \"09:00\"\n";
        QTest::newRow("title 不是字符串") << "num-title"  << "    - title: 5\n      start: \"09:00\"\n";
        QTest::newRow("title 是 bool")   << "bool-title" << "    - title: true\n      start: \"09:00\"\n";
        QTest::newRow("start 未加引号的 9:30")
            << "start-sexagesimal" << "    - title: x\n      start: 9:30\n";
        QTest::newRow("缺 start")        << "no-start"   << "    - title: x\n";
        QTest::newRow("小时越界")        << "bad-hour"   << "    - title: x\n      start: \"25:00\"\n";
        QTest::newRow("分钟越界")        << "bad-min"    << "    - title: x\n      start: \"09:61\"\n";
        QTest::newRow("时间不是字符串")  << "time-num"   << "    - title: x\n      start: 930\n";
        QTest::newRow("date 与 days 同给") << "both"     << "    - title: x\n      date: 2026-09-21\n      days: [mon]\n      start: \"09:00\"\n";
        QTest::newRow("date 格式错")     << "bad-date"   << "    - title: x\n      date: 2026/09/21\n      start: \"09:00\"\n";
        QTest::newRow("日期不存在")      << "no-date"    << "    - title: x\n      date: 2026-02-30\n      start: \"09:00\"\n";
        QTest::newRow("days 不是列表")   << "days-scalar" << "    - title: x\n      days: mon\n      start: \"09:00\"\n";
        QTest::newRow("days 项不是星期") << "days-map"   << "    - title: x\n      days: [{a: 1}]\n      start: \"09:00\"\n";
        QTest::newRow("星期数字越界")    << "day-range"  << "    - title: x\n      days: [9]\n      start: \"09:00\"\n";
        QTest::newRow("星期加了引号")    << "day-quoted" << "    - title: x\n      days: [\"1\"]\n      start: \"09:00\"\n";
        QTest::newRow("remind 是负的")   << "remind-neg" << "    - title: x\n      start: \"09:00\"\n      remind_before_min: -1\n";
        QTest::newRow("remind 是字符串") << "remind-str" << "    - title: x\n      start: \"09:00\"\n      remind_before_min: \"5\"\n";
        QTest::newRow("action 是列表")   << "action-list" << "    - title: x\n      start: \"09:00\"\n      action: [1]\n";
        QTest::newRow("条目不是映射")    << "entry-num"  << "    - 5\n";
    }

    void badShapesAreEachReported()
    {
        QFETCH(QString, name);
        QFETCH(QString, body);
        Q_UNUSED(name);

        const ScheduleResult result = parseIt(QStringLiteral("scheduler:\n  recurring:\n") + body);
        QVERIFY2(result.ok, qPrintable(result.error.isEmpty() ? body : result.error));
        QCOMPARE(result.totalInConfig, 0);
        QVERIFY2(!result.problems.isEmpty(), qPrintable(body));
    }

    void sectionNotAListIsReported()
    {
        const ScheduleResult result = parseIt(QStringLiteral(R"(scheduler:
  recurring: 5
)"));
        QVERIFY2(result.ok, qPrintable(result.error));
        QCOMPARE(result.totalInConfig, 0);
        QCOMPARE(result.problems.size(), 1);
        QVERIFY(result.problems.at(0).contains(QStringLiteral("不是列表")));
    }

    void emptyActionIsAccepted()
    {
        const ScheduleResult result = parseIt(QStringLiteral(R"(scheduler:
  recurring:
    - title: 空 action
      start: "09:00"
      action:
    - title: 空映射 action
      start: "10:00"
      action: {}
)"));
        QVERIFY2(result.ok, qPrintable(result.error));
        QCOMPARE(result.problems.size(), 0);
        QCOMPARE(rowsOn(result, 0), 2);
    }

    // ---------------------------------------------------------------- 位置 ---
    void topLevelFallbackWorksPerKey()
    {
        const ScheduleResult result = parseIt(QStringLiteral(R"(scheduler:
  interval_min: 1
  oneoff:
    - title: 段里的
      date: 2026-09-21
      start: "09:00"
recurring:
  - title: 顶层的
    start: "08:00"
)"));
        QVERIFY2(result.ok, qPrintable(result.error));
        QCOMPARE(rowsOn(result, 0), 2);
        QCOMPARE(titleOn(result, 0, 0), QStringLiteral("顶层的"));   // 08:00 排在前面
        QCOMPARE(titleOn(result, 0, 1), QStringLiteral("段里的"));
    }

    // ---------------------------------------------------------------- 整体 ---
    void badYamlFailsTheWholeThing()
    {
        const ScheduleResult result = parseIt(QStringLiteral("scheduler: [1, 2\n"));
        QVERIFY(!result.ok);
        QVERIFY(!result.error.isEmpty());
        QCOMPARE(result.days.size(), 0);
        QCOMPARE(result.totalRows(), 0);
    }

    void topLevelNotAMappingFails()
    {
        const ScheduleResult result = parseIt(QStringLiteral("- 1\n- 2\n"));
        QVERIFY(!result.ok);
        QVERIFY(result.error.contains(QStringLiteral("顶层")));
    }

    void missingFileIsReported()
    {
        QTemporaryDir dir;
        QVERIFY(dir.isValid());
        const ScheduleResult result =
            ScheduleModel::loadFromConfig(dir.filePath(QStringLiteral("nope.yaml")), kNow);
        QVERIFY(!result.ok);
        QVERIFY(result.error.contains(QStringLiteral("读不到配置文件")));
    }

    void loadFromConfigReadsRealFile()
    {
        QTemporaryDir dir;
        QVERIFY(dir.isValid());
        const QString path = dir.filePath(QStringLiteral("config.yaml"));
        QFile file(path);
        QVERIFY(file.open(QIODevice::WriteOnly | QIODevice::Truncate));
        const QByteArray text = QByteArray(R"(scheduler:
  recurring:
    - title: 文件里的
      start: "11:00"
)");
        QCOMPARE(file.write(text), static_cast<qint64>(text.size()));
        file.close();

        const ScheduleResult result = ScheduleModel::loadFromConfig(path, kNow);
        QVERIFY2(result.ok, qPrintable(result.error));
        QCOMPARE(rowsOn(result, 0), 1);
        QCOMPARE(titleOn(result, 0, 0), QStringLiteral("文件里的"));
    }

#ifdef SCHEDULE_PARITY_DIR
    /// S3：跨实现一致性守卫。
    ///   夹具与期望都由 Python 侧生成（tests/test_schedule_parity.py 用**真的**
    ///   agent.core.scheduler），这里读同一份夹具 + 同一份期望做断言 ——
    ///   C++ 这边镜像的语义漂了，这个用例就红。
    void parityWithPythonFixtures()
    {
        const QDir dir(QStringLiteral(SCHEDULE_PARITY_DIR));
        QVERIFY2(dir.exists(), qPrintable(dir.absolutePath()));

        const QStringList fixtures =
            dir.entryList(QStringList() << QStringLiteral("*.yaml"), QDir::Files, QDir::Name);
        QVERIFY2(fixtures.size() >= 6, qPrintable(dir.absolutePath()));

        for (const QString& name : fixtures) {
            const QString base = name.left(name.size() - 5);          // 去掉 ".yaml"
            const QString expectPath = dir.filePath(base + QStringLiteral(".expect.json"));
            QFile file(expectPath);
            QVERIFY2(file.open(QIODevice::ReadOnly), qPrintable(expectPath));
            const QJsonObject expected = QJsonDocument::fromJson(file.readAll()).object();
            file.close();
            QVERIFY2(!expected.isEmpty(), qPrintable(expectPath));

            const ScheduleResult result =
                ScheduleModel::loadFromConfig(dir.filePath(name), kNow);

            // 期望里的 now 是 Python 侧写死的同一个时刻；对不上说明两边常量漂了
            QCOMPARE(expected.value(QStringLiteral("now")).toString(),
                     kNow.toString(Qt::ISODate));

            if (!expected.value(QStringLiteral("python_ok")).toBool()) {
                // Python 会整份拒绝（SchedulerError）：C++ 侧不许崩，且必须把问题暴露出来
                QVERIFY2(!result.problems.isEmpty(), qPrintable(name));
                continue;
            }

            QVERIFY2(result.ok, qPrintable(result.error));
            QVERIFY2(result.problems.isEmpty(),
                     qPrintable(result.problems.join(QStringLiteral("; "))));
            QVERIFY2(result.totalInConfig == expected.value(QStringLiteral("total_in_config")).toInt(),
                     qPrintable(QStringLiteral("%1: total_in_config 期望 %2 实际 %3")
                                    .arg(name)
                                    .arg(expected.value(QStringLiteral("total_in_config")).toInt())
                                    .arg(result.totalInConfig)));

            const QJsonArray days = expected.value(QStringLiteral("days")).toArray();
            QVERIFY2(result.days.size() == days.size(), qPrintable(name));
            for (int d = 0; d < days.size(); ++d) {
                const QJsonObject day = days.at(d).toObject();
                const ScheduleDay& actualDay = result.days.at(d);
                QCOMPARE(actualDay.label, day.value(QStringLiteral("label")).toString());
                QCOMPARE(actualDay.date.toString(Qt::ISODate),
                         day.value(QStringLiteral("date")).toString());

                const QJsonArray rows = day.value(QStringLiteral("rows")).toArray();
                // 整段比对（失败时能一眼看出差在哪一行），格式 time|end|title|past
                QVERIFY2(renderRows(actualDay.rows) == renderJsonRows(rows),
                         qPrintable(QStringLiteral("%1 %2 的行不一致:\n--- C++ ---\n%3\n"
                                                   "--- Python ---\n%4")
                                        .arg(name, actualDay.label,
                                             renderRows(actualDay.rows), renderJsonRows(rows))));
            }
        }
    }
#endif
};

QTEST_APPLESS_MAIN(TestScheduleModel)

#include "test_schedule_model.moc"
