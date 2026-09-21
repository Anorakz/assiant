// ============================================================================
//  gui/tests/test_config_store.cpp — ConfigStore 单测（文本级键路径替换）
//
//  重点验证"保注释、保顺序、只动目标行"，以及出错时**拒绝写入**。
// ============================================================================
#include <QCoreApplication>
#include <QDir>
#include <QFile>
#include <QTemporaryDir>
#include <QtTest/QtTest>

#include "core/config_store.h"

using core::ConfigStore;
using core::Flavor;
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
                          "  # 字段约定注释（必须保住）\n"
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
    void yamlKeepsCommentsOnSave();
    void yamlBacksUpOriginal();
    void yamlInsertsMissingKeyUnderParent();
    void yamlRefusesWhenParentMissing();
    void yamlQuotesTrickyValues();
    void inMemorySetIsVisibleBeforeSave();
    void loadMissingFileFails();
    void envFlavorRoundTrip();
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

    QCOMPARE(store.value(QStringLiteral("llm.mode")), QStringLiteral("disabled"));
    QCOMPARE(store.value(QStringLiteral("llm.model_path")), QStringLiteral("/old/path.rknn"));
    QCOMPARE(store.value(QStringLiteral("llm.cloud.base")),
             QStringLiteral("https://api.example.com/v1"));
    QCOMPARE(store.value(QStringLiteral("llm.cloud.key")), QString());
    QCOMPARE(store.boolValue(QStringLiteral("other.flag"), false), true);
    QCOMPARE(store.value(QStringLiteral("不存在")), QString());
    QCOMPARE(store.value(QStringLiteral("llm.nope"), QStringLiteral("默认")),
             QStringLiteral("默认"));
}

void TestConfigStore::yamlKeepsCommentsOnSave()
{
    QTemporaryDir dir;
    QVERIFY(dir.isValid());
    const QString path = dir.filePath(QStringLiteral("a.yaml"));
    QVERIFY(writeFile(path, QString::fromUtf8(kYaml)));

    ConfigStore store;
    QVERIFY(store.load(path));
    store.set(QStringLiteral("llm.model_path"),
              QStringLiteral("/home/kickpi/model/Qwen3-0.6B-Q4_K_M.gguf"));
    QString error;
    QVERIFY2(store.save(&error), qPrintable(error));

    const QString text = readFile(path);
    // 只动了目标行（注意断言用"两空格 + 键名"，能抓住"把全路径写进文件"这类错误）
    QVERIFY(text.contains(QStringLiteral("\n  model_path: /home/kickpi/model/Qwen3-0.6B-Q4_K_M.gguf")));
    QVERIFY(!text.contains(QStringLiteral("llm.model_path")));        // 全路径不许泄漏进文件
    QVERIFY(text.contains(QStringLiteral("  # 字段约定注释（必须保住）")));
    QVERIFY(text.contains(QStringLiteral("# 顶层注释")));
    QVERIFY(text.contains(QStringLiteral("other:")));
    QVERIFY(text.contains(QStringLiteral("  flag: true   # 行尾注释")));   // 行尾注释也保住
    QVERIFY(text.contains(QStringLiteral("  mode: disabled")));           // 未涉及的键原样

    // 行数不变（替换而不是重排）
    QCOMPARE(text.count(QLatin1Char('\n')), QString::fromUtf8(kYaml).count(QLatin1Char('\n')));
    // 重新索引后能读到新值
    QCOMPARE(store.value(QStringLiteral("llm.model_path")),
             QStringLiteral("/home/kickpi/model/Qwen3-0.6B-Q4_K_M.gguf"));
    QVERIFY(!store.hasPending());
}

void TestConfigStore::yamlBacksUpOriginal()
{
    QTemporaryDir dir;
    QVERIFY(dir.isValid());
    const QString path = dir.filePath(QStringLiteral("a.yaml"));
    QVERIFY(writeFile(path, QString::fromUtf8(kYaml)));

    ConfigStore store;
    QVERIFY(store.load(path));
    store.set(QStringLiteral("llm.mode"), QStringLiteral("cloud"));
    QVERIFY(store.save());

    const QString bak = readFile(path + QStringLiteral(".bak"));
    QCOMPARE(bak, QString::fromUtf8(kYaml));       // 备份是改动前的原文
    QVERIFY(readFile(path).contains(QStringLiteral("  mode: cloud")));
}

void TestConfigStore::yamlInsertsMissingKeyUnderParent()
{
    QTemporaryDir dir;
    QVERIFY(dir.isValid());
    const QString path = dir.filePath(QStringLiteral("a.yaml"));
    QVERIFY(writeFile(path, QStringLiteral("llm:\n  mode: disabled\nother:\n  flag: true\n")));

    ConfigStore store;
    QVERIFY(store.load(path));
    store.set(QStringLiteral("llm.port"), QStringLiteral("9000"));

    QVector<LineChange> changes;
    QString error;
    QVERIFY2(store.planChanges(&changes, &error), qPrintable(error));
    QCOMPARE(changes.size(), 1);
    QCOMPARE(changes.first().lineNo, -1);                       // 新增
    QCOMPARE(changes.first().newLine, QStringLiteral("  port: 9000"));   // 父块缩进 + 2

    QVERIFY(store.save());
    const QString text = readFile(path);
    QVERIFY(text.contains(QStringLiteral("  port: 9000")));
    // 插在 llm 块里、other 之前
    QVERIFY(text.indexOf(QStringLiteral("  port: 9000"))
            < text.indexOf(QStringLiteral("other:")));
    QCOMPARE(store.value(QStringLiteral("llm.port")), QStringLiteral("9000"));
}

void TestConfigStore::yamlRefusesWhenParentMissing()
{
    QTemporaryDir dir;
    QVERIFY(dir.isValid());
    const QString path = dir.filePath(QStringLiteral("a.yaml"));
    const QString original = QStringLiteral("llm:\n  mode: disabled\n");
    QVERIFY(writeFile(path, original));

    ConfigStore store;
    QVERIFY(store.load(path));
    store.set(QStringLiteral("nosuchblock.key"), QStringLiteral("x"));

    QString error;
    QVERIFY(!store.save(&error));
    QVERIFY(error.contains(QStringLiteral("拒绝写入")));
    QCOMPARE(readFile(path), original);                          // 文件一个字节没动
    QVERIFY(!QFile::exists(path + QStringLiteral(".bak")));       // 也没留备份
}

void TestConfigStore::yamlQuotesTrickyValues()
{
    QTemporaryDir dir;
    QVERIFY(dir.isValid());
    const QString path = dir.filePath(QStringLiteral("a.yaml"));
    QVERIFY(writeFile(path, QStringLiteral("llm:\n  mode: disabled\n")));

    ConfigStore store;
    QVERIFY(store.load(path));
    store.set(QStringLiteral("llm.empty"), QString());
    store.set(QStringLiteral("llm.commentish"), QStringLiteral("a # b"));
    QVERIFY(store.save());

    ConfigStore reread;
    QVERIFY(reread.load(path));
    QCOMPARE(reread.value(QStringLiteral("llm.empty")), QString());
    QCOMPARE(reread.value(QStringLiteral("llm.commentish")), QStringLiteral("a # b"));
}

// set() 只是"写入意图"，但读取应该立刻看得到新值（--debug 这类内存覆盖依赖它）
void TestConfigStore::inMemorySetIsVisibleBeforeSave()
{
    QTemporaryDir dir;
    QVERIFY(dir.isValid());
    const QString path = dir.filePath(QStringLiteral("config.yaml"));
    QVERIFY(writeFile(path, QStringLiteral("debug: false\nwake:\n  idle_ms: 5000\n")));

    ConfigStore store;
    QVERIFY(store.load(path));
    QCOMPARE(store.boolValue(QStringLiteral("debug"), false), false);

    store.setBool(QStringLiteral("debug"), true);
    QCOMPARE(store.value(QStringLiteral("debug")), QStringLiteral("true"));
    QCOMPARE(store.boolValue(QStringLiteral("debug"), false), true);      // 未落盘也可见
    QVERIFY(store.contains(QStringLiteral("debug")));

    store.set(QStringLiteral("wake.idle_ms"), QStringLiteral("2500"));
    QCOMPARE(store.intValue(QStringLiteral("wake.idle_ms"), 5000), 2500);

    // 新增的键同样立刻可读
    store.set(QStringLiteral("theme"), QStringLiteral("grey"));
    QCOMPARE(store.value(QStringLiteral("theme")), QStringLiteral("grey"));

    // 文件此刻还没变
    QVERIFY(readFile(path).contains(QStringLiteral("debug: false")));

    QVERIFY(store.save());
    QVERIFY(readFile(path).contains(QStringLiteral("debug: true")));
    QCOMPARE(store.boolValue(QStringLiteral("debug"), false), true);
}

void TestConfigStore::envFlavorRoundTrip()
{
    QTemporaryDir dir;
    QVERIFY(dir.isValid());
    const QString path = dir.filePath(QStringLiteral("llm.env"));
    const QString original = QStringLiteral(
        "# 分节注释\n"
        "LLM_MODEL_PATH=/home/kickpi/model/Qwen3-0.6B-Q4_K_M.gguf\n"
        "LLM_MODEL_NAME=qwen3-0.6b\n"
        "\n"
        "LLM_PORT=9000\n");
    QVERIFY(writeFile(path, original));

    ConfigStore env(Flavor::Env);
    QString error;
    QVERIFY2(env.load(path, &error), qPrintable(error));
    QCOMPARE(env.value(QStringLiteral("LLM_PORT")), QStringLiteral("9000"));

    env.set(QStringLiteral("LLM_MODEL_NAME"), QStringLiteral("qwen3.5-0.8b"));
    env.set(QStringLiteral("LLM_NEW"), QStringLiteral("1"));     // 新键 → 追加到末尾
    QVERIFY2(env.save(&error), qPrintable(error));

    const QString text = readFile(path);
    QVERIFY(text.contains(QStringLiteral("LLM_MODEL_NAME=qwen3.5-0.8b")));
    QVERIFY(text.contains(QStringLiteral("# 分节注释")));
    QVERIFY(text.contains(QStringLiteral("LLM_MODEL_PATH=/home/kickpi/model/Qwen3-0.6B-Q4_K_M.gguf")));
    QCOMPARE(text.count(QLatin1Char('\n')), original.count(QLatin1Char('\n')) + 1);
}

void TestConfigStore::loadMissingFileFails()
{
    // 不存在的路径必须**失败**：否则调用方 set+save 会造出只有个别键的残桩配置，
    // 把真正的 config/config.yaml 顶掉（T9 出图时真的发生过）。
    const QString path = QDir::tempPath() + QStringLiteral("/config_missing_%1.yaml")
                                             .arg(QCoreApplication::applicationPid());
    QFile::remove(path);
    core::ConfigStore store;
    QString error;
    QVERIFY(!store.load(path, &error));
    QVERIFY(!error.isEmpty());
    QVERIFY(!QFile::exists(path));          // 加载失败不许顺手创建文件

    // 只有真加载成功了，save 才可能写出文件
    store.set(QStringLiteral("k"), QStringLiteral("v"));   // set() 返回 void
    QVERIFY(!store.save(&error));           // 没加载过 → 不写
    QVERIFY(!QFile::exists(path));
}

QTEST_APPLESS_MAIN(TestConfigStore)
#include "test_config_store.moc"
