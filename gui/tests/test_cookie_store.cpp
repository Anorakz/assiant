// ============================================================================
//  gui/tests/test_cookie_store.cpp — B 站凭据文件**只读**单测（T14-3）
//
//  ⚠ T14-3 起 GUI 不写凭据文件：写它的是 Agent（`agent/core/settings_credentials.py`，
//    走 IPC 的 `set_config.credentials`）。这里只验"读得对、掩码不泄密、路径按仓库根"。
// ============================================================================
#include <QDir>
#include <QFile>
#include <QTemporaryDir>
#include <QtTest/QtTest>

#include "core/cookie_store.h"

using core::CookieStore;

namespace {

bool writeFile(const QString& path, const QString& content)
{
    QFile file(path);
    if (!file.open(QIODevice::WriteOnly | QIODevice::Truncate)) {
        return false;
    }
    const QByteArray bytes = content.toUtf8();
    return file.write(bytes) == bytes.size();
}

} // namespace

class TestCookieStore : public QObject {
    Q_OBJECT

private slots:
    void allowedKeysAreTheThreeWeAccept();
    void readsTheThreeKeysAndIgnoresTheRest();
    void missingOrBrokenFileIsAnonymousNotAnError();
    void masksForDisplay();
    void resolvesRelativePathAgainstRepoRoot();
};

void TestCookieStore::allowedKeysAreTheThreeWeAccept()
{
    // ⚠ 大小写敏感（`SEESSDATA` 那种笔误 B 站会当没登录）—— 只认这三个，顺序也钉住
    QCOMPARE(CookieStore::allowedKeys(),
             QStringList({QStringLiteral("SESSDATA"), QStringLiteral("bili_jct"),
                          QStringLiteral("DedeUserID")}));
    QCOMPARE(CookieStore::defaultRelativePath(), QStringLiteral("config/bilibili_cookie.json"));
}

void TestCookieStore::readsTheThreeKeysAndIgnoresTheRest()
{
    QTemporaryDir dir;
    QVERIFY(dir.isValid());
    const QString path = dir.filePath(QStringLiteral("cookie.json"));
    QVERIFY(writeFile(path, QStringLiteral(
        "{\n  \"SESSDATA\": \"sess-1\",\n  \"bili_jct\": \"jct-2\",\n"
        "  \"DedeUserID\": \"42\",\n  \"别的键\": \"x\",\n  \"SESSDATA2\": \"y\"\n}\n")));

    CookieStore cookie;
    QVERIFY(cookie.load(path));
    QVERIFY(!cookie.isEmpty());
    QCOMPARE(cookie.value(QStringLiteral("SESSDATA")), QStringLiteral("sess-1"));
    QCOMPARE(cookie.value(QStringLiteral("bili_jct")), QStringLiteral("jct-2"));
    QCOMPARE(cookie.value(QStringLiteral("DedeUserID")), QStringLiteral("42"));
    QVERIFY(!cookie.keys().contains(QStringLiteral("别的键")));
    QVERIFY(!cookie.keys().contains(QStringLiteral("SESSDATA2")));
    QCOMPARE(cookie.keys().size(), 3);
}

void TestCookieStore::missingOrBrokenFileIsAnonymousNotAnError()
{
    QTemporaryDir dir;
    QVERIFY(dir.isValid());

    CookieStore missing;
    QVERIFY(!missing.load(dir.filePath(QStringLiteral("nope.json"))));
    QVERIFY(missing.isEmpty());

    const QString broken = dir.filePath(QStringLiteral("broken.json"));
    QVERIFY(writeFile(broken, QStringLiteral("这不是 JSON\n")));
    CookieStore bad;
    QVERIFY(!bad.load(broken));          // 坏文件 = 没读到（匿名是合法状态）
    QVERIFY(bad.isEmpty());
    QVERIFY(bad.value(QStringLiteral("SESSDATA")).isEmpty());
}

void TestCookieStore::masksForDisplay()
{
    QCOMPARE(CookieStore::mask(QStringLiteral("abc")), QStringLiteral("***"));
    QCOMPARE(CookieStore::mask(QStringLiteral("abcdefghij")),
             QStringLiteral("ab…ij（10 位）"));
    QVERIFY(!CookieStore::mask(QStringLiteral("abcdefghij")).contains(QStringLiteral("cdefgh")));
}

void TestCookieStore::resolvesRelativePathAgainstRepoRoot()
{
    // 相对路径按**仓库根**解析 —— 与 Agent 读的是同一个文件
    QCOMPARE(CookieStore::resolvePath(QStringLiteral("config/bilibili_cookie.json"),
                                      QStringLiteral("/home/kickpi/myproject/assitant")),
             QStringLiteral("/home/kickpi/myproject/assitant/config/bilibili_cookie.json"));
    QCOMPARE(CookieStore::resolvePath(QStringLiteral("/tmp/x.json"), QStringLiteral("/repo")),
             QStringLiteral("/tmp/x.json"));
    QCOMPARE(CookieStore::resolvePath(QString(), QStringLiteral("/repo")),
             QStringLiteral("/repo/") + CookieStore::defaultRelativePath());
}

QTEST_APPLESS_MAIN(TestCookieStore)
#include "test_cookie_store.moc"
