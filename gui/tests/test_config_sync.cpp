// ============================================================================
//  gui/tests/test_config_sync.cpp — 配置派生单测（config.yaml → llm.env）
//
//  归一化 D 系列：配置真源只有 config/config.yaml，llm.env 是**派生文件**。
//  这里验证：
//    · 8 个键按映射表派生（含 local_api_key → LLM_API_KEY）
//    · 云端参数**不进** llm.env
//    · llm.env 自己的布局键（HOST / LOG_* / RUN_DIR / PID_FILE）不被碰
//    · config.yaml 里没有的键就不同步（"有相同配置项才同步"）
//    · apply 后注释与顺序保住、留 .bak、**不动 config.yaml**
// ============================================================================
#include <QFile>
#include <QTemporaryDir>
#include <QtTest/QtTest>

#include "core/config_store.h"
#include "core/config_sync.h"

using core::ConfigStore;
using core::ConfigSyncer;
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

/// llm.env 的行是 `KEY=value`（没有冒号），所以取值助手按 `=` 切。
/// ⚠ D3 之后 ConfigSyncer **只**派生 llm.env（不再产出 config.yaml 侧的改动），
///   所以这里只需要这一个助手；以前那个按 `:` 切的 newValueOf 已随它一起删除。
QString envValueOf(const QVector<LineChange>& changes, const QString& key,
                   const QString& fallback = QStringLiteral("<无>"))
{
    for (const LineChange& c : changes) {
        if (c.key == key) {
            return c.newLine.section(QLatin1Char('='), 1).trimmed();
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

/// 板端真实的 llm.env 形态（含 5 个不由映射表管的布局键）
const char* const kEnv = "# ==================== 模型配置 ====================\n"
                         "LLM_MODEL_PATH=/old/model.gguf\n"
                         "LLM_MODEL_NAME=old-name\n"
                         "\n"
                         "# ==================== 服务配置 ====================\n"
                         "LLM_HOST=127.0.0.1\n"
                         "LLM_PORT=9000\n"
                         "LLM_API_KEY=old-key\n"
                         "\n"
                         "# ==================== 上下文与性能 ====================\n"
                         "LLM_CTX_SIZE=1024\n"
                         "LLM_BATCH_SIZE=128\n"
                         "LLM_THREADS=2\n"
                         "LLM_THREADS_BATCH=2\n"
                         "\n"
                         "# ==================== 路径 ====================\n"
                         "LLM_LOG_DIR=/tmp/llm/logs\n"
                         "LLM_RUN_DIR=/tmp/llm/run\n"
                         "LLM_PID_FILE=/tmp/llm/run/llama-server.pid\n"
                         "LLM_LOG_FILE=/tmp/llm/logs/llama-server.log\n";

/// config.yaml：真源。llm 段带注释、带云端参数、带 llama-server 一组键。
const char* const kConfig = "# 全局配置模板\n"
                            "llm:\n"
                            "  # 推理位置注释（必须保住）\n"
                            "  mode: edge\n"
                            "  model_path: /home/kickpi/model/Qwen3-0.6B-Q4_K_M.gguf\n"
                            "  max_tokens: 512\n"
                            "  temperature: 0.7\n"
                            "  api_base: https://cloud.example.com/v1\n"
                            "  api_key: cloud-secret\n"
                            "  model: gpt-4o\n"
                            "  timeout_s: 45\n"
                            "  model_name: qwen3-0.6b\n"
                            "  port: 9001\n"
                            "  ctx_size: 2048\n"
                            "  batch_size: 256\n"
                            "  threads: 4\n"
                            "  threads_batch: 5\n"
                            "  local_api_key: sk-local-secret\n"
                            "sunshine:\n"
                            "  host: 192.168.137.1\n";

} // namespace

class TestConfigSync : public QObject {
    Q_OBJECT

private slots:
    void derivesAllMappedKeys();
    void cloudKeysDoNotReachEnv();
    void applyWritesEnvKeepsCommentsAndNeverTouchesConfig();
    void missingKeyIsNotSynced();
    void unknownTargetKeyIsNotFound();
};

void TestConfigSync::derivesAllMappedKeys()
{
    QTemporaryDir dir;
    QVERIFY(dir.isValid());
    const QString envPath = dir.filePath(QStringLiteral("llm.env"));
    const QString cfgPath = dir.filePath(QStringLiteral("config.yaml"));
    QVERIFY(writeFile(envPath, QString::fromUtf8(kEnv)));
    QVERIFY(writeFile(cfgPath, QString::fromUtf8(kConfig)));

    ConfigStore cfg;
    QVERIFY(cfg.load(cfgPath));
    ConfigSyncer syncer(envPath);
    const SyncPlan plan = syncer.plan(cfg);
    QVERIFY2(plan.ok, qPrintable(plan.error));

    // 映射表是 8 个键，这里全都该出现（值都与 llm.env 里的旧值不同）
    QCOMPARE(plan.envChanges.size(), 8);
    QCOMPARE(envValueOf(plan.envChanges, QStringLiteral("LLM_MODEL_PATH")),
             QStringLiteral("/home/kickpi/model/Qwen3-0.6B-Q4_K_M.gguf"));
    QCOMPARE(envValueOf(plan.envChanges, QStringLiteral("LLM_MODEL_NAME")),
             QStringLiteral("qwen3-0.6b"));
    QCOMPARE(envValueOf(plan.envChanges, QStringLiteral("LLM_PORT")), QStringLiteral("9001"));
    QCOMPARE(envValueOf(plan.envChanges, QStringLiteral("LLM_CTX_SIZE")), QStringLiteral("2048"));
    QCOMPARE(envValueOf(plan.envChanges, QStringLiteral("LLM_BATCH_SIZE")), QStringLiteral("256"));
    QCOMPARE(envValueOf(plan.envChanges, QStringLiteral("LLM_THREADS")), QStringLiteral("4"));
    QCOMPARE(envValueOf(plan.envChanges, QStringLiteral("LLM_THREADS_BATCH")), QStringLiteral("5"));
    // 本地 llama-server 的 key 来自 llm.local_api_key（**不是** 云端的 llm.api_key）
    QCOMPARE(envValueOf(plan.envChanges, QStringLiteral("LLM_API_KEY")),
             QStringLiteral("sk-local-secret"));
    QVERIFY(plan.diff.contains(QStringLiteral("LLM_MODEL_PATH")));

    // 映射表本身
    QCOMPARE(ConfigSyncer::envTargetKeys().size(), 8);
}

void TestConfigSync::cloudKeysDoNotReachEnv()
{
    QTemporaryDir dir;
    QVERIFY(dir.isValid());
    const QString envPath = dir.filePath(QStringLiteral("llm.env"));
    const QString cfgPath = dir.filePath(QStringLiteral("config.yaml"));
    QVERIFY(writeFile(envPath, QString::fromUtf8(kEnv)));
    QVERIFY(writeFile(cfgPath, QString::fromUtf8(kConfig)));

    ConfigStore cfg;
    QVERIFY(cfg.load(cfgPath));
    const SyncPlan plan = ConfigSyncer(envPath).plan(cfg);
    QVERIFY2(plan.ok, qPrintable(plan.error));

    // 云端参数是给 Agent 的，不该出现在 llm.env 的改动里
    const QString diffText = plan.diff;
    QVERIFY(!diffText.contains(QStringLiteral("cloud-secret")));
    QVERIFY(!diffText.contains(QStringLiteral("https://cloud.example.com/v1")));
    QVERIFY(!diffText.contains(QStringLiteral("gpt-4o")));
    // 布局键也不在映射表里 → 不在改动里
    for (const QString& untouched : {QStringLiteral("LLM_HOST"), QStringLiteral("LLM_LOG_DIR"),
                                     QStringLiteral("LLM_RUN_DIR"), QStringLiteral("LLM_PID_FILE"),
                                     QStringLiteral("LLM_LOG_FILE")}) {
        QVERIFY2(!hasKey(plan.envChanges, untouched), qPrintable(untouched));
    }
}

void TestConfigSync::applyWritesEnvKeepsCommentsAndNeverTouchesConfig()
{
    QTemporaryDir dir;
    QVERIFY(dir.isValid());
    const QString envPath = dir.filePath(QStringLiteral("llm.env"));
    const QString cfgPath = dir.filePath(QStringLiteral("config.yaml"));
    QVERIFY(writeFile(envPath, QString::fromUtf8(kEnv)));
    QVERIFY(writeFile(cfgPath, QString::fromUtf8(kConfig)));

    ConfigStore cfg;
    QVERIFY(cfg.load(cfgPath));
    QString error;
    QString diff;
    QVERIFY2(ConfigSyncer(envPath).apply(cfg, &error, &diff), qPrintable(error));
    QVERIFY(!diff.isEmpty());

    const QString envText = readFile(envPath);
    QVERIFY(envText.contains(
        QStringLiteral("LLM_MODEL_PATH=/home/kickpi/model/Qwen3-0.6B-Q4_K_M.gguf")));
    QVERIFY(envText.contains(QStringLiteral("LLM_CTX_SIZE=2048")));
    QVERIFY(envText.contains(QStringLiteral("LLM_API_KEY=sk-local-secret")));
    // 注释与顺序保住
    QVERIFY(envText.contains(QStringLiteral("# ==================== 上下文与性能 ====================")));
    QVERIFY(envText.contains(QStringLiteral("# ==================== 路径 ====================")));
    // 布局键原样
    QVERIFY(envText.contains(QStringLiteral("LLM_HOST=127.0.0.1")));
    QVERIFY(envText.contains(QStringLiteral("LLM_LOG_FILE=/tmp/llm/logs/llama-server.log")));

    // .bak 是改动前的原文
    QCOMPARE(readFile(envPath + QStringLiteral(".bak")), QString::fromUtf8(kEnv));

    // ⚠ 关键：config.yaml **一个字节都没动**（它是真源，派生不该反写它）
    QCOMPARE(readFile(cfgPath), QString::fromUtf8(kConfig));
    QVERIFY(!QFile::exists(cfgPath + QStringLiteral(".bak")));
}

void TestConfigSync::missingKeyIsNotSynced()
{
    QTemporaryDir dir;
    QVERIFY(dir.isValid());
    const QString envPath = dir.filePath(QStringLiteral("llm.env"));
    const QString cfgPath = dir.filePath(QStringLiteral("config.yaml"));
    QVERIFY(writeFile(envPath, QString::fromUtf8(kEnv)));
    // config.yaml 里只给 model_path，其余键都不存在
    QVERIFY(writeFile(cfgPath,
                      QStringLiteral("llm:\n  model_path: /only/model.gguf\n")));

    ConfigStore cfg;
    QVERIFY(cfg.load(cfgPath));
    const SyncPlan plan = ConfigSyncer(envPath).plan(cfg);
    QVERIFY2(plan.ok, qPrintable(plan.error));

    QCOMPARE(plan.envChanges.size(), 1);                     // 只有 LLM_MODEL_PATH
    QCOMPARE(plan.envChanges.first().key, QStringLiteral("LLM_MODEL_PATH"));
    QCOMPARE(envValueOf(plan.envChanges, QStringLiteral("LLM_MODEL_PATH")),
             QStringLiteral("/only/model.gguf"));
}

void TestConfigSync::unknownTargetKeyIsNotFound()
{
    QTemporaryDir dir;
    QVERIFY(dir.isValid());
    const QString cfgPath = dir.filePath(QStringLiteral("config.yaml"));
    QVERIFY(writeFile(cfgPath, QString::fromUtf8(kConfig)));
    ConfigStore cfg;
    QVERIFY(cfg.load(cfgPath));

    bool found = true;
    ConfigSyncer::envValue(cfg, QStringLiteral("LLM_NOPE"), &found);
    QVERIFY(!found);
    // 云端的键也不能从这条通道取到（它压根不是 llm.env 的键）
    ConfigSyncer::envValue(cfg, QStringLiteral("llm.api_key"), &found);
    QVERIFY(!found);
}

QTEST_APPLESS_MAIN(TestConfigSync)
#include "test_config_sync.moc"
