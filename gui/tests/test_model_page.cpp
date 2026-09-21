// ============================================================================
//  gui/tests/test_model_page.cpp — 模型测试页控件级测试（T11）
//
//  重点验"界面 → config.yaml → 派生 llm.env"这条链路，以及
//  禁用/本地/云端的块显隐。服务脚本用假仓库（造几个假脚本）验调用。
// ============================================================================
#include <QComboBox>
#include <QDir>
#include <QFile>
#include <QLabel>
#include <QLineEdit>
#include <QPlainTextEdit>
#include <QPushButton>
#include <QRadioButton>
#include <QSpinBox>
#include <QTemporaryDir>
#include <QtTest/QtTest>

#include "core/config_store.h"
#include "ui/model_page.h"

class TestModelPage : public QObject {
    Q_OBJECT

private slots:
    void modeSwitchesVisibleBlocks();
    void loadFromConfigFillsWidgets();
    void saveWritesConfigAndDerivesEnv();
    void siglipIsReadOnlyText();
    void missingPathsAreReportedNotCrash();
};

namespace {

void writeFile(const QString& path, const QString& text)
{
    QDir().mkpath(QFileInfo(path).absolutePath());
    QFile file(path);
    QVERIFY(file.open(QIODevice::WriteOnly));
    file.write(text.toUtf8());
    file.close();
}

/// 造一个小仓库：config/config.yaml（唯一真源）+ llm/config/llm.env（派生目标）
void makeRepo(const QString& root)
{
    writeFile(root + QStringLiteral("/config/config.yaml"),
              QStringLiteral("llm:\n"
                             "  mode: disabled\n"
                             "  model_path: /tmp/models/a.gguf\n"
                             "  ctx_size: 2048\n"
                             "  batch_size: 256\n"
                             "  threads: 4\n"
                             "  port: 9000\n"
                             "  temperature: 0.7\n"
                             "  api_key: sk-old\n"
                             "  api_base: https://old.example.com/v1\n"
                             "  model: old-model\n"
                             "gui:\n"
                             "  wake:\n"
                             "    idle_ms: 5000\n"));
    writeFile(root + QStringLiteral("/llm/config/llm.env"),
              QStringLiteral("# llm.env\nLLM_MODE=disabled\nLLM_MODEL_PATH=/tmp/models/a.gguf\n"
                             "LLM_PORT=9000\nLLM_API_KEY=sk-old\n"));
}

} // namespace

void TestModelPage::modeSwitchesVisibleBlocks()
{
    ModelPage page;
    // 禁用：两块都不显示
    page.modeButton(QStringLiteral("disabled"))->setChecked(true);
    QVERIFY(!page.localBlock()->isVisibleTo(&page));
    QVERIFY(!page.cloudBlock()->isVisibleTo(&page));
    QCOMPARE(page.currentMode(), QStringLiteral("disabled"));

    page.modeButton(QStringLiteral("edge"))->setChecked(true);
    QVERIFY(page.localBlock()->isVisibleTo(&page));
    QVERIFY(!page.cloudBlock()->isVisibleTo(&page));
    QVERIFY(page.statusText().contains(QStringLiteral("llama-server")));

    page.modeButton(QStringLiteral("cloud"))->setChecked(true);
    QVERIFY(!page.localBlock()->isVisibleTo(&page));
    QVERIFY(page.cloudBlock()->isVisibleTo(&page));
    QVERIFY(page.statusText().contains(QStringLiteral("config/config.yaml")));
}

void TestModelPage::loadFromConfigFillsWidgets()
{
    QTemporaryDir tmp;
    QVERIFY(tmp.isValid());
    makeRepo(tmp.path());

    ModelPage page;
    page.setPaths(tmp.path() + QStringLiteral("/config/config.yaml"), tmp.path());
    QCOMPARE(page.currentMode(), QStringLiteral("disabled"));
    QCOMPARE(page.modelBox()->currentText(), QStringLiteral("/tmp/models/a.gguf"));
    QCOMPARE(page.ctxSpin()->value(), 2048);
    QCOMPARE(page.threadsSpin()->value(), 4);
    QCOMPARE(page.cloudBaseEdit()->text(), QStringLiteral("https://old.example.com/v1"));
    QCOMPARE(page.cloudKeyEdit()->text(), QStringLiteral("sk-old"));
}

void TestModelPage::saveWritesConfigAndDerivesEnv()
{
    QTemporaryDir tmp;
    QVERIFY(tmp.isValid());
    makeRepo(tmp.path());

    ModelPage page;
    page.setPaths(tmp.path() + QStringLiteral("/config/config.yaml"), tmp.path());

    // 改成"本地 + 新参数"再保存
    page.modeButton(QStringLiteral("edge"))->setChecked(true);
    page.ctxSpin()->setValue(4096);
    page.threadsSpin()->setValue(3);
    QVERIFY(page.saveAndSync());

    // config.yaml（唯一真源）真的改了
    core::ConfigStore after;
    QString error;
    QVERIFY(after.load(tmp.path() + QStringLiteral("/config/config.yaml"), &error));
    QCOMPARE(after.value(QStringLiteral("llm.mode")), QStringLiteral("edge"));
    QCOMPARE(after.value(QStringLiteral("llm.ctx_size")), QStringLiteral("4096"));
    QCOMPARE(after.value(QStringLiteral("llm.threads")), QStringLiteral("3"));

    // llm.env 是**派生**文件：只有映射表里的 8 个键会跟着走（模式不在其中 ——
    // 模式是给 Agent 看的），这里断言 ctx / 线程落对了
    QFile env(tmp.path() + QStringLiteral("/llm/config/llm.env"));
    QVERIFY(env.open(QIODevice::ReadOnly));
    const QString envText = QString::fromUtf8(env.readAll());
    QVERIFY2(envText.contains(QStringLiteral("LLM_CTX_SIZE=4096")), qPrintable(envText));
    QVERIFY(envText.contains(QStringLiteral("LLM_THREADS=3")));

    // mode 用的是规范词汇 edge（界面、config.yaml、Agent 三处一致）
    QFile cfg(tmp.path() + QStringLiteral("/config/config.yaml"));
    QVERIFY(cfg.open(QIODevice::ReadOnly));
    const QString cfgText = QString::fromUtf8(cfg.readAll());
    QVERIFY2(cfgText.contains(QStringLiteral("mode: edge")), qPrintable(cfgText));

    // 日志里要留下"做过什么"
    QVERIFY(page.logView()->toPlainText().contains(QStringLiteral("config.yaml 已更新")));
    QVERIFY(page.logView()->toPlainText().contains(QStringLiteral("已派生")));
}

void TestModelPage::siglipIsReadOnlyText()
{
    ModelPage page;
    // SigLIP 是固定块：只有说明文字，没有任何可改控件
    QVERIFY(page.siglipLabel() != nullptr);
    QVERIFY(page.siglipLabel()->text().contains(QStringLiteral("siglip_full.rknn")));
    QVERIFY(page.siglipLabel()->text().contains(QStringLiteral("不提供任何开关")));
}

void TestModelPage::missingPathsAreReportedNotCrash()
{
    ModelPage page;
    // 没设置路径就点保存 → 明确提示，不崩
    QVERIFY(!page.saveAndSync());
    QVERIFY(page.logView()->toPlainText().contains(QStringLiteral("无法保存")));

    // 指向不存在的 config.yaml → 读失败提示
    page.setPaths(QStringLiteral("/tmp/definitely-missing/config.yaml"), QStringLiteral("/tmp"));
    QVERIFY(page.logView()->toPlainText().contains(QStringLiteral("读配置失败")));

    // 点服务按钮（仓库根不存在）→ 只提示，不崩
    page.startButton()->click();
    QVERIFY(page.logView()->toPlainText().contains(QStringLiteral("脚本不存在")));
}

QTEST_MAIN(TestModelPage)
#include "test_model_page.moc"
