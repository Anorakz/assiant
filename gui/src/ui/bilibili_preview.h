// ============================================================================
//  gui/src/ui/bilibili_preview.h — 视频区·预览栏 + 地址栏（T11-7）
//
//  这是你 T11 定下来的那条链路的**看得见的那一头**：
//
//      ┌─ 地址栏（只读）: https://www.bilibili.com/video/BV…  ┌ 对话：luna say maybe ┐
//      ├─ 预览栏: [缩略图1] [缩略图2] … [缩略图N]   ← N = 能放下的格数（上报给 Agent）
//      └─ 队列一条都没有时: 一行灰字说明（不显示空列表）
//
//  规矩（都是你定的）
//  ---------------------------------------------------------------------------
//  · **只显示、不决定放什么**：点缩略图 -> `pickRequested(index)` -> `bilibili_pick`；
//    队列增删（翻页/去重/边界）全在 Agent 侧（`agent/core/bilibili.py`）；
//  · 队列长度 = **3 × 这里的格数**：所以格子数一变就要上报 `bilibili_viewport{visible}`；
//  · **地址栏是只读的**（GUI 不能自己搜 —— 关键词只从对话或画面来）；
//  · **一个清晰度字段都没有**：清晰度只走聊天气泡（LLM）。
// ============================================================================
#pragma once

#include <QHash>
#include <QJsonObject>
#include <QString>
#include <QWidget>

class QLabel;
class QLineEdit;
class QListWidget;
class QListWidgetItem;
class QStackedWidget;
class CoverLoader;

class BilibiliPreview : public QWidget {
    Q_OBJECT

public:
    explicit BilibiliPreview(QWidget* parent = nullptr);

    /// 取图器（封面用）。不注入也能用 —— 那就是"只有文字、没有缩略图"。
    void setCoverLoader(CoverLoader* loader);

    /// 协议 topic `bilibili` 的 data（**原样**丢进来，字段含义见 docs/ipc-protocol.md §3）。
    void setData(const QJsonObject& data);

    QListWidget* list() const { return list_; }
    QLineEdit* addressBar() const { return address_; }
    QLabel* hintLabel() const { return hint_; }
    QLabel* sourceLabel() const { return source_; }
    QLabel* addressCaption() const { return addressCaption_; }
    /// 0 = 灰字说明（队列空）/ 1 = 预览列表（单测核对显示的是哪一页）
    QStackedWidget* pages() const { return pages_; }

    /// 预览栏现在能放下几格（= 上报的 `visible`）。
    int visibleCells() const;
    /// 列表里现在有几条（= Agent 给的窗口长度）。
    int itemCount() const;
    /// 当前第几条（Agent 的 `index`）。
    int currentIndex() const { return index_; }
    /// 当前那条的 bvid（没有队列 = 空）。
    QString currentBvid() const { return currentBvid_; }
    /// 最近一次上报出去的格数（0 = 还没上报过）。
    int reportedCells() const { return reported_; }

    /// 一格的宽（含间隔）—— 单测与验收都要用它算可见格数。
    static int cellWidth();
    static int cellHeight();
    /// 地址栏那一行 + 列表的总高度（VideoPanel 按它留地方）。
    static int barHeight();

    /// **等价于用户点了第 index 格**（真点击也走这一条；验收/单测不必模拟鼠标）。
    /// @return 发出的 `pickRequested` 的下标；越界返回 -1 且什么都不发。
    int activateItem(int index);

signals:
    /// 用户点了第 index 格（**只有真点击才发**；代码移动高亮不发）。
    void pickRequested(int index);
    /// 可见格数变了 / 第一次算出结果 -> 该发 `bilibili_viewport{visible}` 了。
    void viewportChanged(int visible);

protected:
    void resizeEvent(QResizeEvent* event) override;
    void showEvent(QShowEvent* event) override;
    /// 盯**列表可见区**的尺寸：预览栏自己的尺寸可能不变，而列表是布局给的
    /// （实测踩到：上报过一次 `visible=1`，之后再没机会纠正 —— 队列目标就成了 3 条）
    bool eventFilter(QObject* watched, QEvent* event) override;

private:
    void rebuild(const QStringList& bvids, int index);
    void updateHighlight(int index);
    void updateAddress(const QString& bvid, const QString& url);
    void updateSourceText(const QJsonObject& data);
    void maybeReportViewport();
    void requestCoversAround(int index);
    QListWidgetItem* itemFor(const QString& bvid) const;

    QLabel* addressCaption_ = nullptr;
    QLineEdit* address_ = nullptr;
    QLabel* source_ = nullptr;
    QStackedWidget* pages_ = nullptr;      ///< 0 = 灰字说明 / 1 = 列表
    QLabel* hint_ = nullptr;
    QListWidget* list_ = nullptr;

    CoverLoader* loader_ = nullptr;
    QJsonObject data_;
    QHash<QString, QListWidgetItem*> items_;   ///< bvid -> 列表项
    QStringList bvids_;
    int index_ = 0;
    QString currentBvid_;
    int reported_ = 0;
};
