// ============================================================================
//  gui/src/ui/cover_loader.cpp — 封面取图实现（T11-7）
// ============================================================================
#include "ui/cover_loader.h"

#include <QDebug>
#include <QNetworkAccessManager>
#include <QNetworkReply>
#include <QNetworkRequest>
#include <QTimer>

namespace {

/// 取图超时（毫秒）：板端网络慢，但十几张图不能有一张把这一轮拖死。
constexpr int kCoverTimeoutMs = 15000;

/// 与 Agent 侧同一支 UA（`agent/net/bilibili_api.py` 的 DEFAULT_UA 同一口径）。
const char kUserAgent[] =
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36";

/// 实测（T11-0）：封面 CDN 不带 Referer 直接 403，所以必须带头。
const char kReferer[] = "https://www.bilibili.com/";

} // namespace

CoverLoader::CoverLoader(QObject* parent)
    : QObject(parent)
{
    // 默认取图钩子：QNetworkAccessManager（懒建，见下面 lambda 里的 net_）。
    fetcher_ = [this](const QUrl& url,
                      const QHash<QByteArray, QByteArray>& headers,
                      std::function<void(const QByteArray&, bool)> done) {
        if (net_ == nullptr) {
            net_ = new QNetworkAccessManager(this);
        }
        QNetworkRequest request(url);
        // 跟随 http->https / CDN 跳转（B 站封面偶尔会 302 到另一个节点）
        request.setAttribute(QNetworkRequest::FollowRedirectsAttribute, true);
        for (auto it = headers.constBegin(); it != headers.constEnd(); ++it) {
            request.setRawHeader(it.key(), it.value());
        }
        QNetworkReply* reply = net_->get(request);
        // 超时用**挂在 reply 上的**单次定时器：reply 一结束它就被父对象一起回收，
        // 不会留下野定时器（也就不用自己维护一张 reply->timer 的表）。
        auto* timer = new QTimer(reply);
        timer->setSingleShot(true);
        connect(timer, &QTimer::timeout, reply, [reply]() {
            if (reply->isRunning()) {
                reply->abort();
            }
        });
        timer->start(kCoverTimeoutMs);
        connect(reply, &QNetworkReply::finished, this, [reply, done]() {
            const bool ok = (reply->error() == QNetworkReply::NoError);
            const QByteArray bytes = ok ? reply->readAll() : QByteArray();
            reply->deleteLater();
            if (done) {
                done(bytes, ok && !bytes.isEmpty());
            }
        });
    };
}

CoverLoader::CoverLoader(Fetcher fetcher, QObject* parent)
    : QObject(parent)
    , fetcher_(std::move(fetcher))
{
}

CoverLoader::~CoverLoader() = default;

QHash<QByteArray, QByteArray> CoverLoader::requestHeaders()
{
    QHash<QByteArray, QByteArray> headers;
    headers.insert(QByteArrayLiteral("User-Agent"), QByteArray(kUserAgent));
    headers.insert(QByteArrayLiteral("Referer"), QByteArray(kReferer));
    return headers;
}

void CoverLoader::request(const QString& key, const QString& url)
{
    const QString cleanKey = key.trimmed();
    if (cleanKey.isEmpty()) {
        return;
    }
    if (cache_.contains(cleanKey)) {
        emit coverReady(cleanKey, cache_.value(cleanKey));       // 缓存命中：同步给
        return;
    }
    if (failed_.contains(cleanKey) || pending_.contains(cleanKey)) {
        return;                                                  // 别再折腾同一张图
    }
    const QUrl parsed(url.trimmed());
    if (url.trimmed().isEmpty() || !parsed.isValid() || parsed.scheme().isEmpty()) {
        failed_.insert(cleanKey);                                // 地址就是空的：当失败，不重试
        return;
    }

    pending_.insert(cleanKey);
    ++fetchCount_;
    const QHash<QByteArray, QByteArray> headers = requestHeaders();
    fetcher_(parsed, headers, [this, cleanKey](const QByteArray& bytes, bool ok) {
        deliver(cleanKey, bytes, ok);
    });
}

void CoverLoader::deliver(const QString& key, const QByteArray& bytes, bool ok)
{
    if (!pending_.remove(key)) {
        return;                          // 从没请求过 / 已经结算过（重复回调）
    }
    if (!ok) {
        failed_.insert(key);
        qInfo().noquote() << QStringLiteral("[bilibili] 封面没下来（这一格就空着）: %1").arg(key);
        emit coverFailed(key);
        return;
    }
    QPixmap cover;
    if (!cover.loadFromData(bytes)) {
        failed_.insert(key);
        qWarning().noquote() << QStringLiteral("[bilibili] 封面不是能认的图片: %1").arg(key);
        emit coverFailed(key);
        return;
    }
    cache_.insert(key, cover);
    qInfo().noquote() << QStringLiteral("[bilibili] 封面到了 %1（%2×%3，缓存 %4 张）")
                             .arg(key)
                             .arg(cover.width())
                             .arg(cover.height())
                             .arg(cache_.size());
    emit coverReady(key, cover);
}
