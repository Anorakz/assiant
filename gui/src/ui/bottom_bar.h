// ============================================================================
//  gui/src/ui/bottom_bar.h — 下区域容器（96px，不通栏）
//
//  方案 §3.3：同一位置**二选一**（互斥）：
//      非游戏模式 → 音乐条（MusicBar）
//      游戏模式   → B站视频封面缩略图（T9 填充，现在只有"未接入"占位页）
// ============================================================================
#pragma once

#include <QString>
#include <QWidget>

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
    /// T9 会往这个页面里塞真正的封面缩略图
    QWidget* coverPage() const { return cover_; }

private:
    QStackedWidget* stack_ = nullptr;
    MusicBar* music_ = nullptr;
    QWidget* cover_ = nullptr;
};
