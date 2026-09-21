// ============================================================================
//  gui/tools/gui_config_sync.cpp — 配置派生 CLI（T2 证据链 / T11 可复用）
//
//  做一件事：按 config.yaml（唯一真源）派生 llm/config/llm.env。
//
//  默认 **dry-run**：只打印将要发生的 diff，**不写任何文件**。
//  加 --apply 才真写（llm.env 留 .bak + 原子 rename）。
//
//  归一化 D 系列之前这个 CLI 是"gui.yaml → llm.env + config.yaml"；
//  现在配置真源只有 config.yaml，所以它只剩"派生 llm.env"一件事。
//
//  用法
//  ---------------------------------------------------------------------------
//      gui/build/gui_config_sync --config config/config.yaml
//      gui/build/gui_config_sync --config /tmp/g/config.yaml --env /tmp/g/llm.env --apply
// ============================================================================
#include <QCommandLineOption>
#include <QCommandLineParser>
#include <QCoreApplication>
#include <QDebug>

#include "core/config_store.h"
#include "core/config_sync.h"

int main(int argc, char** argv)
{
    QCoreApplication app(argc, argv);
    QCoreApplication::setApplicationName(QStringLiteral("gui_config_sync"));

    QCommandLineParser parser;
    parser.setApplicationDescription(
        QStringLiteral("按 config.yaml 派生 llm.env（默认只预览 diff）"));
    parser.addHelpOption();

    const QCommandLineOption cfgOpt({QStringLiteral("c"), QStringLiteral("config")},
                                    QStringLiteral("config.yaml 路径（真源）"),
                                    QStringLiteral("path"),
                                    QStringLiteral("config/config.yaml"));
    const QCommandLineOption envOpt({QStringLiteral("e"), QStringLiteral("env")},
                                    QStringLiteral("llm.env 路径（派生产物）"),
                                    QStringLiteral("path"),
                                    QStringLiteral("llm/config/llm.env"));
    const QCommandLineOption applyOpt(QStringLiteral("apply"),
                                      QStringLiteral("真的写入（默认只预览）"));
    parser.addOptions({cfgOpt, envOpt, applyOpt});
    parser.process(app);

    core::ConfigStore cfg;
    QString error;
    if (!cfg.load(parser.value(cfgOpt), &error)) {
        qWarning().noquote() << "[sync]" << error;
        return 1;
    }

    core::ConfigSyncer syncer(parser.value(envOpt));

    if (!parser.isSet(applyOpt)) {
        const core::SyncPlan plan = syncer.plan(cfg);
        if (!plan.ok) {
            qWarning().noquote() << "[sync]" << plan.error;
            return 1;
        }
        qInfo().noquote() << "[sync] dry-run（未写文件）";
        qInfo().noquote() << (plan.diff.isEmpty() ? QStringLiteral("[sync] 无差异")
                                                  : plan.diff);
        return 0;
    }

    QString diff;
    if (!syncer.apply(cfg, &error, &diff)) {
        qWarning().noquote() << "[sync]" << error;
        return 1;
    }
    qInfo().noquote() << "[sync] apply";
    qInfo().noquote() << (diff.isEmpty() ? QStringLiteral("[sync] 无差异（未写）") : diff);
    return 0;
}
