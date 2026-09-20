// ============================================================================
//  gui/src/core/config_sync.h — gui.yaml（唯一真源）→ llm.env / config.yaml
//
//  方案 §5 的映射表（D3 定案：本地模型一律写 GGUF 路径）
//  ---------------------------------------------------------------------------
//    gui.yaml                  llm/config/llm.env           config/config.yaml
//    llm.local_model      →    LLM_MODEL_PATH          →    llm.model_path
//    llm.model_name       →    LLM_MODEL_NAME
//    llm.port             →    LLM_PORT
//    llm.ctx_size         →    LLM_CTX_SIZE
//    llm.batch_size       →    LLM_BATCH_SIZE
//    llm.threads          →    LLM_THREADS
//    llm.threads_batch    →    LLM_THREADS_BATCH
//    llm.api_key          →    LLM_API_KEY             →    llm.api_key(**仅云端模式**)
//    llm.mode             →                             →    llm.mode(local→edge/cloud/disabled)
//    llm.cloud.key        →                             →    llm.api_key(云端时)
//    llm.cloud.base       →                             →    llm.api_base
//    llm.cloud.model      →                             →    llm.model
//    llm.cloud.timeout_s  →                             →    llm.timeout_s
//    llm.temperature      →                             →    llm.temperature
//
//  只有 gui.yaml 里**确实存在**的键才同步（"有相同配置项才同步"）。
//  写入沿用 ConfigStore 的文本级替换：注释与顺序都保住。
// ============================================================================
#pragma once

#include <QString>
#include <QVector>

#include "core/config_store.h"

namespace core {

struct SyncPlan {
    bool ok = false;
    QString error;
    QVector<LineChange> envChanges;
    QVector<LineChange> configChanges;
    QString diff;                 ///< 写前预览（人可读）
};

class ConfigSyncer {
public:
    ConfigSyncer(QString envPath, QString agentConfigPath);

    /// 只算不改。
    SyncPlan plan(const ConfigStore& gui) const;

    /// 应用（两个目标各自 `.bak` + 原子写）。任一目标算不出改动就整体不写。
    bool apply(const ConfigStore& gui, QString* error, QString* diffOut) const;

    /// 映射表（供测试与文档核对）：返回映射到的目标键列表。
    static QStringList envTargetKeys();
    static QStringList configTargetKeys();

    /// 单个目标键的取值。found=false 表示 gui 里没有对应项（不同步）。
    static QString envValue(const ConfigStore& gui, const QString& targetKey, bool* found);
    static QString configValue(const ConfigStore& gui, const QString& targetKey, bool* found);

private:
    bool buildStores(const ConfigStore& gui, ConfigStore* env, ConfigStore* cfg,
                     QString* error) const;

    QString envPath_;
    QString configPath_;
};

} // namespace core
