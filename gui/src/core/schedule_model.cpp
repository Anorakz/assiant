// ============================================================================
//  gui/src/core/schedule_model.cpp — 日程解析（yaml-cpp 只读）+ 今天/明天展开
//
//  与 agent/core/scheduler.py 的逐条对应关系写在 schedule_model.h 的注释里。
//  这里只强调两条实现约定：
//    · yaml-cpp 在**类型不符/缺键/语法错**时一律抛 YAML::Exception（板端 0.6.2 实测），
//      所以解析前先查 IsDefined / IsMap / IsSequence / IsScalar，外层再兜一层 catch
//    · Python 区分 `days: [1]`（int，合法）与 `days: ["1"]`（字符串，报错），
//      yaml-cpp 里两者的区别在 Tag()：plain = "?"，quoted = "!"（板端 0.6.2 实测）
// ============================================================================
#include "core/schedule_model.h"

#include <QFile>
#include <QRegularExpression>
#include <QSet>
#include <QTime>

#include <algorithm>

#include <yaml-cpp/yaml.h>

namespace core {
namespace {

const char* const kWeekdays[7] = {"mon", "tue", "wed", "thu", "fri", "sat", "sun"};

/// 解析出来的一条日程（内部表示，不外泄）。
struct Entry {
    QString title;
    int startMinute = 0;
    int endMinute = -1;   ///< -1 = 没有 end
    bool oneoff = false;
    QDate on;             ///< 仅 oneoff
    QSet<int> days;       ///< 仅 recurring；空 = 每天
};

QString clockText(int minute)
{
    return QStringLiteral("%1:%2")
        .arg(minute / 60, 2, 10, QLatin1Char('0'))
        .arg(minute % 60, 2, 10, QLatin1Char('0'));
}

/// 纯数字判断：用 QChar::digitValue()，因此全角数字（"０９"）也算数字 ——
/// Python 的 str.isdigit() + int() 同样接受全角数字，这里保持等价。
bool allDigits(const QString& text)
{
    if (text.isEmpty()) {
        return false;
    }
    for (const QChar ch : text) {
        if (ch.digitValue() < 0) {
            return false;
        }
    }
    return true;
}

int digitsToInt(const QString& text)
{
    int value = 0;
    for (const QChar ch : text) {
        value = value * 10 + ch.digitValue();
    }
    return value;
}

/// 镜像 parse_clock：'9:30' / '09:30' / '0930' / 全角冒号。
bool parseClock(const QString& text, int* outMinute, QString* why)
{
    QString raw = text.trimmed();
    raw.replace(QChar(0xFF1A), QLatin1Char(':'));   // '：' -> ':'
    QString hh;
    QString mm;
    if (raw.contains(QLatin1Char(':'))) {
        const QStringList parts = raw.split(QLatin1Char(':'));
        if (parts.size() != 2) {
            *why = QStringLiteral("时间要写成 HH:MM");
            return false;
        }
        hh = parts.at(0).trimmed();
        mm = parts.at(1).trimmed();
    } else {
        if (raw.size() != 4 || !allDigits(raw)) {
            *why = QStringLiteral("时间要写成 HH:MM");
            return false;
        }
        hh = raw.left(2);
        mm = raw.mid(2);
    }
    if (!allDigits(hh) || !allDigits(mm)) {
        *why = QStringLiteral("时间要写成 HH:MM");
        return false;
    }
    const int hour = digitsToInt(hh);
    const int minute = digitsToInt(mm);
    if (hour < 0 || hour > 23) {
        *why = QStringLiteral("小时要在 0..23（得到 %1）").arg(hour);
        return false;
    }
    if (minute < 0 || minute > 59) {
        *why = QStringLiteral("分钟要在 0..59（得到 %1）").arg(minute);
        return false;
    }
    *outMinute = hour * 60 + minute;
    return true;
}

/// 镜像 _weekday_index：未加引号的整数 0..6，或者 mon/tue/... 前缀匹配。
/// @note 句柄按值传（非 const）：见 missing() 上面第 2 条坑
bool parseWeekday(YAML::Node node, int* outIndex, QString* why)
{
    const bool plain = (node.Tag() == "?");      // quoted -> "!"，plain -> "?"
    const QString raw = QString::fromStdString(node.Scalar()).trimmed().toLower();
    if (plain && allDigits(raw)) {
        const int value = digitsToInt(raw);
        if (value >= 0 && value <= 6) {
            *outIndex = value;
            return true;
        }
        *why = QStringLiteral("星期数字要在 0..6（得到 %1）").arg(value);
        return false;
    }
    for (int i = 0; i < 7; ++i) {
        const QString name = QString::fromLatin1(kWeekdays[i]);
        if (raw == name || raw.startsWith(name)) {
            *outIndex = i;
            return true;
        }
    }
    *why = QStringLiteral("不认识的星期 %1（可用 mon..sun 或 0..6）").arg(raw);
    return false;
}

/// 镜像 date.fromisoformat(str(x).strip())：只认 YYYY-MM-DD。
bool parseIsoDate(const QString& raw, QDate* out, QString* why)
{
    static const QRegularExpression pattern(QStringLiteral("^\\d{4}-\\d{2}-\\d{2}$"));
    const QString text = raw.trimmed();
    if (!pattern.match(text).hasMatch()) {
        *why = QStringLiteral("日期要写成 YYYY-MM-DD");
        return false;
    }
    const QDate parsed = QDate::fromString(text, QStringLiteral("yyyy-MM-dd"));
    if (!parsed.isValid()) {
        *why = QStringLiteral("日期不存在（%1）").arg(text);
        return false;
    }
    *out = parsed;
    return true;
}

/// 取不到值的节点。⚠ 板端 yaml-cpp 0.6.2 有两个坑（S2 实测，别踩回去）：
///   1. **默认构造的 `YAML::Node()` 是 `IsDefined()==true` 但 `IsNull()==true`** ——
///      所以"用 IsDefined() 判断有没有值"不成立，必须两个都看
///   2. **从 `const YAML::Node` 上取出来的节点没有 memory holder**：对它调
///      `IsNull()` 会抛 `invalid node`（`IsDefined()` 反而不抛）。所以本文件里凡是
///      要索引/查询的句柄一律用**非 const** `YAML::Node`（见下面 parse/parseEntry）
bool missing(const YAML::Node& node)
{
    return !node.IsDefined() || node.IsNull();
}

QString scalarText(const YAML::Node& node)
{
    return QString::fromStdString(node.Scalar());
}

/// PyYAML（YAML 1.1）会把某些 **plain** 标量解析成 int / float / bool / null，
/// 那些值到 Python 侧就不是 str 了 —— 而 `title` / `start` / `end` 都要求 str
/// （`isinstance(text, str)`）。这里做一个保守的同形状判断：
/// 命中就认为"这个 plain 标量在 Python 眼里不是字符串"。
/// ⚠ 只覆盖常见形状；更冷的写法（锚点、显式 !tag、复杂 flow）不在保证范围内，
///    边界见 docs/config-sources.md。
bool plainIsNotString(const QString& raw)
{
    const QString text = raw.trimmed();
    if (text.isEmpty() || text == QLatin1String("~")) {
        return true;                       // null
    }
    const QString lower = text.toLower();
    if (lower == QLatin1String("null") || lower == QLatin1String("true")
        || lower == QLatin1String("false") || lower == QLatin1String("yes")
        || lower == QLatin1String("no") || lower == QLatin1String("on")
        || lower == QLatin1String("off") || lower == QLatin1String("y")
        || lower == QLatin1String("n")) {
        return true;
    }
    // 整数：十进制 / 0b / 0x / 0o / 八进制 / 六十进制（"9:30" 在 YAML 1.1 里是整数!）
    static const QRegularExpression intRe(QStringLiteral(
        "^[-+]?(0b[01_]+|0x[0-9a-fA-F_]+|0o?[0-7_]+|0|[1-9][0-9_]*"
        "|[1-9][0-9_]*(?::[0-5]?[0-9])+)$"));
    if (intRe.match(text).hasMatch()) {
        return true;
    }
    static const QRegularExpression floatRe(QStringLiteral(
        "^[-+]?(([0-9][0-9_]*)?\\.[0-9_]+|[0-9][0-9_]*\\.|[0-9][0-9_]*[eE][-+]?[0-9]+)$"));
    if (floatRe.match(text).hasMatch()) {
        return true;
    }
    static const QRegularExpression infNanRe(
        QStringLiteral("^[-+]?(\\.inf|\\.Inf|\\.INF|\\.nan|\\.NaN|\\.NAN)$"));
    return infNanRe.match(text).hasMatch();
}

/// 这个标量在 Python 眼里是字符串吗？加引号的一定是；plain 要看形状。
bool isStringScalar(const YAML::Node& node)
{
    if (!node.IsScalar()) {
        return false;
    }
    if (node.Tag() != "?") {
        return true;                       // 带引号 / 显式 tag
    }
    return !plainIsNotString(QString::fromStdString(node.Scalar()));
}

/// 镜像 ScheduleEvent.from_config。成功返回 true；失败把原因写进 why。
/// @note 句柄按值传（非 const）：见 missing() 上面第 2 条坑
bool parseEntry(YAML::Node node, const QString& key, int index,
                Entry* out, QString* why)
{
    const QString where = QStringLiteral("%1 #%2").arg(key).arg(index + 1);
    auto fail = [&](const QString& reason) {
        *why = QStringLiteral("%1: %2").arg(where, reason);
        return false;
    };

    if (!node.IsMap()) {
        return fail(QStringLiteral("不是一个映射（- title: ... 这种）"));
    }

    // ---- title：非空字符串，去首尾空白 ----
    YAML::Node titleNode = node["title"];
    if (!titleNode.IsDefined() || !titleNode.IsScalar()) {
        return fail(QStringLiteral("缺少 title"));
    }
    if (!isStringScalar(titleNode)) {
        return fail(QStringLiteral("title 必须是字符串（加引号）"));
    }
    const QString title = scalarText(titleNode).trimmed();
    if (title.isEmpty()) {
        return fail(QStringLiteral("title 是空的"));
    }

    // ---- date / days 互斥 ----
    YAML::Node dateNode = node["date"];
    YAML::Node daysNode = node["days"];
    const bool hasDate = dateNode.IsDefined() && !dateNode.IsNull();
    const bool hasDays = daysNode.IsDefined() && !daysNode.IsNull();
    if (hasDate && hasDays) {
        return fail(QStringLiteral("不能同时给 date 和 days"));
    }

    // ---- start（必需）----
    YAML::Node startNode = node["start"];
    if (!startNode.IsDefined() || !startNode.IsScalar()) {
        return fail(QStringLiteral("缺少 start"));
    }
    if (!isStringScalar(startNode)) {
        return fail(QStringLiteral("start 必须是字符串（加引号）"));
    }
    int startMinute = 0;
    QString clockWhy;
    if (!parseClock(scalarText(startNode), &startMinute, &clockWhy)) {
        return fail(QStringLiteral("start %1（得到 %2）")
                        .arg(clockWhy, scalarText(startNode)));
    }

    // ---- end（可选）----
    int endMinute = -1;
    YAML::Node endNode = node["end"];
    if (endNode.IsDefined() && !endNode.IsNull()) {
        if (!endNode.IsScalar() || !isStringScalar(endNode)
            || !parseClock(scalarText(endNode), &endMinute, &clockWhy)) {
            return fail(QStringLiteral("end %1").arg(clockWhy));
        }
        if (endMinute < startMinute) {
            return fail(QStringLiteral("end 早于 start"));
        }
    }

    // ---- remind_before_min（可选，非负整数）----
    YAML::Node remindNode = node["remind_before_min"];
    if (remindNode.IsDefined() && !remindNode.IsNull()) {
        const bool plain = (remindNode.Tag() == "?");
        if (!remindNode.IsScalar() || !plain || !allDigits(scalarText(remindNode))) {
            return fail(QStringLiteral("remind_before_min 要是非负整数"));
        }
    }

    // ---- action（可选，必须是映射；空值视为没给）----
    YAML::Node actionNode = node["action"];
    if (actionNode.IsDefined() && !actionNode.IsNull()) {
        const bool empty = (actionNode.IsSequence() || actionNode.IsMap()) && actionNode.size() == 0;
        if (!actionNode.IsMap() && !empty) {
            return fail(QStringLiteral("action 必须是一个映射"));
        }
    }

    Entry entry;
    entry.title = title;
    entry.startMinute = startMinute;
    entry.endMinute = endMinute;

    if (hasDate) {
        entry.oneoff = true;
        QString dateWhy;
        if (!dateNode.IsScalar() || !parseIsoDate(scalarText(dateNode), &entry.on, &dateWhy)) {
            return fail(QStringLiteral("date %1").arg(dateWhy));
        }
    } else if (hasDays) {
        if (!daysNode.IsSequence()) {
            return fail(QStringLiteral("days 必须是一个列表"));
        }
        for (std::size_t i = 0; i < daysNode.size(); ++i) {
            YAML::Node item = daysNode[i];
            if (!item.IsScalar()) {
                return fail(QStringLiteral("days 里第 %1 项不是星期").arg(i + 1));
            }
            int weekday = 0;
            QString dayWhy;
            if (!parseWeekday(item, &weekday, &dayWhy)) {
                return fail(QStringLiteral("days 里 %1").arg(dayWhy));
            }
            entry.days.insert(weekday);
        }
    }

    *out = entry;
    return true;
}

/// 这条日程在那一天会不会发生（镜像 ScheduleEvent.occurs_on）。
bool occursOn(const Entry& entry, const QDate& day)
{
    if (entry.oneoff) {
        return entry.on == day;
    }
    if (entry.days.isEmpty()) {
        return true;                       // 空 = 每天
    }
    return entry.days.contains(day.dayOfWeek() - 1);   // Qt: 1=Mon..7=Sun -> 0..6
}

bool rowBefore(const ScheduleRow& a, const ScheduleRow& b)
{
    if (a.time != b.time) {
        return a.time < b.time;
    }
    return a.title < b.title;
}

/// 窗口终点的说法：`明天 18:26` / `09-25 18:26`。
/// 镜像 agent/cli.py 的 `window_end_text`（同一套口径：今天/明天/后天，更远的给日期）。
QString endLabel(const QDateTime& end, const QDateTime& now)
{
    const qint64 days = now.date().daysTo(end.date());
    QString label;
    switch (days) {
    case 0: label = QStringLiteral("今天"); break;
    case 1: label = QStringLiteral("明天"); break;
    case 2: label = QStringLiteral("后天"); break;
    default: label = end.date().toString(QStringLiteral("MM-dd")); break;
    }
    return QStringLiteral("%1 %2").arg(label, end.time().toString(QStringLiteral("HH:mm")));
}

} // namespace

int ScheduleResult::totalRows() const
{
    int total = 0;
    for (const ScheduleDay& day : days) {
        total += day.rows.size();
    }
    return total;
}

ScheduleResult ScheduleModel::loadFromConfig(const QString& path, const QDateTime& now)
{
    QFile file(path);
    if (!file.open(QIODevice::ReadOnly)) {
        ScheduleResult result;
        result.ok = false;
        result.error = QStringLiteral("读不到配置文件：%1").arg(path);
        return result;
    }
    const QString text = QString::fromUtf8(file.readAll());
    return parse(text, now);
}

ScheduleResult ScheduleModel::parse(const QString& yamlText, const QDateTime& now)
{
    ScheduleResult result;
    QVector<Entry> entries;

    try {
        YAML::Node root = YAML::Load(yamlText.toStdString());
        if (!root.IsMap()) {
            result.ok = false;
            result.error = QStringLiteral("配置的顶层不是一个映射");
            return result;
        }

        YAML::Node schedulerNode = root["scheduler"];
        YAML::Node section;                      // 只有真是映射时才当"段"用
        if (schedulerNode.IsMap()) {
            section = schedulerNode;
        }

        const char* const keys[2] = {"recurring", "oneoff"};
        for (const char* key : keys) {
            YAML::Node list;
            if (section.IsMap()) {
                list = section[key];
            }
            if (missing(list)) {
                list = root[key];          // 逐键回落到顶层（与 Python 一致）
            }
            if (missing(list)) {
                continue;
            }
            if (!list.IsSequence()) {
                result.problems << QStringLiteral("%1 不是列表（Agent 会因此拒绝整份日程）")
                                       .arg(QString::fromLatin1(key));
                continue;
            }
            for (std::size_t i = 0; i < list.size(); ++i) {
                Entry entry;
                QString why;
                if (parseEntry(list[i], QString::fromLatin1(key),
                               static_cast<int>(i), &entry, &why)) {
                    entries.append(entry);
                } else {
                    result.problems << why;
                }
            }
        }
    } catch (const YAML::Exception& exc) {
        result.ok = false;
        result.error = QStringLiteral("配置读不出来（YAML）：%1")
                           .arg(QString::fromUtf8(exc.what()));
        return result;
    } catch (const std::exception& exc) {
        result.ok = false;
        result.error = QStringLiteral("配置读不出来：%1")
                           .arg(QString::fromUtf8(exc.what()));
        return result;
    }

    result.ok = true;
    result.totalInConfig = entries.size();

    const QDate today = now.date();
    const QDate tomorrow = today.addDays(1);
    const QTime clock = now.time();
    const int nowMinute = clock.hour() * 60 + clock.minute();

    const QDate dates[2] = {today, tomorrow};
    const QString labels[2] = {QStringLiteral("今天"), QStringLiteral("明天")};
    for (int i = 0; i < 2; ++i) {
        ScheduleDay day;
        day.label = labels[i];
        day.date = dates[i];
        for (const Entry& entry : entries) {
            if (!occursOn(entry, dates[i])) {
                continue;
            }
            ScheduleRow row;
            row.time = clockText(entry.startMinute);
            row.end = (entry.endMinute >= 0) ? clockText(entry.endMinute) : QString();
            row.title = entry.title;
            // 只有"今天"段才有过去/将来之分；纯时间比较，不代表 Agent 触发过
            row.past = (i == 0) && (nowMinute > entry.startMinute);
            day.rows.append(row);
        }
        std::sort(day.rows.begin(), day.rows.end(), rowBefore);
        result.days.append(day);
    }

    return result;
}

ScheduleResult ScheduleModel::applyWindow(const ScheduleResult& expanded, const QDateTime& now,
                                          int hours)
{
    ScheduleResult result = expanded;                 // ok / error / problems / totalInConfig 原样
    // ⚠ 窗口上限就是 24 小时：**展开层只有今天/明天两段**（那是与 Python 逐条对齐、
    //    被 parity 夹具盯着的一层）。想要更宽的窗口，得同时改展开层与夹具 —— 在那之前
    //    把 hours 收敛到 24，免得副标题写着"接下来 72 小时"却只显示了两天。
    const int wanted = (hours > 0) ? hours : kWindowHours;
    result.windowHours = qMin(wanted, kWindowHours);
    // 分钟粒度：把 now 截到分钟（行的时刻只有分钟，两侧口径要一致）
    const QDateTime from(now.date(), QTime(now.time().hour(), now.time().minute()));
    result.windowEnd = from.addSecs(static_cast<qint64>(result.windowHours) * 3600);
    result.windowEndText = endLabel(result.windowEnd, from);

    if (!result.ok) {
        return result;                    // 整份读不出来: days 本来就是空的, 别再动它
    }

    // 起点还要往前挪 kTailMinutes：那一段"刚刚过去的"留着（界面上是变暗的行）
    const QDateTime windowFrom = from.addSecs(-static_cast<qint64>(kTailMinutes) * 60);

    for (ScheduleDay& day : result.days) {
        QVector<ScheduleRow> keep;
        for (const ScheduleRow& row : day.rows) {
            const QDateTime moment(day.date, QTime::fromString(row.time, QStringLiteral("HH:mm")));
            // 窗口 = [now - 尾巴, now + hours)；尾巴里的行 `past` 用展开层算出来的值（变暗）
            if (moment.isValid() && moment >= windowFrom && moment < result.windowEnd) {
                keep.append(row);
            }
        }
        day.rows = keep;
    }
    return result;
}

ScheduleResult ScheduleModel::loadWindowed(const QString& path, const QDateTime& now, int hours)
{
    return applyWindow(loadFromConfig(path, now), now, hours);
}

} // namespace core
