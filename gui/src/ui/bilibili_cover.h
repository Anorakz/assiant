// ============================================================================
//  gui/src/ui/bilibili_cover.h — 下区域·B 站封面（GAME 模式，T11-7）
//
//  下区域在 GAME 模式下显示的是**当前那一条**的封面 + 标题 + 作者/时长/播放量，
//  以及"这条队列是谁驱动的"（对话关键词 / 画面认出的游戏）。
//  以前这里是"B站视频封面未接入"占位（T9 留的），现在填上真东西。
//
//  规矩
//  ---------------------------------------------------------------------------
//  · 只显示 `bilibili` topic 给的**当前那条**，不自己挑、不自己搜；
//  · 封面图走共享的 CoverLoader（UA + Referer，见 cover_loader.h）；
//  · **不显示清晰度**（你定的：清晰度只走聊天气泡）；
//  · ⚠ 标题再长也不许把版面撑开（板端实测：B 站标题一行能顶到 1280+，
//    结果整个主区被推宽、地址栏与预览栏一起被挤出屏幕）—— 一律**省略号截断**，
//    完整标题挂在 tooltip 里。
// ============================================================================
#pragma once

#include <QJsonObject>
#include <QString>
#include <QWidget>

class QLabel;
class CoverLoader;

class BilibiliCover : public QWidget {
    Q_OBJECT

public:
    explicit BilibiliCover(QWidget* parent = nullptr);

    void setCoverLoader(CoverLoader* loader);
    /// 协议 topic `bilibili` 的 data（与 VideoPanel::setBilibili 同一份）。
    void setData(const QJsonObject& data);

    QLabel* coverLabel() const { return cover_; }
    QLabel* titleLabel() const { return title_; }
    QLabel* metaLabel() const { return meta_; }
    QLabel* positionLabel() const { return position_; }
    QLabel* sourceLabel() const { return source_; }
    /// 手里有没有"当前那一条"（空 = 显示的是一句说明）
    bool hasVideo() const { return hasVideo_; }
    QString currentBvid() const { return bvid_; }
    /// text 在**当前宽度**下渲染出来的样子（单测据此核对"真的截断了"）
    static QString elideFor(const QLabel* label, const QString& text);

protected:
    void resizeEvent(QResizeEvent* event) override;
    /// 四个标签自己的 resize 也要重新截断（布局给宽度时走的是**标签**的 resize）
    bool eventFilter(QObject* watched, QEvent* event) override;

private:
    void repositionText();
    void showPlaceholder(const QString& text);

    QLabel* cover_ = nullptr;
    QLabel* title_ = nullptr;
    QLabel* meta_ = nullptr;
    QLabel* position_ = nullptr;
    QLabel* source_ = nullptr;
    CoverLoader* loader_ = nullptr;
    QString bvid_;
    bool hasVideo_ = false;
    /// 完整文本（显示时按宽度截断，tooltip 里给全文）
    QString fullTitle_;
    QString fullMeta_;
    QString fullPosition_;
    QString fullSource_;
    bool repositioning_ = false;
};
