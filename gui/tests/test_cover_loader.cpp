// ============================================================================
//  gui/tests/test_cover_loader.cpp — 封面取图器单测（T11-7）
//
//  **不发真网络**：取图钩子是注入的（同步回调），所以这里能钉住
//    · 头里必须有 UA + Referer（板端实测：不带就被 CDN 回 403）
//    · 同一张图**只下一次**（缓存命中 / 正在飞 / 已经失败过）
//    · 地址是空的 / 不是图片 → 记成失败，且**不反复重试**
//    · 失败会发 coverFailed（界面据此写一句实话，而不是一直转圈）
//  ⚠ 取图钩子会被 std::function **拷贝**一份，所以计数放在 shared_ptr 里，
//    否则测的是那份拷贝（踩过一次的坑）。
// ============================================================================
#include <QBuffer>
#include <QImage>
#include <QtTest/QtTest>

#include <memory>

#include "ui/cover_loader.h"

namespace {

/// 一张能认的 PNG（2×2），用来验"字节 -> QPixmap"
QByteArray pngBytes()
{
    QImage image(2, 2, QImage::Format_RGB32);
    image.fill(Qt::red);
    QByteArray bytes;
    QBuffer buffer(&bytes);
    buffer.open(QIODevice::WriteOnly);
    image.save(&buffer, "PNG");
    return bytes;
}

/// 取图的记录（跨拷贝共享）
struct FetchLog {
    int calls = 0;
    QStringList urls;
    QHash<QByteArray, QByteArray> lastHeaders;
};

/// 假取图钩子：按脚本回结果，并记下每次请求
class FakeFetcher {
public:
    FakeFetcher(QByteArray bytes, bool ok, std::shared_ptr<FetchLog> log)
        : bytes_(std::move(bytes))
        , ok_(ok)
        , log_(std::move(log))
    {
    }

    void operator()(const QUrl& url,
                    const QHash<QByteArray, QByteArray>& headers,
                    std::function<void(const QByteArray&, bool)> done)
    {
        ++log_->calls;
        log_->urls.append(url.toString());
        log_->lastHeaders = headers;
        if (done) {
            done(bytes_, ok_);
        }
    }

private:
    QByteArray bytes_;
    bool ok_ = true;
    std::shared_ptr<FetchLog> log_;
};

} // namespace

class TestCoverLoader : public QObject {
    Q_OBJECT

private slots:
    void requestHeadersCarryUserAgentAndReferer();
    void aGoodImageEndsUpInTheCache();
    void theSameKeyIsFetchedOnlyOnce();
    void aFailedFetchIsRememberedAndNotRetried();
    void garbageBytesCountAsFailure();
    void anEmptyKeyOrUrlIsRefusedWithoutAFetch();
    void cachedImageIsDeliveredImmediately();
};

void TestCoverLoader::requestHeadersCarryUserAgentAndReferer()
{
    const QHash<QByteArray, QByteArray> headers = CoverLoader::requestHeaders();
    QVERIFY(headers.contains(QByteArrayLiteral("User-Agent")));
    QVERIFY(headers.contains(QByteArrayLiteral("Referer")));
    QVERIFY(headers.value(QByteArrayLiteral("Referer")).contains("bilibili.com"));
    // UA 不能是空/默认值（B 站 CDN 会 403）
    QVERIFY(headers.value(QByteArrayLiteral("User-Agent")).contains("Mozilla"));
}

void TestCoverLoader::aGoodImageEndsUpInTheCache()
{
    auto log = std::make_shared<FetchLog>();
    CoverLoader loader(FakeFetcher(pngBytes(), true, log));
    QSignalSpy ready(&loader, &CoverLoader::coverReady);

    loader.request(QStringLiteral("BV1"), QStringLiteral("https://i0.hdslb.com/a.jpg"));

    QCOMPARE(log->calls, 1);
    QCOMPARE(ready.count(), 1);
    QCOMPARE(ready.first().at(0).toString(), QStringLiteral("BV1"));
    QCOMPARE(loader.cacheSize(), 1);
    QCOMPARE(loader.cached(QStringLiteral("BV1")).size().width(), 2);
    QVERIFY(!loader.hasFailed(QStringLiteral("BV1")));
    // 请求头确实带上了（取图钩子收到的那一份）
    QVERIFY(log->lastHeaders.contains(QByteArrayLiteral("Referer")));
    QVERIFY(log->lastHeaders.value(QByteArrayLiteral("User-Agent")).contains("Mozilla"));
}

void TestCoverLoader::theSameKeyIsFetchedOnlyOnce()
{
    auto log = std::make_shared<FetchLog>();
    CoverLoader loader(FakeFetcher(pngBytes(), true, log));
    QSignalSpy ready(&loader, &CoverLoader::coverReady);

    loader.request(QStringLiteral("BV1"), QStringLiteral("https://x/1.jpg"));
    loader.request(QStringLiteral("BV1"), QStringLiteral("https://x/1.jpg"));
    loader.request(QStringLiteral("BV1"), QStringLiteral("https://x/1.jpg"));

    QCOMPARE(log->calls, 1);             // 后两次是缓存命中
    QCOMPARE(ready.count(), 3);          // 但每次调用都能拿到图（幂等）
}

void TestCoverLoader::aFailedFetchIsRememberedAndNotRetried()
{
    auto log = std::make_shared<FetchLog>();
    CoverLoader loader(FakeFetcher(pngBytes(), /*ok=*/false, log));
    QSignalSpy failed(&loader, &CoverLoader::coverFailed);
    QSignalSpy ready(&loader, &CoverLoader::coverReady);

    loader.request(QStringLiteral("BV1"), QStringLiteral("https://x/1.jpg"));
    loader.request(QStringLiteral("BV1"), QStringLiteral("https://x/1.jpg"));

    QCOMPARE(log->calls, 1);             // 失败过就不再试
    QCOMPARE(failed.count(), 1);
    QCOMPARE(failed.first().at(0).toString(), QStringLiteral("BV1"));
    QCOMPARE(ready.count(), 0);
    QVERIFY(loader.hasFailed(QStringLiteral("BV1")));
    QCOMPARE(loader.cacheSize(), 0);
}

void TestCoverLoader::garbageBytesCountAsFailure()
{
    auto log = std::make_shared<FetchLog>();
    CoverLoader loader(FakeFetcher(QByteArray("this is not an image"), true, log));
    QSignalSpy failed(&loader, &CoverLoader::coverFailed);

    loader.request(QStringLiteral("BV1"), QStringLiteral("https://x/1.jpg"));

    QCOMPARE(failed.count(), 1);
    QCOMPARE(loader.cacheSize(), 0);
    QVERIFY(loader.hasFailed(QStringLiteral("BV1")));
}

void TestCoverLoader::anEmptyKeyOrUrlIsRefusedWithoutAFetch()
{
    auto log = std::make_shared<FetchLog>();
    CoverLoader loader(FakeFetcher(pngBytes(), true, log));

    loader.request(QString(), QStringLiteral("https://x/1.jpg"));       // 没有键
    loader.request(QStringLiteral("BV1"), QString());                    // 没有地址
    loader.request(QStringLiteral("BV2"), QStringLiteral("不是地址"));

    QCOMPARE(log->calls, 0);              // 地址都不对就不该发请求
    QVERIFY(loader.hasFailed(QStringLiteral("BV1")));
    QVERIFY(loader.hasFailed(QStringLiteral("BV2")));
}

void TestCoverLoader::cachedImageIsDeliveredImmediately()
{
    auto log = std::make_shared<FetchLog>();
    CoverLoader loader(FakeFetcher(pngBytes(), true, log));
    loader.request(QStringLiteral("BV1"), QStringLiteral("https://x/1.jpg"));

    QSignalSpy ready(&loader, &CoverLoader::coverReady);
    loader.request(QStringLiteral("BV1"), QStringLiteral("https://x/1.jpg"));
    QCOMPARE(ready.count(), 1);           // 缓存命中是同步给的（不等事件循环）
    QCOMPARE(log->calls, 1);
}

QTEST_MAIN(TestCoverLoader)
#include "test_cover_loader.moc"
