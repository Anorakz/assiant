// ============================================================================
//  gui/tests/test_model_page.cpp — 模型页测试（T11 / T14-3）
//
//  ⚠ T14-3 起本页**不写文件、不跑脚本**：
//    · 「保存」只发出 `configSaveRequested(keys, credentials)`，落盘 + 派生 llm.env
//      由 Agent 做（docs/adr/0005）；
//    · 「启动/停止/状态」只发出 `serviceRequested(action)`，脚本由 Agent 跑。
//  所以这里盯的是：载入配置照显示、请求内容对、失败路径有明确日志、不崩。
// ============================================================================
#include <QComboBox>
#include "ui/state_views.h"   // G-D-3：完整类型（判据用 isRunning ✓）
#include <QDir>
#include <QDoubleSpinBox>
#include <QFile>
#include <QJsonObject>
#include <QLabel>
#include <QLineEdit>
#include <QPlainTextEdit>
#include <QPushButton>
#include <QRadioButton>
#include <QSignalSpy>
#include <QSpinBox>
#include <QTemporaryDir>
#include <QtTest/QtTest>

#include "core/config_store.h"
#include "ui/model_page.h"

class TestModelPage : public QObject {
    Q_OBJECT

private slots:
    /// T15-16 G-D-3：跑分是异步的 ⇒ 发起时有加载感 ✓、收到结果就收 ✓
    void theBenchmarkShowsALoadingSkeleton();
    void loadFromConfigFillsWidgets();
    void saveEmitsTheRequest();
    void serviceButtonsAskTheAgent();
    void missingPathsAreReportedNotCrash();
    void siglipIsReadOnlyText();

private:
    void makeRepo(const QString& root);
};

void TestModelPage::makeRepo(const QString& root)
{
    QDir(root).mkpath(QStringLiteral("config"));
    QDir(root).mkpath(QStringLiteral("llm/config"));
    QDir(root).mkpath(QStringLiteral("llm/models"));
    QFile cfg(root + QStringLiteral("/config/config.yaml"));
    cfg.open(QIODevice::WriteOnly);
    cfg.write(QStringLiteral("llm:\n"
                             "  mode: disabled\n"
                             "  model_path: /tmp/does-not-exist.gguf\n"
                             "  ctx_size: 2048\n"
                             "  batch_size: 128\n"
                             "  threads: 4\n"
                             "  port: 9000\n"
                             "  temperature: 0.7\n"
                             "  api_base: https://old.example.com/v1\n"
                             "  model: old-model\n"
                             "  api_key: sk-old\n").toUtf8());
    cfg.close();
    QFile env(root + QStringLiteral("/llm/config/llm.env"));
    env.open(QIODevice::WriteOnly);
    env.write(QStringLiteral("# llm.env\nLLM_CTX_SIZE=2048\nLLM_THREADS=4\n").toUtf8());
    env.close();
}

void TestModelPage::loadFromConfigFillsWidgets()
{
    QTemporaryDir tmp;
    QVERIFY(tmp.isValid());
    makeRepo(tmp.path());

    ModelPage page;
    page.setPaths(tmp.path() + QStringLiteral("/config/config.yaml"), tmp.path());
    page.loadFromConfig();

    QCOMPARE(page.currentMode(), QStringLiteral("disabled"));
    QVERIFY(page.modeButton(QStringLiteral("disabled"))->isChecked());
    QCOMPARE(page.ctxSpin()->value(), 2048);
    QCOMPARE(page.threadsSpin()->value(), 4);
    QCOMPARE(page.cloudBaseEdit()->text(), QStringLiteral("https://old.example.com/v1"));
    QCOMPARE(page.cloudKeyEdit()->text(), QStringLiteral("sk-old"));
}

void TestModelPage::saveEmitsTheRequest()
{
    QTemporaryDir tmp;
    QVERIFY(tmp.isValid());
    makeRepo(tmp.path());
    const QString configPath = tmp.path() + QStringLiteral("/config/config.yaml");

    ModelPage page;
    page.setPaths(configPath, tmp.path());
    page.loadFromConfig();

    QSignalSpy spy(&page, &ModelPage::configSaveRequested);
    page.modeButton(QStringLiteral("edge"))->setChecked(true);
    page.ctxSpin()->setValue(4096);
    page.threadsSpin()->setValue(3);
    QVERIFY(page.saveAndSync());

    QCOMPARE(spy.count(), 1);
    const QJsonObject keys = spy.first().at(0).toJsonObject();
    QCOMPARE(keys.value(QStringLiteral("llm.mode")).toString(), QStringLiteral("edge"));
    QCOMPARE(keys.value(QStringLiteral("llm.ctx_size")).toString(), QStringLiteral("4096"));
    QCOMPARE(keys.value(QStringLiteral("llm.threads")).toString(), QStringLiteral("3"));
    QCOMPARE(keys.value(QStringLiteral("llm.api_key")).toString(), QStringLiteral("sk-old"));

    // ⚠ 本页**一个字节都没写**（落盘是 Agent 的事）——真源保持原样
    core::ConfigStore after;
    QString error;
    QVERIFY(after.load(configPath, &error));
    QCOMPARE(after.value(QStringLiteral("llm.mode")), QStringLiteral("disabled"));
    QCOMPARE(after.value(QStringLiteral("llm.ctx_size")), QStringLiteral("2048"));

    // 日志里要留下"交给谁了"
    QVERIFY(page.logView()->toPlainText().contains(QStringLiteral("交给 Agent")));
}

void TestModelPage::serviceButtonsAskTheAgent()
{
    QTemporaryDir tmp;
    QVERIFY(tmp.isValid());
    makeRepo(tmp.path());

    ModelPage page;
    page.setPaths(tmp.path() + QStringLiteral("/config/config.yaml"), tmp.path());

    QSignalSpy spy(&page, &ModelPage::serviceRequested);
    page.startButton()->click();
    page.stopButton()->click();
    page.statusButton()->click();

    QCOMPARE(spy.count(), 3);
    QCOMPARE(spy.at(0).at(0).toString(), QStringLiteral("start"));
    QCOMPARE(spy.at(1).at(0).toString(), QStringLiteral("stop"));
    QCOMPARE(spy.at(2).at(0).toString(), QStringLiteral("status"));

    // Agent 的回执写进日志（成功/失败都写）
    QJsonObject result;
    result.insert(QStringLiteral("action"), QStringLiteral("start"));
    result.insert(QStringLiteral("ok"), false);
    result.insert(QStringLiteral("message"), QStringLiteral("start.sh 退出码 3"));
    page.onServiceResult(result);
    QVERIFY(page.logView()->toPlainText().contains(QStringLiteral("退出码 3")));
}

void TestModelPage::missingPathsAreReportedNotCrash()
{
    ModelPage page;
    // 没设置路径就点保存 -> 明确提示，不崩
    QVERIFY(!page.saveAndSync());
    QVERIFY(page.logView()->toPlainText().contains(QStringLiteral("没设置 config.yaml 路径")));

    // 指向不存在的仓库 -> 载入只记日志，不崩
    page.setPaths(QStringLiteral("/tmp/definitely-missing-config.yaml"), QStringLiteral("/tmp/nope"));
    page.loadFromConfig();
    page.startButton()->click();          // 没连 Agent 也不该崩（只发信号）
    QVERIFY(page.startButton() != nullptr);
}

void TestModelPage::siglipIsReadOnlyText()
{
    ModelPage page;
    // SigLIP 是固定块：只有说明文字，没有任何可改控件
    QVERIFY(page.siglipLabel() != nullptr);
    QVERIFY(page.siglipLabel()->text().contains(QStringLiteral("siglip_full.rknn")));
    QVERIFY(page.siglipLabel()->text().contains(QStringLiteral("不提供任何开关")));
}


/// T15-16 G-D-3：`model_page` 的跑分/扫描走 IPC **回调** ✓（跑分还是秒级 ✓）⇒ 窗口真实存在 ✓；
/// 判据用 `findChild<Skeleton*>()` ✓ —— 不在头文件上为测试开洞 ✓。
void TestModelPage::theBenchmarkShowsALoadingSkeleton()
{
    ModelPage page;
    Skeleton* bar = page.findChild<Skeleton*>();
    QVERIFY2(bar != nullptr, "模型页没有加载骨架 ✗");
    QVERIFY2(!bar->isRunning(), "还没干活就不该在跑 ✓");

    page.startBenchmark(QStringLiteral("cpu"));      // 走真实入口 ✓
    QVERIFY2(bar->isRunning(), "发起跑分后骨架该在跑 ✓（这就是「秒级等待」的加载感 ✓）");

    QJsonObject result;
    result.insert(QStringLiteral("ok"), true);
    page.onServiceResult(result);                    // 收到结果 ✓
    QVERIFY2(!bar->isRunning(),
             "收到结果后骨架还在跑 ✗ —— 会白烧唤醒（见 G-B-4 的教训）");
}

QTEST_MAIN(TestModelPage)
#include "test_model_page.moc"
