// ============================================================================
//  gui/src/core/config_sync.cpp — config.yaml → llm.env 的派生实现
// ============================================================================
#include "core/config_sync.h"

#include <utility>

namespace core {

ConfigSyncer::ConfigSyncer(QString envPath)
    : envPath_(std::move(envPath))
{
}

QStringList ConfigSyncer::envTargetKeys()
{
    return {QStringLiteral("LLM_MODEL_PATH"), QStringLiteral("LLM_MODEL_NAME"),
            QStringLiteral("LLM_PORT"), QStringLiteral("LLM_CTX_SIZE"),
            QStringLiteral("LLM_BATCH_SIZE"), QStringLiteral("LLM_THREADS"),
            QStringLiteral("LLM_THREADS_BATCH"), QStringLiteral("LLM_API_KEY")};
}

QString ConfigSyncer::envValue(const ConfigStore& cfg, const QString& targetKey, bool* found)
{
    // targetKey(llm.env 的键) -> config.yaml 顶层 llm.* 的键
    static const struct { const char* env; const char* yaml; } kMap[] = {
        {"LLM_MODEL_PATH", "llm.model_path"},
        {"LLM_MODEL_NAME", "llm.model_name"},
        {"LLM_PORT", "llm.port"},
        {"LLM_CTX_SIZE", "llm.ctx_size"},
        {"LLM_BATCH_SIZE", "llm.batch_size"},
        {"LLM_THREADS", "llm.threads"},
        {"LLM_THREADS_BATCH", "llm.threads_batch"},
        {"LLM_API_KEY", "llm.local_api_key"},
    };

    for (const auto& pair : kMap) {
        if (targetKey == QLatin1String(pair.env)) {
            const QString source = QString::fromLatin1(pair.yaml);
            *found = cfg.contains(source);
            return cfg.value(source);
        }
    }
    *found = false;
    return QString();
}

bool ConfigSyncer::buildEnv(const ConfigStore& cfg, ConfigStore* env, QString* error) const
{
    if (!env->load(envPath_, error)) {
        return false;
    }
    for (const QString& targetKey : envTargetKeys()) {
        bool found = false;
        const QString value = envValue(cfg, targetKey, &found);
        if (found) {
            env->set(targetKey, value);
        }
    }
    return true;
}

SyncPlan ConfigSyncer::plan(const ConfigStore& cfg) const
{
    SyncPlan out;
    ConfigStore env(Flavor::Env);
    QString error;
    if (!buildEnv(cfg, &env, &error)) {
        out.error = error;
        return out;
    }
    if (!env.planChanges(&out.envChanges, &error)) {
        out.error = QStringLiteral("llm.env: %1").arg(error);
        return out;
    }
    if (!out.envChanges.isEmpty()) {
        out.diff = ConfigStore::renderDiff(envPath_, out.envChanges);
    }
    out.ok = true;
    return out;
}

bool ConfigSyncer::apply(const ConfigStore& cfg, QString* error, QString* diffOut) const
{
    ConfigStore env(Flavor::Env);
    QString localError;
    if (!buildEnv(cfg, &env, &localError)) {
        if (error) {
            *error = localError;
        }
        return false;
    }

    // 先把改动算出来：算不出来（比如键找不到父块）就整体不写
    QVector<LineChange> envChanges;
    if (!env.planChanges(&envChanges, &localError)) {
        if (error) {
            *error = QStringLiteral("llm.env: %1").arg(localError);
        }
        return false;
    }

    if (diffOut) {
        *diffOut = envChanges.isEmpty()
                       ? QString()
                       : ConfigStore::renderDiff(envPath_, envChanges);
    }

    if (!env.save(&localError)) {
        if (error) {
            *error = localError;
        }
        return false;
    }
    return true;
}

} // namespace core
