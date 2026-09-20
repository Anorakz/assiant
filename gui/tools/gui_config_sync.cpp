// ============================================================================
//  gui/tools/gui_config_sync.cpp — 配置同步 CLI（T2 证据链 / T11 可复用）
//
//  做一件事：按 gui.yaml（唯一真源）把改动同步到 llm.env 与 config.yaml。
//
//  默认 **dry-run**：只打印将要发生的 diff，**不写任何文件**。
//  加 --apply 才真写（每个目标各自 .bak + 原子 rename）。
//
//  用法
//  ---------------------------------------------------------------------------
//      gui/build/gui_config_sync --gui gui/config/gui.yaml
//      gui/build/gui_config_sync --gui /tmp/g/gui.yaml --env /tmp/g/llm.env --agent /tmp/g/config.yaml --apply
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
        QStringLiteral("按 gui.yaml 同步 llm.env / config.yaml（默认只预览 diff）"));
    parser.addHelpOption();

    const QCommandLineOption guiOpt({QStringLiteral("g"), QStringLiteral("gui")},
                                    QStringLiteral("gui.yaml 路径（真源）"),
                                    QStringLiteral("path"));
    const QCommandLineOption envOpt({QStringLiteral("e"), QStringLiteral("env")},
                                    QStringLiteral("llm.env 路径"),
                                    QStringLiteral("path"),
                                    QStringLiteral("llm/config/llm.env"));
    const QCommandLineOption agentOpt({QStringLiteral("a"), QStringLiteral("agent")},
                                      QStringLiteral("config.yaml 路径"),
                                      QStringLiteral("path"),
                                      QStringLiteral("config/config.yaml"));
    const QCommandLineOption applyOpt(QStringLiteral("apply"),
                                      QStringLiteral("真的写入（默认只预览）"));
    parser.addOptions({guiOpt, envOpt, agentOpt, applyOpt});
    parser.process(app);

    if (!parser.isSet(guiOpt)) {
        qWarning().noquote() << "[sync] 必须给 --gui <gui.yaml>（用 --help 看用法）";
        return 2;
    }

    core::ConfigStore gui;
    QString error;
    if (!gui.load(parser.value(guiOpt), &error)) {
        qWarning().noquote() << "[sync]" << error;
        return 1;
    }

    core::ConfigSyncer syncer(parser.value(envOpt), parser.value(agentOpt));

    if (!parser.isSet(applyOpt)) {
        const core::SyncPlan plan = syncer.plan(gui);
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
    if (!syncer.apply(gui, &error, &diff)) {
        qWarning().noquote() << "[sync]" << error;
        return 1;
    }
    qInfo().noquote() << "[sync] apply";
    qInfo().noquote() << (diff.isEmpty() ? QStringLiteral("[sync] 无差异（未写）") : diff);
    return 0;
}
