// ============================================================================
//  gui/src/core/view_state.h — 协议 → 显示状态（不依赖 QWidgets，可单测）
//
//  输入：docs/ipc-protocol.md §3 的 topic（status / llm / wallpaper / music）
//  输出：界面要显示什么（模式、连接、最近回复、壁纸、曲目）+ 诊断计数
//
//  ⚠ 这里**只存"标量型"的那几个 topic**：`bilibili`（队列是数组）由视频区自己消化，
//    它算**已知** topic（T11-7 起不再计入 `ignoredTopicCount`），只是不在这里存；
//    `schedule` 是 GUI **故意不认**的（契约见 tests/test_view_state.cpp）。
//
//  处理规则（与协议 §6「一条坏消息只影响它自己」对齐）
//  ---------------------------------------------------------------------------
//    · 未知 topic                → 忽略（ignoredTopicCount++，**不算错误**）
//    · 已知 topic 字段缺失/类型错 → 该字段不动 + droppedCount++，其它合法字段照常应用
//      （协议原文是"丢弃并记 warning"；对显示状态而言，部分应用比整条丢弃更有用，
//        计数照样暴露给 debug 面板）
// ============================================================================
#pragma once

#include <QJsonObject>
#include <QString>
#include <QStringList>

namespace core {

class ViewState {
public:
    /// 协议 §3 的模式全集（**全大写**）。
    static const QStringList& modes();
    static bool isValidMode(const QString& mode);

    /// 「模式切换按钮」的可选项（方案 §3 首页可变按钮）：
    ///   IDLE 或未知 → SLEEP / STUDY / GAME（三个入口）
    ///   STUDY / GAME → IDLE（退出）+ SLEEP + 另一个活跃模式
    ///                  ⚠ 跨模式那一跳由 **Agent 按状态图的规矩走**（`GAME -> IDLE -> SLEEP`），
    ///                  界面不再自己拦（T12-3：以前把跨模式全灰掉，"从游戏点睡眠"根本发不出去）
    ///   SLEEP       → 只有 IDLE（退出当前模式）
    static QStringList modeSwitchChoices(const QString& currentMode);

    /// 应用一条消息。返回是否有任一字段被更新。
    bool applyMessage(const QString& topic, const QJsonObject& data,
                      const QString& rawLine = QString());

    // ---- status ----
    bool hasStatus() const { return hasStatus_; }
    QString mode() const { return mode_; }
    bool connected() const { return connected_; }

    // ---- llm ----
    bool hasLlm() const { return hasLlm_; }
    QString llmText() const { return llmText_; }

    // ---- wallpaper ----
    bool hasWallpaper() const { return hasWallpaper_; }
    QString wallpaperPath() const { return wallpaperPath_; }
    int wallpaperIndex() const { return wallpaperIndex_; }

    // ---- music ----
    bool hasMusic() const { return hasMusic_; }
    QString musicTitle() const { return musicTitle_; }
    bool musicPlaying() const { return musicPlaying_; }

    // ---- 诊断（设置页 debug 显示）----
    int droppedCount() const { return droppedCount_; }
    int ignoredTopicCount() const { return ignoredTopicCount_; }
    QString lastTopic() const { return lastTopic_; }
    QString lastRawLine() const { return lastRawLine_; }

    void reset();

private:
    QString mode_;
    bool connected_ = false;
    bool hasStatus_ = false;

    bool hasLlm_ = false;
    QString llmText_;

    bool hasWallpaper_ = false;
    QString wallpaperPath_;
    int wallpaperIndex_ = -1;

    bool hasMusic_ = false;
    QString musicTitle_;
    bool musicPlaying_ = false;

    int droppedCount_ = 0;
    int ignoredTopicCount_ = 0;
    QString lastTopic_;
    QString lastRawLine_;
};

} // namespace core
