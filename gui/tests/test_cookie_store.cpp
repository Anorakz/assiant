// ============================================================================
//  gui/tests/test_cookie_store.cpp — B 站凭据文件单测（T13-9）
//
//  验：只认三个键（笔误拒绝）、合并写、空值不改、`.bak` + 原子写、JSON 形状
//  （两空格缩进 + 结尾换行，与 Python 侧同一份文件对得上）、路径解析按仓库根。
//  全部用临时目录，不碰仓库里那份真凭据。
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

QString readFile(const QString& path)
{
    QFile file(path);
    if (!file.open(QIODevice::ReadOnly)) {
        return QString();
    }
    return QString::fromUtf8(file.readAll());
}

} // namespace

class TestCookieStore : public QObject {
    Q_OBJECT

private slots:
    void typoKeyIsRefused();
    void mergesAndKeepsOtherKeys();
    void jsonShapeMatchesPython();
    void emptyValueDoesNotTouchTheKey();
    void backupsTheOriginal();
    void brokenFileReadsAsAnonymous();
    void masksForDisplay();
    void resolvesRelativePathAgainstRepoRoot();
    void refusesNewlines();
};

void TestCookieStore::typoKeyIsRefused()
{
    QTemporaryDir dir;
    QVERIFY(dir.isValid());
    const QString path = dir.filePath(QStringLiteral("bilibili_cookie.json"));

    CookieStore cookie;
    QString error;
    // ⚠ 实测踩过：写成 SEESSDATA（多一个 E）B 站不认（nav 回 -101）→ 这里直接拒绝
    QVERIFY(!cookie.set(QStringLiteral("SEESSDATA"), QStringLiteral("x"), &error));
    QVERIFY(error.contains(QStringLiteral("SESSDATA")));
    // 被拒之后什么都没记下：保存不该写出文件
    bool wrote = true;
    QVERIFY(cookie.save(path, &error, &wrote));
    QVERIFY(!wrote);
    QVERIFY(!QFile::exists(path));
}

void TestCookieStore::mergesAndKeepsOtherKeys()
{
    QTemporaryDir dir;
    QVERIFY(dir.isValid());
    const QString path = dir.filePath(QStringLiteral("bilibili_cookie.json"));
    QVERIFY(writeFile(path, QStringLiteral(
        "{\n"
        "  \"SESSDATA\": \"sess-old\",\n"
        "  \"bili_jct\": \"jct-old\",\n"
        "  \"DedeUserID\": \"42\"\n"
        "}\n")));

    // 只改一个键：别的键必须原样留着（"合并写"）
    CookieStore cookie;
    QVERIFY(cookie.set(QStringLiteral("SESSDATA"), QStringLiteral("sess-new")));
    QString error;
    bool wrote = false;
    QVERIFY2(cookie.save(path, &error, &wrote), qPrintable(error));
    QVERIFY(wrote);

    CookieStore after;
    QVERIFY(after.load(path));
    QCOMPARE(after.value(QStringLiteral("SESSDATA")), QStringLiteral("sess-new"));
    QCOMPARE(after.value(QStringLiteral("bili_jct")), QStringLiteral("jct-old"));
    QCOMPARE(after.value(QStringLiteral("DedeUserID")), QStringLiteral("42"));
}

void TestCookieStore::jsonShapeMatchesPython()
{
    QTemporaryDir dir;
    QVERIFY(dir.isValid());
    const QString path = dir.filePath(QStringLiteral("bilibili_cookie.json"));

    CookieStore cookie;
    QVERIFY(cookie.set(QStringLiteral("DedeUserID"), QStringLiteral("42")));
    QVERIFY(cookie.set(QStringLiteral("SESSDATA"), QStringLiteral("a\"b\\c")));
    QString error;
    QVERIFY2(cookie.save(path, &error), qPrintable(error));

    // 两空格缩进 + 结尾换行 + 键按 ALLOWED_KEYS 顺序（与 settings_credentials.py 一致）
    QCOMPARE(readFile(path),
             QStringLiteral("{\n"
                            "  \"SESSDATA\": \"a\\\"b\\\\c\",\n"
                            "  \"DedeUserID\": \"42\"\n"
                            "}\n"));
}

void TestCookieStore::emptyValueDoesNotTouchTheKey()
{
    QTemporaryDir dir;
    QVERIFY(dir.isValid());
    const QString path = dir.filePath(QStringLiteral("bilibili_cookie.json"));
    const QString original = QStringLiteral("{\n  \"SESSDATA\": \"keep-me\"\n}\n");
    QVERIFY(writeFile(path, original));

    CookieStore cookie;
    QVERIFY(cookie.set(QStringLiteral("SESSDATA"), QString()));   // 界面留空 = 不改动
    QString error;
    bool wrote = true;
    QVERIFY2(cookie.save(path, &error, &wrote), qPrintable(error));
    QVERIFY(!wrote);                                  // 没有待写值 → 不写文件、不留 .bak
    QCOMPARE(readFile(path), original);
    QVERIFY(!QFile::exists(path + QStringLiteral(".bak")));
}

void TestCookieStore::backupsTheOriginal()
{
    QTemporaryDir dir;
    QVERIFY(dir.isValid());
    const QString path = dir.filePath(QStringLiteral("bilibili_cookie.json"));
    const QString original = QStringLiteral("{\n  \"SESSDATA\": \"old\"\n}\n");
    QVERIFY(writeFile(path, original));

    CookieStore cookie;
    QVERIFY(cookie.set(QStringLiteral("SESSDATA"), QStringLiteral("new")));
    QVERIFY(cookie.save(path));
    QCOMPARE(readFile(path + QStringLiteral(".bak")), original);   // 备份是改动前的原文
    QVERIFY(!QFile::exists(path + QStringLiteral(".tmp")));        // 临时文件不能留下
}

void TestCookieStore::brokenFileReadsAsAnonymous()
{
    QTemporaryDir dir;
    QVERIFY(dir.isValid());
    const QString path = dir.filePath(QStringLiteral("bilibili_cookie.json"));
    QVERIFY(writeFile(path, QStringLiteral("这不是 JSON\n")));

    CookieStore cookie;
    QVERIFY(!cookie.load(path));          // 坏文件 = 没读到（匿名是合法状态，不是错误）
    QVERIFY(cookie.isEmpty());

    // 坏文件照样能覆盖写（写之前留 .bak）
    QVERIFY(cookie.set(QStringLiteral("SESSDATA"), QStringLiteral("fresh")));
    QString error;
    QVERIFY2(cookie.save(path, &error), qPrintable(error));
    CookieStore after;
    QVERIFY(after.load(path));
    QCOMPARE(after.value(QStringLiteral("SESSDATA")), QStringLiteral("fresh"));
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
    // 相对路径按**仓库根**解析（与 agent/core/settings_credentials.py 同一个文件）
    QCOMPARE(CookieStore::resolvePath(QStringLiteral("config/bilibili_cookie.json"),
                                      QStringLiteral("/home/kickpi/myproject/assitant")),
             QStringLiteral("/home/kickpi/myproject/assitant/config/bilibili_cookie.json"));
    QCOMPARE(CookieStore::resolvePath(QStringLiteral("/tmp/x.json"), QStringLiteral("/repo")),
             QStringLiteral("/tmp/x.json"));
    // 空 = 用默认位置
    QCOMPARE(CookieStore::resolvePath(QString(), QStringLiteral("/repo")),
             QStringLiteral("/repo/") + CookieStore::defaultRelativePath());
}

void TestCookieStore::refusesNewlines()
{
    CookieStore cookie;
    QString error;
    QVERIFY(!cookie.set(QStringLiteral("SESSDATA"), QStringLiteral("a\nb"), &error));
    QVERIFY(!error.isEmpty());
}

QTEST_APPLESS_MAIN(TestCookieStore)
#include "test_cookie_store.moc"
