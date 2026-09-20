// ============================================================================
//  gui/src/core/config_sync.cpp — 配置同步实现
// ============================================================================
#include "core/config_sync.h"

#include <utility>

namespace core {

namespace {

/// gui 的 mode 取值（local/cloud/disabled）→ config.yaml 的 llm.mode（edge/cloud/disabled）
QString agentMode(const QString& guiMode)
{
    if (guiMode == QLatin1String("local") || guiMode == QLatin1String("edge")
        || guiMode == QLatin1String("board")) {
        return QStringLiteral("edge");
    }
    if (guiMode == QLatin1String("cloud")) {
        return QStringLiteral("cloud");
    }
    return QStringLiteral("disabled");
}

} // namespace

ConfigSyncer::ConfigSyncer(QString envPath, QString agentConfigPath)
    : envPath_(std::move(envPath)), configPath_(std::move(agentConfigPath))
{
}

QStringList ConfigSyncer::envTargetKeys()
{
    return {QStringLiteral("LLM_MODEL_PATH"), QStringLiteral("LLM_MODEL_NAME"),
            QStringLiteral("LLM_PORT"), QStringLiteral("LLM_CTX_SIZE"),
            QStringLiteral("LLM_BATCH_SIZE"), QStringLiteral("LLM_THREADS"),
            QStringLiteral("LLM_THREADS_BATCH"), QStringLiteral("LLM_API_KEY")};
}

QStringList ConfigSyncer::configTargetKeys()
{
    return {QStringLiteral("llm.mode"), QStringLiteral("llm.model_path"),
            QStringLiteral("llm.api_key"), QStringLiteral("llm.api_base"),
            QStringLiteral("llm.model"), QStringLiteral("llm.timeout_s"),
            QStringLiteral("llm.temperature")};
}

QString ConfigSyncer::envValue(const ConfigStore& gui, const QString& targetKey, bool* found)
{
    *found = true;
    const QString one = QStringLiteral("llm.local_model");
    if (targetKey == QLatin1String("LLM_MODEL_PATH")) {
        *found = gui.contains(one);
        return gui.value(one);
    }
    if (targetKey == QLatin1String("LLM_MODEL_NAME")) {
        *found = gui.contains(QStringLiteral("llm.model_name"));
        return gui.value(QStringLiteral("llm.model_name"));
    }
    if (targetKey == QLatin1String("LLM_PORT")) {
        *found = gui.contains(QStringLiteral("llm.port"));
        return gui.value(QStringLiteral("llm.port"));
    }
    if (targetKey == QLatin1String("LLM_CTX_SIZE")) {
        *found = gui.contains(QStringLiteral("llm.ctx_size"));
        return gui.value(QStringLiteral("llm.ctx_size"));
    }
    if (targetKey == QLatin1String("LLM_BATCH_SIZE")) {
        *found = gui.contains(QStringLiteral("llm.batch_size"));
        return gui.value(QStringLiteral("llm.batch_size"));
    }
    if (targetKey == QLatin1String("LLM_THREADS")) {
        *found = gui.contains(QStringLiteral("llm.threads"));
        return gui.value(QStringLiteral("llm.threads"));
    }
    if (targetKey == QLatin1String("LLM_THREADS_BATCH")) {
        *found = gui.contains(QStringLiteral("llm.threads_batch"));
        return gui.value(QStringLiteral("llm.threads_batch"));
    }
    if (targetKey == QLatin1String("LLM_API_KEY")) {
        *found = gui.contains(QStringLiteral("llm.api_key"));
        return gui.value(QStringLiteral("llm.api_key"));
    }
    *found = false;
    return QString();
}

QString ConfigSyncer::configValue(const ConfigStore& gui, const QString& targetKey, bool* found)
{
    *found = true;
    if (targetKey == QLatin1String("llm.mode")) {
        *found = gui.contains(QStringLiteral("llm.mode"));
        return agentMode(gui.value(QStringLiteral("llm.mode")));
    }
    if (targetKey == QLatin1String("llm.model_path")) {
        *found = gui.contains(QStringLiteral("llm.local_model"));
        return gui.value(QStringLiteral("llm.local_model"));
    }
    if (targetKey == QLatin1String("llm.api_key")) {
        // config.yaml 里这个字段是给**云端**用的：只有云端模式才同步 cloud.key。
        // local/disabled 下我们不动它 —— 免得把 cloud 的 key 洗成 llama-server 的 key。
        const QString mode = gui.value(QStringLiteral("llm.mode"));
        if (mode != QLatin1String("cloud")) {
            *found = false;
            return QString();
        }
        *found = gui.contains(QStringLiteral("llm.cloud.key"));
        return gui.value(QStringLiteral("llm.cloud.key"));
    }
    if (targetKey == QLatin1String("llm.api_base")) {
        *found = gui.contains(QStringLiteral("llm.cloud.base"));
        return gui.value(QStringLiteral("llm.cloud.base"));
    }
    if (targetKey == QLatin1String("llm.model")) {
        *found = gui.contains(QStringLiteral("llm.cloud.model"));
        return gui.value(QStringLiteral("llm.cloud.model"));
    }
    if (targetKey == QLatin1String("llm.timeout_s")) {
        *found = gui.contains(QStringLiteral("llm.cloud.timeout_s"));
        return gui.value(QStringLiteral("llm.cloud.timeout_s"));
    }
    if (targetKey == QLatin1String("llm.temperature")) {
        *found = gui.contains(QStringLiteral("llm.temperature"));
        return gui.value(QStringLiteral("llm.temperature"));
    }
    *found = false;
    return QString();
}

bool ConfigSyncer::buildStores(const ConfigStore& gui, ConfigStore* env, ConfigStore* cfg,
                               QString* error) const
{
    if (!env->load(envPath_, error)) {
        return false;
    }
    if (!cfg->load(configPath_, error)) {
        return false;
    }
    for (const QString& targetKey : envTargetKeys()) {
        bool found = false;
        const QString value = envValue(gui, targetKey, &found);
        if (found) {
            env->set(targetKey, value);
        }
    }
    for (const QString& targetKey : configTargetKeys()) {
        bool found = false;
        const QString value = configValue(gui, targetKey, &found);
        if (found) {
            cfg->set(targetKey, value);
        }
    }
    return true;
}

SyncPlan ConfigSyncer::plan(const ConfigStore& gui) const
{
    SyncPlan out;
    ConfigStore env(Flavor::Env);
    ConfigStore cfg(Flavor::Yaml);
    QString error;
    if (!buildStores(gui, &env, &cfg, &error)) {
        out.error = error;
        return out;
    }
    if (!env.planChanges(&out.envChanges, &error)) {
        out.error = QStringLiteral("llm.env: %1").arg(error);
        return out;
    }
    if (!cfg.planChanges(&out.configChanges, &error)) {
        out.error = QStringLiteral("config.yaml: %1").arg(error);
        return out;
    }
    QStringList parts;
    if (!out.envChanges.isEmpty()) {
        parts << ConfigStore::renderDiff(envPath_, out.envChanges);
    }
    if (!out.configChanges.isEmpty()) {
        parts << ConfigStore::renderDiff(configPath_, out.configChanges);
    }
    out.diff = parts.join(QLatin1Char('\n'));
    out.ok = true;
    return out;
}

bool ConfigSyncer::apply(const ConfigStore& gui, QString* error, QString* diffOut) const
{
    ConfigStore env(Flavor::Env);
    ConfigStore cfg(Flavor::Yaml);
    QString localError;
    if (!buildStores(gui, &env, &cfg, &localError)) {
        if (error) {
            *error = localError;
        }
        return false;
    }

    // 先把两边都算好：任一侧算不出来（比如键找不到父块）就整体不写
    QVector<LineChange> envChanges;
    QVector<LineChange> configChanges;
    if (!env.planChanges(&envChanges, &localError)) {
        if (error) {
            *error = QStringLiteral("llm.env: %1").arg(localError);
        }
        return false;
    }
    if (!cfg.planChanges(&configChanges, &localError)) {
        if (error) {
            *error = QStringLiteral("config.yaml: %1").arg(localError);
        }
        return false;
    }

    if (diffOut) {
        QStringList parts;
        if (!envChanges.isEmpty()) {
            parts << ConfigStore::renderDiff(envPath_, envChanges);
        }
        if (!configChanges.isEmpty()) {
            parts << ConfigStore::renderDiff(configPath_, configChanges);
        }
        *diffOut = parts.join(QLatin1Char('\n'));
    }

    if (!env.save(&localError)) {
        if (error) {
            *error = localError;
        }
        return false;
    }
    if (!cfg.save(&localError)) {
        if (error) {
            *error = QStringLiteral("%1（llm.env 已写入）").arg(localError);
        }
        return false;
    }
    return true;
}

} // namespace core
