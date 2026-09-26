// ============================================================================
//  gui/src/ui/cover_loader.h — B 站封面（缩略图）取图器（T11-7）
//
//  为什么要它
//  ---------------------------------------------------------------------------
//  · 队列条目里的 `cover` 是 **B 站 CDN 的图片地址**（`https://i0.hdslb.com/…`），
//    Agent 只给地址、**不代下图片**（队列"只存地址"）；
//  · 板端实测（T11-0）：这两个 CDN **不带 `User-Agent` + `Referer` 就回 403** ——
//    所以取图必须自己带头，见 requestHeaders()；
//  · 预览栏一屏十几张图，来回翻页会重复请求同一张 → 必须**内存缓存**；
//  · 网络不通/超时/图坏了都要**能降级**：那一格就是没有图（文字照显示），
//    并且**失败过就不再重试**（否则每次刷新都白等一轮）。
//
//  设计
//  ---------------------------------------------------------------------------
//  · 键是 `bvid`（队列里唯一），值是 QPixmap；
//  · `request()` 是**幂等**的：已缓存 -> 立刻发 coverReady；失败过 -> 什么都不做；
//    正在飞 -> 只记下这次调用（同一个 key 不会发两次请求）；
//  · 取图这件事本身是**可注入**的（`Fetcher`）—— 单测不发真网络，
//    真实现用 QNetworkAccessManager（15 秒超时，超时就 abort，不留悬挂请求）。
// ============================================================================
#pragma once

#include <QByteArray>
#include <QHash>
#include <QObject>
#include <QPixmap>
#include <QSet>
#include <QString>
#include <QUrl>

#include <functional>

class QNetworkAccessManager;

class CoverLoader : public QObject {
    Q_OBJECT

public:
    /// 取图钩子：拿到 url + 请求头，回 bytes（`ok=false` = 失败）。
    /// @note 回调**可能同步**触发（测试替身就是同步的），所以要能处理"request() 里又回来"。
    using Fetcher = std::function<void(const QUrl& url,
                                       const QHash<QByteArray, QByteArray>& headers,
                                       std::function<void(const QByteArray&, bool)> done)>;

    explicit CoverLoader(QObject* parent = nullptr);
    /// 注入取图钩子（单测用；生产走默认的 QNetworkAccessManager）。
    explicit CoverLoader(Fetcher fetcher, QObject* parent = nullptr);
    ~CoverLoader() override;

    /// 取图必须带的头（实测：不带 Referer/UA 会被 CDN 回 403）。
    static QHash<QByteArray, QByteArray> requestHeaders();

    /// 要一张图：缓存 -> 立刻 emit；失败过 -> 不再试；在飞 -> 等那一次。
    void request(const QString& key, const QString& url);

    QPixmap cached(const QString& key) const { return cache_.value(key); }
    bool hasFailed(const QString& key) const { return failed_.contains(key); }
    bool hasPending(const QString& key) const { return pending_.contains(key); }
    int cacheSize() const { return cache_.size(); }
    int fetchCount() const { return fetchCount_; }

signals:
    /// 图到了（key = bvid）。⚠ **只成功才发** —— 失败发的是下面那条。
    void coverReady(const QString& key, const QPixmap& cover);
    /// 这张图**取不到**（网络/403/不是图片）。给"封面"那块显示一句实话用；
    /// 预览栏那一格什么都不做（本来就只有文字）。
    void coverFailed(const QString& key);

private:
    void deliver(const QString& key, const QByteArray& bytes, bool ok);

    Fetcher fetcher_;
    QNetworkAccessManager* net_ = nullptr;      ///< 只在用默认取图钩子时才建
    QHash<QString, QPixmap> cache_;
    QSet<QString> pending_;
    QSet<QString> failed_;
    int fetchCount_ = 0;
};
