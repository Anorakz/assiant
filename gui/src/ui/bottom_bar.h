// ============================================================================
//  gui/src/ui/bottom_bar.h — 下区域容器（96px，不通栏）
//
//  方案 §3.3：同一位置**二选一**（互斥）：
//      非游戏模式 → 音乐条（MusicBar）
//      游戏模式   → B 站封面/标题（T9 留的占位，**T11-7 填成真东西**）
// ============================================================================
#pragma once

#include <QString>
#include <QWidget>

class BilibiliCover;
class MusicBar;
class QStackedWidget;

class BottomBar : public QWidget {
    Q_OBJECT

public:
    explicit BottomBar(QWidget* parent = nullptr);

    /// 模式变化时切换页面：GAME → 封面页；其它（含未知）→ 音乐条。
    void setMode(const QString& mode);

    /// 当前显示的是哪一页："music" | "cover"（单测/验收日志用）
    QString pageName() const;

    MusicBar* musicBar() const { return music_; }
    /// 封面页本体（T11-7：封面/标题/作者/第几条/队列来源）
    BilibiliCover* bilibiliCover() const { return cover_; }
    /// 兼容老名字：同一个控件（返回 QWidget*）
    QWidget* coverPage() const;

private:
    QStackedWidget* stack_ = nullptr;
    MusicBar* music_ = nullptr;
    BilibiliCover* cover_ = nullptr;
};
