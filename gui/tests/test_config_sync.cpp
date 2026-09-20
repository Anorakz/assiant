// ============================================================================
//  gui/tests/test_config_sync.cpp — 配置同步器单测（方案 §5 映射表）
//
//  验证：gui.yaml 为真源 → llm.env / config.yaml 的映射、云端与本地的 api_key 差异、
//        apply 后注释保住、缺失的 gui 键不同步。
// ============================================================================
#include <QFile>
#include <QTemporaryDir>
#include <QtTest/QtTest>

#include "core/config_store.h"
#include "core/config_sync.h"

using core::ConfigStore;
using core::ConfigSyncer;
using core::Flavor;
using core::LineChange;
using core::SyncPlan;

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

/// 从改动列表里取某个键的新值；没有该键返回 fallback。
QString newValueOf(const QVector<LineChange>& changes, const QString& key,
                   const QString& fallback = QStringLiteral("<无>"))
{
    for (const LineChange& c : changes) {
        if (c.key == key) {
            return c.newLine.section(QLatin1Char(':'), 1).trimmed();
        }
    }
    return fallback;
}

bool hasKey(const QVector<LineChange>& changes, const QString& key)
{
    for (const LineChange& c : changes) {
        if (c.key == key) {
            return true;
        }
    }
    return false;
}

const char* const kEnv = "# ==================== 模型配置 ====================\n"
                         "LLM_MODEL_PATH=/old/model.gguf\n"
                         "LLM_MODEL_NAME=old-name\n"
                         "\n"
                         "# ==================== 服务配置 ====================\n"
                         "LLM_PORT=9000\n"
                         "LLM_API_KEY=old-key\n"
                         "\n"
                         "# ==================== 上下文与性能 ====================\n"
                         "LLM_CTX_SIZE=1024\n"
                         "LLM_BATCH_SIZE=128\n"
                         "LLM_THREADS=2\n"
                         "LLM_THREADS_BATCH=2\n";

const char* const kAgentConfig = "# 全局配置模板\n"
                                 "llm:\n"
                                 "  # 推理位置注释（必须保住）\n"
                                 "  mode: disabled\n"
                                 "  model_path: /old/path.rknn\n"
                                 "  max_tokens: 512\n"
                                 "  temperature: 0.7\n"
                                 "  api_base: https://api.example.com/v1\n"
                                 "  api_key: \"\"\n"
                                 "  model: gpt-4o-mini\n"
                                 "  timeout_s: 30\n";

const char* const kGuiLocal = "llm:\n"
                              "  mode: local\n"
                              "  local_model: /home/kickpi/model/Qwen3-0.6B-Q4_K_M.gguf\n"
                              "  model_name: qwen3-0.6b\n"
                              "  port: 9000\n"
                              "  ctx_size: 2048\n"
                              "  batch_size: 256\n"
                              "  threads: 4\n"
                              "  threads_batch: 4\n"
                              "  api_key: sk-local-secret\n"
                              "  temperature: 0.7\n"
                              "  cloud:\n"
                              "    base: https://cloud.example.com/v1\n"
                              "    model: gpt-4o\n"
                              "    key: cloud-secret\n"
                              "    timeout_s: 45\n";

const char* const kGuiCloud = "llm:\n"
                              "  mode: cloud\n"
                              "  local_model: /home/kickpi/model/Qwen3-0.6B-Q4_K_M.gguf\n"
                              "  model_name: qwen3-0.6b\n"
                              "  port: 9000\n"
                              "  ctx_size: 2048\n"
                              "  batch_size: 256\n"
                              "  threads: 4\n"
                              "  threads_batch: 4\n"
                              "  api_key: sk-local-secret\n"
                              "  temperature: 0.7\n"
                              "  cloud:\n"
                              "    base: https://cloud.example.com/v1\n"
                              "    model: gpt-4o\n"
                              "    key: cloud-secret\n"
                              "    timeout_s: 45\n";

} // namespace

class TestConfigSync : public QObject {
    Q_OBJECT

private slots:
    void localModeMapsAndKeepsAgentApiKeyUntouched();
    void cloudModeWritesCloudKey();
    void applyWritesBothAndKeepsComments();
    void missingGuiKeyIsNotSynced();
};

void TestConfigSync::localModeMapsAndKeepsAgentApiKeyUntouched()
{
    QTemporaryDir dir;
    QVERIFY(dir.isValid());
    const QString envPath = dir.filePath(QStringLiteral("llm.env"));
    const QString cfgPath = dir.filePath(QStringLiteral("config.yaml"));
    QVERIFY(writeFile(envPath, QString::fromUtf8(kEnv)));
    QVERIFY(writeFile(cfgPath, QString::fromUtf8(kAgentConfig)));
    const QString guiPath = dir.filePath(QStringLiteral("gui.yaml"));
    QVERIFY(writeFile(guiPath, QString::fromUtf8(kGuiLocal)));

    ConfigStore gui;
    QVERIFY(gui.load(guiPath));
    ConfigSyncer syncer(envPath, cfgPath);
    const SyncPlan plan = syncer.plan(gui);
    QVERIFY2(plan.ok, qPrintable(plan.error));

    // llm.env：模型与性能参数全部跟着 gui 走
    QVERIFY(hasKey(plan.envChanges, QStringLiteral("LLM_MODEL_PATH")));
    QVERIFY(hasKey(plan.envChanges, QStringLiteral("LLM_CTX_SIZE")));
    QVERIFY(hasKey(plan.envChanges, QStringLiteral("LLM_THREADS_BATCH")));
    QVERIFY(plan.diff.contains(QStringLiteral("LLM_MODEL_PATH")));

    // config.yaml：local -> edge；model_path 写 GGUF
    QCOMPARE(newValueOf(plan.configChanges, QStringLiteral("llm.mode")), QStringLiteral("edge"));
    QCOMPARE(newValueOf(plan.configChanges, QStringLiteral("llm.model_path")),
             QStringLiteral("/home/kickpi/model/Qwen3-0.6B-Q4_K_M.gguf"));
    // 关键：本地模式**不碰** Agent 的 api_key（那是云端的字段）
    QVERIFY(!hasKey(plan.configChanges, QStringLiteral("llm.api_key")));
}

void TestConfigSync::cloudModeWritesCloudKey()
{
    QTemporaryDir dir;
    QVERIFY(dir.isValid());
    const QString envPath = dir.filePath(QStringLiteral("llm.env"));
    const QString cfgPath = dir.filePath(QStringLiteral("config.yaml"));
    QVERIFY(writeFile(envPath, QString::fromUtf8(kEnv)));
    QVERIFY(writeFile(cfgPath, QString::fromUtf8(kAgentConfig)));
    const QString guiPath = dir.filePath(QStringLiteral("gui.yaml"));
    QVERIFY(writeFile(guiPath, QString::fromUtf8(kGuiCloud)));

    ConfigStore gui;
    QVERIFY(gui.load(guiPath));
    ConfigSyncer syncer(envPath, cfgPath);
    const SyncPlan plan = syncer.plan(gui);
    QVERIFY2(plan.ok, qPrintable(plan.error));

    QCOMPARE(newValueOf(plan.configChanges, QStringLiteral("llm.mode")), QStringLiteral("cloud"));
    QCOMPARE(newValueOf(plan.configChanges, QStringLiteral("llm.api_key")),
             QStringLiteral("cloud-secret"));
    QCOMPARE(newValueOf(plan.configChanges, QStringLiteral("llm.api_base")),
             QStringLiteral("https://cloud.example.com/v1"));
    QCOMPARE(newValueOf(plan.configChanges, QStringLiteral("llm.timeout_s")), QStringLiteral("45"));
}

void TestConfigSync::applyWritesBothAndKeepsComments()
{
    QTemporaryDir dir;
    QVERIFY(dir.isValid());
    const QString envPath = dir.filePath(QStringLiteral("llm.env"));
    const QString cfgPath = dir.filePath(QStringLiteral("config.yaml"));
    QVERIFY(writeFile(envPath, QString::fromUtf8(kEnv)));
    QVERIFY(writeFile(cfgPath, QString::fromUtf8(kAgentConfig)));
    const QString guiPath = dir.filePath(QStringLiteral("gui.yaml"));
    QVERIFY(writeFile(guiPath, QString::fromUtf8(kGuiLocal)));

    ConfigStore gui;
    QVERIFY(gui.load(guiPath));
    ConfigSyncer syncer(envPath, cfgPath);
    QString error;
    QString diff;
    QVERIFY2(syncer.apply(gui, &error, &diff), qPrintable(error));
    QVERIFY(!diff.isEmpty());

    const QString envText = readFile(envPath);
    QVERIFY(envText.contains(QStringLiteral("LLM_MODEL_PATH=/home/kickpi/model/Qwen3-0.6B-Q4_K_M.gguf")));
    QVERIFY(envText.contains(QStringLiteral("LLM_CTX_SIZE=2048")));
    QVERIFY(envText.contains(QStringLiteral("# ==================== 上下文与性能 ====================")));

    const QString cfgText = readFile(cfgPath);
    QVERIFY(cfgText.contains(QStringLiteral("\n  mode: edge")));
    QVERIFY(!cfgText.contains(QStringLiteral("llm.mode")));            // 全路径不许泄漏
    QVERIFY(cfgText.contains(QStringLiteral("  # 推理位置注释（必须保住）")));
    QVERIFY(cfgText.contains(QStringLiteral("  max_tokens: 512")));
    QVERIFY(cfgText.contains(QStringLiteral("  api_key: \"\"")));     // 本地模式不动它

    // 两边都留了 .bak，且是改动前的原文
    QCOMPARE(readFile(envPath + QStringLiteral(".bak")), QString::fromUtf8(kEnv));
    QCOMPARE(readFile(cfgPath + QStringLiteral(".bak")), QString::fromUtf8(kAgentConfig));
}

void TestConfigSync::missingGuiKeyIsNotSynced()
{
    QTemporaryDir dir;
    QVERIFY(dir.isValid());
    const QString envPath = dir.filePath(QStringLiteral("llm.env"));
    const QString cfgPath = dir.filePath(QStringLiteral("config.yaml"));
    QVERIFY(writeFile(envPath, QString::fromUtf8(kEnv)));
    QVERIFY(writeFile(cfgPath, QString::fromUtf8(kAgentConfig)));
    const QString guiPath = dir.filePath(QStringLiteral("gui.yaml"));
    // gui 里只给 local_model，其余键都不存在
    QVERIFY(writeFile(guiPath, QStringLiteral("llm:\n  local_model: /only/model.gguf\n")));

    ConfigStore gui;
    QVERIFY(gui.load(guiPath));
    ConfigSyncer syncer(envPath, cfgPath);
    const SyncPlan plan = syncer.plan(gui);
    QVERIFY2(plan.ok, qPrintable(plan.error));

    QCOMPARE(plan.envChanges.size(), 1);                     // 只有 LLM_MODEL_PATH
    QCOMPARE(plan.envChanges.first().key, QStringLiteral("LLM_MODEL_PATH"));
    QVERIFY(plan.configChanges.isEmpty() == false);           // llm.model_path 会同步
    QCOMPARE(plan.configChanges.size(), 1);
    QCOMPARE(plan.configChanges.first().key, QStringLiteral("llm.model_path"));
}

QTEST_APPLESS_MAIN(TestConfigSync)
#include "test_config_sync.moc"
