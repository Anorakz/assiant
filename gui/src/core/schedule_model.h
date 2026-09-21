// ============================================================================
//  gui/src/core/schedule_model.h — 日程的"读 + 展开"（不依赖 Widgets，可单测）
//
//  为什么在这里读配置（把取舍写清楚，免得后人再问一遍）
//  ---------------------------------------------------------------------------
//    · 归一化后的决定（方案 C2）：GUI **只读** config/config.yaml 的
//      `scheduler.recurring` / `scheduler.oneoff` 来显示日程区；
//      不新增 IPC topic，也不让 GUI 自己存一份日程。
//    · **写**路径仍然只有 ConfigStore（文本级替换，保住注释与顺序）。
//      yaml-cpp 只用于**读** —— 它会整体重排、丢注释，绝不能参与写回。
//    · 代价：这里必须在 C++ 里**镜像** agent/core/scheduler.py 的日程语义。
//      防漂移靠 tests/test_schedule_parity.py（用真正的 Python 语义生成期望）
//      + C++ 侧断言；覆盖范围就是那份夹具，超出夹具的写法不保证等价
//      （docs/config-sources.md 写明了这条边界，别当它是"完全等价"）。
//
//  镜像的语义（逐条对应 ScheduleEvent.from_config）
//  ---------------------------------------------------------------------------
//    · 位置   `scheduler.recurring` / `scheduler.oneoff`；某个键在 scheduler 段里
//             找不到时**逐键**回落到顶层同名键（Python 就是这么找的）
//    · title  非空字符串（去首尾空白）；缺失/空/非字符串 -> 该条记 problem
//    · start  "9:30" / "09:30" / "0930"，全角冒号也收；时 0..23、分 0..59
//    · end    可选；必须 >= start
//    · days   仅 recurring：整数 0..6（周一=0）或 mon/tue/... 短名（大小写不敏感、
//             **前缀匹配**，所以 'monday' 也认）；空列表/缺失 = 每天
//             ⚠ 中文星期不认（Python 的 WEEKDAYS 只有英文短名）
//    · date   仅 oneoff：只认 YYYY-MM-DD；`date` 与 `days` 不能同时给
//    · remind_before_min 可选、非负整数；action 可选、必须是映射（空值视为没给）
//    · 坏条目**不抛**：进 problems，其它条目照常显示。注意 Agent 那边遇到坏条目是
//      **整份拒绝**（SchedulerError），所以界面用一行提示把它暴露出来，而不是假装没事
//
//  显示口径
//  ---------------------------------------------------------------------------
//    · 固定两段：今天、明天；每段按 (start, title) 升序
//    · `past`（今天段专用）= 现在时刻已过 start —— 纯时间比较，
//      **不代表** Agent 一定触发过（Agent 还有 window_min / late_grace_min）
//    · 不展示 remind_before_min 推导出的提醒时刻（那是 Agent 的触发语义）
// ============================================================================
#pragma once

#include <QDate>
#include <QDateTime>
#include <QString>
#include <QStringList>
#include <QVector>

namespace core {

/// 日程区里的一行。
struct ScheduleRow {
    QString time;       ///< "HH:MM"（规范化成两位，便于等宽对齐）
    QString end;        ///< 可空；有 end 时是 "HH:MM"
    QString title;
    bool past = false;  ///< 仅"今天"段：start 已过（不代表 Agent 触发过）
};

/// 一段（今天 / 明天）。
struct ScheduleDay {
    QString label;              ///< "今天" / "明天"
    QDate date;                 ///< 这一段是哪天
    QVector<ScheduleRow> rows;  ///< 按 (start, title) 升序
};

/// 一次"读 + 展开"的结果。
struct ScheduleResult {
    /// false = 整份读不出来（文件缺失 / YAML 语法错），此时 error 有值、
    /// days 里没有可用行。逐条坏日程**不算**整份失败（进 problems）。
    bool ok = false;
    QString error;              ///< 整份失败的原因（界面显示一行）
    QVector<ScheduleDay> days;  ///< 固定两段：今天、明天（可能都为空）
    QStringList problems;       ///< 逐条坏日程，形如 "recurring #1: 缺少 title"
    int totalInConfig = 0;      ///< 配置里成功解析出几条（含今天/明天之外的）

    /// 两段合计行数。
    int totalRows() const;
};

class ScheduleModel {
public:
    /// 从配置文件读（一般就是 GUI 已经解析到的 config/config.yaml）。
    /// @param now 注入"现在"；单测靠它做到确定性
    static ScheduleResult loadFromConfig(const QString& path, const QDateTime& now);

    /// 直接读一段 YAML 文本（单测 / 沙箱用，不碰文件系统）。
    static ScheduleResult parse(const QString& yamlText, const QDateTime& now);
};

} // namespace core
