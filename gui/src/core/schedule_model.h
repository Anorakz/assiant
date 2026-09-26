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
//    · state  **日程的内容 = 状态**（T12-4）：`sleep` / `idle` / `study` / `game`
//             （大小写不敏感；老写法 `action: {state: …}` 也认）。
//             缺 state -> 该条记 problem 并跳过（Agent 那边也是"跳过 + 警告"，
//             因为老配置里那几条纯提醒就是这样，不能让 Agent 起不来）；
//             而 state 拼错 / 时间不合法这种**真空写错**的，Agent 那边仍旧整份拒绝
//             （SchedulerError）—— 界面这边一律"逐条记 problem"，不假装没事
//    · start  "9:30" / "09:30" / "0930"，全角冒号也收；时 0..23、分 0..59
//    · days   仅 recurring：整数 0..6（周一=0）或 mon/tue/... 短名（大小写不敏感、
//             **前缀匹配**，所以 'monday' 也认）；空列表/缺失 = 每天
//             ⚠ 中文星期不认（Python 的 WEEKDAYS 只有英文短名）
//    · date   仅 oneoff：只认 YYYY-MM-DD；`date` 与 `days` 不能同时给
//    · 老字段 `title` / `end` / `remind_before_min` / `prompt` **不再读**（T12-4 去掉的），
//      出现了就当没写 —— Agent 那边会在启动日志里逐条说明"该怎么照做"
//    · 坏条目**不抛**：进 problems，其它条目照常显示。注意 Agent 那边遇到"时间不合法"
//      这种是**整份拒绝**（SchedulerError），所以界面用一行提示把它暴露出来，而不是假装没事
//
//  显示口径
//  ---------------------------------------------------------------------------
//    · 展开层（`parse` / `loadFromConfig`）：固定两段今天、明天；每段按 (start, state) 升序。
//      **这一层不带窗口** —— 它是与 agent/core/scheduler.py 逐条对齐的那一层，
//      夹具（tests/data/schedule_parity）与 C++ 的 parity 用例都盯着它，别往里塞显示规则。
//    · 窗口层（`applyWindow` / `loadWindowed`，R 系列）：只留 `[now - kTailMinutes, now + hours)`
//      里的行 —— 也就是"接下来 24 小时"**外加最近 30 分钟刚过去的**那一小段。
//      尾巴里的行 `past == true`（界面上变暗），与 S4 那套渲染是同一路。
//      ⚠ 窗口起点跨午夜时，前一天的尾巴行 GUI 看不到（展开层只有今天/明天两段）——
//        CLI 那边能看到（它按需展开任意天）。这是"展开层不动"的代价，已记进 docs/gui.md。
//    · `past`（今天段专用）= 现在时刻已过 start —— 纯时间比较，
//      **不代表** Agent 一定触发过（Agent 还有 window_min / late_grace_min）。
//    · `remind_before_min`（"提前多久提醒"）整体去掉了（T12-4）：到点就是到点，
//      没有"提前时刻"这种东西要展示
// ============================================================================
#pragma once

#include <QDate>
#include <QDateTime>
#include <QString>
#include <QStringList>
#include <QVector>

namespace core {

/// 窗口长度（小时）。与 CLI 的 `--hours` 默认值同口径（agent/cli.py 的 WINDOW_HOURS_DEFAULT）。
constexpr int kWindowHours = 24;

/// "刚过去"的尾巴（分钟）。与 CLI 的 `TAIL_MINUTES` 同口径：
/// 窗口只往前看，但**刚刚**过去的那一小段留着 —— 否则"到点了、触发没触发"在界面上
/// 完全看不见。CLI 那边用它显示「已触发/已过（未触发）」；GUI 拿不到触发事实，
/// 所以尾巴里的行只是**变暗**（`past`），不写字。
constexpr int kTailMinutes = 30;

/// 日程区里的一行。
struct ScheduleRow {
    QString time;       ///< "HH:MM"（规范化成两位，便于等宽对齐）
    /// 状态（**小写**，`sleep`/`idle`/`study`/`game`）—— 日程的内容就是它；
    /// 界面显示时自己转成大写/中文（`SchedulePanel::rowTextOf`）
    QString state;
    bool past = false;  ///< 仅"今天"段：start 已过（不代表 Agent 触发过）
};

/// 一段（今天 / 明天）。
struct ScheduleDay {
    QString label;              ///< "今天" / "明天"
    QDate date;                 ///< 这一段是哪天
    QVector<ScheduleRow> rows;  ///< 按 (start, state) 升序
};

/// 一次"读 + 展开"的结果。
struct ScheduleResult {
    /// false = 整份读不出来（文件缺失 / YAML 语法错），此时 error 有值、
    /// days 里没有可用行。逐条坏日程**不算**整份失败（进 problems）。
    bool ok = false;
    QString error;              ///< 整份失败的原因（界面显示一行）
    QVector<ScheduleDay> days;  ///< 固定两段：今天、明天（可能都为空）
    QStringList problems;       ///< 逐条坏日程，形如 "recurring #1: 没有 state —— …"
    int totalInConfig = 0;      ///< 配置里成功解析出几条（含今天/明天之外的）

    /// 窗口长度（小时）：副标题用；未经 `applyWindow` 时是 kWindowHours。
    int windowHours = kWindowHours;
    /// 窗口终点（= now + windowHours）；**无效**表示这份结果没经过窗口层。
    QDateTime windowEnd;
    /// 窗口终点的说法（"明天 18:26" / "09-25 18:26"）；空 = 不知道（没经过窗口层）。
    QString windowEndText;

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

    /// 按窗口裁剪：只留 `[now - kTailMinutes, now + kWindowHours)` 里的行。
    /// @param expanded 展开层的结果；ok=false / problems 原样保留
    /// @param hours 窗口长度；**上限收敛到 kWindowHours(24)** —— 展开层只有今天/明天
    ///              两段（parity 夹具盯着那一层），更宽的窗口拿不到行，与其写着
    ///              "接下来 72 小时"却只显示两天，不如收敛。
    /// @note 比较在**分钟粒度**上做（行的时刻只有分钟）：把 now 截到分钟，
    ///       这样"现在这一分钟"的那条还在，也与 CLI 的窗口口径一致。
    static ScheduleResult applyWindow(const ScheduleResult& expanded, const QDateTime& now,
                                      int hours = kWindowHours);

    /// 应用真正用的入口：读文件 + 展开 + 按窗口裁剪。
    static ScheduleResult loadWindowed(const QString& path, const QDateTime& now,
                                       int hours = kWindowHours);
};

} // namespace core
