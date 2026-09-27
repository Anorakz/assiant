// ============================================================================
//  gui/tests/test_config_store.cpp — ConfigStore 单测（**只读 + 预览**，T14-3）
//
//  ⚠ T14-3 起这个类**不写文件**了：GUI 的写入走 IPC 让 Agent 做（docs/adr/0005）。
//    所以这里盯两件事：
//      · **读**：按缩进栈把 `a.b.c` 标量收进索引（含行尾注释的切分）；
//      · **预览**：`set()` 只记意图，`planChanges()` 说"会改哪一行" —— 一行都不落盘。
// ============================================================================
#include <QCoreApplication>
#include <QDir>
#include <QFile>
#include <QTemporaryDir>
#include <QtTest/QtTest>

#include "core/config_store.h"

using core::ConfigStore;
using core::LineChange;

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

const char* const kYaml = "# 顶层注释\n"
                          "llm:\n"
                          "  # 字段约定注释（读的时候要跳过）\n"
                          "  mode: disabled\n"
                          "  model_path: /old/path.rknn\n"
                          "  cloud:\n"
                          "    base: https://api.example.com/v1\n"
                          "    key: \"\"\n"
                          "\n"
                          "other:\n"
                          "  flag: true   # 行尾注释\n";

} // namespace

class TestConfigStore : public QObject {
    Q_OBJECT

private slots:
    void yamlReadsNestedScalars();
    void loadMissingFileFails();
    void inMemorySetIsVisibleWithoutAnyFile();
    void pendingValuesAreTheRequestPayload();
    void planChangesOnlyTouchesTheTargetLine();
    void planChangesKeepsTheTrailingCommentAlignment();
    void planChangesInsertsMissingKeyUnderParent();
    void planChangesRefusesWhenTheWholeBlockIsMissing();
    void planChangesQuotesTrickyValues();
    void planChangesSkipsValuesThatDidNotChange();
    void previewNeverWritesTheFile();
};

void TestConfigStore::yamlReadsNestedScalars()
{
    QTemporaryDir dir;
    QVERIFY(dir.isValid());
    const QString path = dir.filePath(QStringLiteral("a.yaml"));
    QVERIFY(writeFile(path, QString::fromUtf8(kYaml)));

    ConfigStore store;
    QString error;
    QVERIFY2(store.load(path, &error), qPrintable(error));
    QVERIFY(store.loaded());
    QCOMPARE(store.path(), path);

    QCOMPARE(store.value(QStringLiteral("llm.mode")), QStringLiteral("disabled"));
    QCOMPARE(store.value(QStringLiteral("llm.model_path")), QStringLiteral("/old/path.rknn"));
    QCOMPARE(store.value(QStringLiteral("llm.cloud.base")),
             QStringLiteral("https://api.example.com/v1"));
    QCOMPARE(store.value(QStringLiteral("llm.cloud.key")), QString());
    const bool otherFlag = store.boolValue(QStringLiteral("other.flag"), false);
    QCOMPARE(otherFlag, true);
    // 行尾注释不算值的一部分
    QCOMPARE(store.value(QStringLiteral("other.flag")), QStringLiteral("true"));
    QVERIFY(!store.contains(QStringLiteral("llm.nope")));
    QCOMPARE(store.value(QStringLiteral("llm.nope"), QStringLiteral("默认")),
             QStringLiteral("默认"));
    // ⚠ QCOMPARE 是宏：调用里带逗号会把它当成两个参数 -> 先取出来再比
    const int missingInt = store.intValue(QStringLiteral("llm.nope"), 7);
    QCOMPARE(missingInt, 7);
    const double missingDouble = store.doubleValue(QStringLiteral("llm.nope"), 0.5);
    QCOMPARE(missingDouble, 0.5);
}

void TestConfigStore::loadMissingFileFails()
{
    // 不存在的路径必须**失败**：否则调用方会拿满屏默认值当"读到了"
    const QString path = QDir::tempPath() + QStringLiteral("/t14_3_missing_%1.yaml")
                                             .arg(QCoreApplication::applicationPid());
    QFile::remove(path);
    ConfigStore store;
    QString error;
    QVERIFY(!store.load(path, &error));
    QVERIFY(!error.isEmpty());
    QVERIFY(!store.loaded());
    QVERIFY(!QFile::exists(path));
}

void TestConfigStore::inMemorySetIsVisibleWithoutAnyFile()
{
    // set() 只是"写入意图"（要发给 Agent 的），但读取应该立刻看得到
    ConfigStore store;
    const bool debugBefore = store.boolValue(QStringLiteral("gui.debug"), false);
    QCOMPARE(debugBefore, false);
    store.setBool(QStringLiteral("gui.debug"), true);
    QCOMPARE(store.value(QStringLiteral("gui.debug")), QStringLiteral("true"));
    const bool debugAfter = store.boolValue(QStringLiteral("gui.debug"), false);
    QCOMPARE(debugAfter, true);
    QVERIFY(store.contains(QStringLiteral("gui.debug")));
    QVERIFY(store.hasPending());
}

void TestConfigStore::pendingValuesAreTheRequestPayload()
{
    ConfigStore store;
    store.setBool(QStringLiteral("study.enabled"), true);
    store.set(QStringLiteral("study.relative_band"), QStringLiteral("0.07"));
    store.set(QStringLiteral("study.enabled"), QStringLiteral("false"));   // 覆盖同一个键

    // ⚠ QCOMPARE 是宏：第 3 个参数不行（宏只吃两个）—— 说明写在上一行的注释里
    QCOMPARE(store.pendingKeys(),
             QStringList({QStringLiteral("study.enabled"), QStringLiteral("study.relative_band")}));
    const QHash<QString, QString> values = store.pendingValues();
    QCOMPARE(values.value(QStringLiteral("study.enabled")), QStringLiteral("false"));
    QCOMPARE(values.value(QStringLiteral("study.relative_band")), QStringLiteral("0.07"));
    QCOMPARE(values.size(), 2);

    store.clearPending();
    QVERIFY(!store.hasPending());
    QVERIFY(store.pendingValues().isEmpty());
}

void TestConfigStore::planChangesOnlyTouchesTheTargetLine()
{
    QTemporaryDir dir;
    QVERIFY(dir.isValid());
    const QString path = dir.filePath(QStringLiteral("a.yaml"));
    QVERIFY(writeFile(path, QString::fromUtf8(kYaml)));

    ConfigStore store;
    QVERIFY(store.load(path));
    store.set(QStringLiteral("llm.model_path"), QStringLiteral("/new/path.rknn"));

    QVector<LineChange> changes;
    QString error;
    QVERIFY2(store.planChanges(&changes, &error), qPrintable(error));
    QCOMPARE(changes.size(), 1);
    const LineChange change = changes.first();
    QCOMPARE(change.key, QStringLiteral("llm.model_path"));
    QCOMPARE(change.lineNo, 5);                                    // 1-based
    QCOMPARE(change.oldLine, QStringLiteral("  model_path: /old/path.rknn"));
    QCOMPARE(change.newLine, QStringLiteral("  model_path: /new/path.rknn"));
    QVERIFY(change.oldLine.startsWith(change.newLine.left(2)));    // 缩进原样
    QVERIFY2(ConfigStore::renderDiff(path, changes).contains(
                 QStringLiteral("-   model_path: /old/path.rknn")),
             qPrintable(ConfigStore::renderDiff(path, changes)));
}

void TestConfigStore::planChangesKeepsTheTrailingCommentAlignment()
{
    // 预览里的新行也要把行尾注释**连对齐空白**留在原地（否则看着像把行改丑了）
    ConfigStore store;
    const QString path = QDir::tempPath() + QStringLiteral("/t14_3_align.yaml");
    QVERIFY(writeFile(path, QStringLiteral("llm:\n  mode: disabled      # 对齐的注释\n")));
    QVERIFY(store.load(path));
    store.set(QStringLiteral("llm.mode"), QStringLiteral("cloud"));

    QVector<LineChange> changes;
    QVERIFY(store.planChanges(&changes));
    QCOMPARE(changes.first().newLine, QStringLiteral("  mode: cloud      # 对齐的注释"));
    QFile::remove(path);
}

void TestConfigStore::planChangesInsertsMissingKeyUnderParent()
{
    ConfigStore store;
    const QString path = QDir::tempPath() + QStringLiteral("/t14_3_insert.yaml");
    QVERIFY(writeFile(path, QStringLiteral("llm:\n  mode: disabled\nother:\n  flag: true\n")));
    QVERIFY(store.load(path));
    store.set(QStringLiteral("llm.port"), QStringLiteral("9000"));

    QVector<LineChange> changes;
    QString error;
    QVERIFY2(store.planChanges(&changes, &error), qPrintable(error));
    QCOMPARE(changes.size(), 1);
    QCOMPARE(changes.first().lineNo, -1);                                  // 新增
    QCOMPARE(changes.first().newLine, QStringLiteral("  port: 9000"));     // 父块缩进 + 2
    QVERIFY(changes.first().insertAt < 4);                                 // 插在 llm 块末尾、other 之前
    QFile::remove(path);
}

void TestConfigStore::planChangesRefusesWhenTheWholeBlockIsMissing()
{
    // **整段/整块的新建由 Agent 按模板做**（GUI 只预览已存在的行）—— 这里要说清为什么
    ConfigStore store;
    const QString path = QDir::tempPath() + QStringLiteral("/t14_3_noparent.yaml");
    QVERIFY(writeFile(path, QStringLiteral("llm:\n  mode: disabled\n")));
    QVERIFY(store.load(path));
    store.set(QStringLiteral("study.enabled"), QStringLiteral("true"));

    QVector<LineChange> changes;
    QString error;
    QVERIFY(!store.planChanges(&changes, &error));
    QVERIFY2(error.contains(QStringLiteral("Agent")), qPrintable(error));
    QFile::remove(path);
}

void TestConfigStore::planChangesQuotesTrickyValues()
{
    ConfigStore store;
    const QString path = QDir::tempPath() + QStringLiteral("/t14_3_quote.yaml");
    QVERIFY(writeFile(path, QStringLiteral("llm:\n  mode: disabled\n")));
    QVERIFY(store.load(path));
    store.set(QStringLiteral("llm.note"), QStringLiteral("a # b"));   // 值里有 " #"
    store.set(QStringLiteral("llm.empty"), QString());

    QVector<LineChange> changes;
    QVERIFY(store.planChanges(&changes));
    QCOMPARE(changes.size(), 2);
    QVERIFY(changes.first().newLine.contains(QStringLiteral("\"a # b\"")));
    QVERIFY(changes.last().newLine.endsWith(QStringLiteral("\"\"")));
    QFile::remove(path);
}

void TestConfigStore::planChangesSkipsValuesThatDidNotChange()
{
    QTemporaryDir dir;
    QVERIFY(dir.isValid());
    const QString path = dir.filePath(QStringLiteral("a.yaml"));
    QVERIFY(writeFile(path, QString::fromUtf8(kYaml)));
    ConfigStore store;
    QVERIFY(store.load(path));

    store.set(QStringLiteral("llm.mode"), QStringLiteral("disabled"));   // 与文件里一样
    store.set(QStringLiteral("other.flag"), QStringLiteral("false"));    // 真的变了
    QVector<LineChange> changes;
    QVERIFY(store.planChanges(&changes));
    QCOMPARE(changes.size(), 1);
    QCOMPARE(changes.first().key, QStringLiteral("other.flag"));
}

void TestConfigStore::previewNeverWritesTheFile()
{
    QTemporaryDir dir;
    QVERIFY(dir.isValid());
    const QString path = dir.filePath(QStringLiteral("a.yaml"));
    QVERIFY(writeFile(path, QString::fromUtf8(kYaml)));
    const QString before = readFile(path);

    ConfigStore store;
    QVERIFY(store.load(path));
    store.set(QStringLiteral("llm.mode"), QStringLiteral("cloud"));
    QVector<LineChange> changes;
    QVERIFY(store.planChanges(&changes));
    ConfigStore::renderDiff(path, changes);

    QCOMPARE(readFile(path), before);                       // 一个字节都没动
    QVERIFY(!QFile::exists(path + QStringLiteral(".bak"))); // 也没有 .bak
    QVERIFY(!QFile::exists(path + QStringLiteral(".tmp")));
}

QTEST_APPLESS_MAIN(TestConfigStore)
#include "test_config_store.moc"
