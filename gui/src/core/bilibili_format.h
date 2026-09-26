// ============================================================================
//  gui/src/core/bilibili_format.h — B 站预览栏的**纯函数**（T11-7）
//
//  为什么单独一个文件：这些是"数字 -> 给人看的字"的换算，与窗口/控件无关，
//  所以能被单测直接钉住（T14 起控件的文字一律不许现算现写死在 paint 里）。
//
//  事实来源
//  ---------------------------------------------------------------------------
//  · 队列条目的字段：`docs/ipc-protocol.md` §3 `bilibili` 表
//    （`{bvid,title,author,duration_s,play,cover,url}`）；
//  · GUI **不显示清晰度**（你 T11 定的：清晰度只走 LLM 对话框）—— 这里也没有它；
//  · `video_state` 的载荷形状：同文档 §4（GUI → Agent 的进度回报）。
// ============================================================================
#pragma once

#include <QJsonObject>
#include <QString>

namespace bilibili {

/// 播放量 → 给人看的短写法：`9719` -> `9719`、`1289221` -> `128.9万`。
///
/// @note 只在**超过一万**时才用「万」，且保留一位小数（B 站自己也是这个口径）；
///       负数/异常值一律当 0（宁可显示 0，也不要显示 -1 或者科学计数法）。
QString formatPlayCount(qint64 play);

/// 秒 → `3:04`；超过一小时 → `1:02:03`。负数/0 → `0:00`（队里时长缺失时就是这个）。
QString formatDuration(int seconds);

/// bvid → 视频地址 `https://www.bilibili.com/video/<bvid>`（地址栏显示用）。
///
/// @note 空 bvid → 空字符串（地址栏就该是空的，不编一个假地址出来）。
QString videoUrl(const QString& bvid);

/// 预览栏一行里的第二行：`3:04 · 128.9万`（时长/播放量任一缺就只显示另一个）。
QString previewMeta(int durationS, qint64 play);

/// 预览栏能放下几格缩略图（= 队列目标 3N 里的那个 N）。
///
/// @param viewportWidth 预览栏**可见区**宽度（px）
/// @param cellWidth     一格缩略图的宽（含图标与文字）
/// @param spacing       两格之间的间隔
/// @return **至少 1**（宽度还没算出来时也不能是 0 —— 上报 0 会让 Agent 那边夹到 1）
int visibleCells(int viewportWidth, int cellWidth, int spacing);

/// GUI → Agent 的 `video_state` 载荷（**只有这一处构造**）。
///
/// @param positionMs 播放位置（毫秒；拿不到传 0）
/// @param durationMs 总时长（毫秒；**流式播放实测是 0**，如实传 0）
/// @param playing    播放器当前是不是在放（Agent 靠它决定"暂停时多预取"）
/// @param eof        这一条**放完了**（Agent 据此自动下一集）
/// @note `position_s` 用**秒**（协议里就是这么写的）：`23840ms -> 23.84`。
QJsonObject videoStatePayload(qint64 positionMs, qint64 durationMs, bool playing, bool eof);

} // namespace bilibili
