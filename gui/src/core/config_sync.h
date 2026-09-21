// ============================================================================
//  gui/src/core/config_sync.h — config.yaml（唯一真源）→ llm/config/llm.env（派生）
//
//  归一化 D 系列：配置真源只有 config/config.yaml 一份。
//  llm.env 是**派生文件** —— 它喂给板端的 llama-server 进程，不是任何人手改的对象。
//
//  映射表（config.yaml → llm.env）
//  ---------------------------------------------------------------------------
//      llm.model_path      →  LLM_MODEL_PATH
//      llm.model_name      →  LLM_MODEL_NAME
//      llm.port            →  LLM_PORT
//      llm.ctx_size        →  LLM_CTX_SIZE
//      llm.batch_size      →  LLM_BATCH_SIZE
//      llm.threads         →  LLM_THREADS
//      llm.threads_batch   →  LLM_THREADS_BATCH
//      llm.local_api_key   →  LLM_API_KEY
//
//  ⚠ llm.env 里另有 5 个与部署布局有关的键（LLM_HOST / LLM_LOG_DIR / LLM_RUN_DIR /
//    LLM_PID_FILE / LLM_LOG_FILE）**不在**映射表里 —— 它们由 llm.env 自己维护。
//    同步是"文本级替换"：只改上面那 8 行，其余注释与顺序原样保留。
//
//  ⚠ 云端参数（llm.api_key / llm.api_base / llm.model / llm.timeout_s）**不进** llm.env
//    —— 那是 Agent 用的，由 config.yaml 直接喂给 agent/llm/provider.py。
//
//  ⚠ 本类**不写** config.yaml：调用方（模型页）自己持有那个 ConfigStore 并 save()。
//    同一个文件上跑两个 ConfigStore 实例会互相覆盖（各自持有整份文本快照）。
// ============================================================================
#pragma once

#include <QString>
#include <QVector>

#include "core/config_store.h"

namespace core {

struct SyncPlan {
    bool ok = false;
    QString error;
    QVector<LineChange> envChanges;   ///< 只针对 llm.env（config.yaml 不由本类改）
    QString diff;                     ///< 写前预览（人可读）
};

class ConfigSyncer {
public:
    explicit ConfigSyncer(QString envPath);

    /// 只算不改。
    SyncPlan plan(const ConfigStore& cfg) const;

    /// 应用（llm.env 会留 `.bak` + 原子写）。算不出改动就整体不写。
    bool apply(const ConfigStore& cfg, QString* error, QString* diffOut) const;

    /// 映射表（供测试与文档核对）：返回会被写进 llm.env 的键列表。
    static QStringList envTargetKeys();

    /// 单个目标键的取值。found=false 表示 config.yaml 里没有对应项（不同步）。
    static QString envValue(const ConfigStore& cfg, const QString& targetKey, bool* found);

private:
    bool buildEnv(const ConfigStore& cfg, ConfigStore* env, QString* error) const;

    QString envPath_;
};

} // namespace core
