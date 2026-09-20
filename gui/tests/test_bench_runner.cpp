// ============================================================================
//  gui/tests/test_bench_runner.cpp — 基准测试执行器测试（T12）
//
//  用**假仓库 + 假 bench 脚本**验：接管横幅、运行期禁用服务按钮、
//  停止测试真的把子进程收掉、最新报告能被读到。
//  真脚本（--precheck）由验收脚本在板端跑一次取证。
// ============================================================================
#include <QDir>
#include <QFile>
#include <QLabel>
#include <QPlainTextEdit>
#include <QPushButton>
#include <QTemporaryDir>
#include <QtTest/QtTest>

#include "ui/model_page.h"

class TestBenchRunner : public QObject {
    Q_OBJECT

private slots:
    void precheckRunsAndTakesOverServiceButtons();
    void stopKillsRunningBenchmark();
    void missingScriptIsReported();
    void latestReportIsShown();

private:
    QString makeRepo(QTemporaryDir& tmp, const QString& benchBody);
};

namespace {
void writeFile(const QString& path, const QString& text, bool exec = false)
{
    QDir().mkpath(QFileInfo(path).absolutePath());
    QFile file(path);
    file.open(QIODevice::WriteOnly);
    file.write(text.toUtf8());
    file.close();
    if (exec) {
        file.setPermissions(QFile::ReadOwner | QFile::WriteOwner | QFile::ExeOwner |
                            QFile::ReadGroup | QFile::ExeGroup | QFile::ReadOther | QFile::ExeOther);
    }
}
} // namespace

QString TestBenchRunner::makeRepo(QTemporaryDir& tmp, const QString& benchBody)
{
    const QString root = tmp.path();
    writeFile(root + QStringLiteral("/gui/config/gui.yaml"),
              QStringLiteral("llm:\n  mode: disabled\n  local_model: /tmp/a.gguf\n"));
    writeFile(root + QStringLiteral("/llm/config/llm.env"), QStringLiteral("LLM_PORT=9000\n"));
    writeFile(root + QStringLiteral("/config/config.yaml"), QStringLiteral("llm:\n  mode: disabled\n"));
    writeFile(root + QStringLiteral("/llm/bench_qwen35.py"), benchBody);
    writeFile(root + QStringLiteral("/llm/bench_multimodal.py"), benchBody);
    return root;
}

void TestBenchRunner::precheckRunsAndTakesOverServiceButtons()
{
    QTemporaryDir tmp;
    QVERIFY(tmp.isValid());
    const QString root = makeRepo(
        tmp, QStringLiteral("import sys, time\n"
                            "print('[fake-qwen] 开始预检', flush=True)\n"
                            "time.sleep(1.0)\n"
                            "print('[fake-qwen] 预检完成', flush=True)\n"));
    ModelPage page;
    page.setPaths(root + QStringLiteral("/gui/config/gui.yaml"), root);

    QVERIFY(!page.benchRunning());
    QVERIFY(!page.benchBanner()->isVisibleTo(&page));
    QVERIFY(page.startButton()->isEnabled());

    page.startBenchmark(QStringLiteral("qwen_precheck"));
    QVERIFY(page.benchRunning());
    QVERIFY(page.benchBanner()->isVisibleTo(&page));
    QVERIFY(page.benchBanner()->text().contains(QStringLiteral("接管")));
    // 运行期：服务按钮与其它基准按钮都要禁用（llama-server 被接管）
    QVERIFY(!page.startButton()->isEnabled());
    QVERIFY(!page.stopButton()->isEnabled());
    QVERIFY(!page.qwenFullButton()->isEnabled());
    QVERIFY(page.stopBenchButton()->isEnabled());

    // 等它自己跑完
    QTRY_VERIFY_WITH_TIMEOUT(!page.benchRunning(), 15000);
    QVERIFY(!page.benchBanner()->isVisibleTo(&page));
    QVERIFY(page.startButton()->isEnabled());
    QVERIFY(page.logView()->toPlainText().contains(QStringLiteral("[fake-qwen] 预检完成")));
    QVERIFY(page.logView()->toPlainText().contains(QStringLiteral("退出码 0")));
}

void TestBenchRunner::stopKillsRunningBenchmark()
{
    QTemporaryDir tmp;
    QVERIFY(tmp.isValid());
    const QString root = makeRepo(
        tmp, QStringLiteral("import time\n"
                            "print('[fake-qwen] 长跑开始', flush=True)\n"
                            "time.sleep(60)\n"
                            "print('不该看到这行', flush=True)\n"));
    ModelPage page;
    page.setPaths(root + QStringLiteral("/gui/config/gui.yaml"), root);

    page.startBenchmark(QStringLiteral("qwen_full"));
    QVERIFY(page.benchRunning());
    // 子进程输出是异步到的，要等它来（不能启动后立刻断言）
    QTRY_VERIFY_WITH_TIMEOUT(
        page.logView()->toPlainText().contains(QStringLiteral("长跑开始")), 10000);

    page.stopBenchmark();
    QVERIFY(!page.benchRunning());
    QVERIFY(page.logView()->toPlainText().contains(QStringLiteral("停止测试")));
    QVERIFY(!page.logView()->toPlainText().contains(QStringLiteral("不该看到这行")));
    QVERIFY(page.startButton()->isEnabled());
}

void TestBenchRunner::missingScriptIsReported()
{
    QTemporaryDir tmp;
    QVERIFY(tmp.isValid());
    makeRepo(tmp, QStringLiteral("print('x')\n"));
    QFile::remove(tmp.path() + QStringLiteral("/llm/bench_multimodal.py"));

    ModelPage page;
    page.setPaths(tmp.path() + QStringLiteral("/gui/config/gui.yaml"), tmp.path());
    page.startBenchmark(QStringLiteral("multimodal"));
    QVERIFY(!page.benchRunning());                       // 没起来
    QVERIFY(page.logView()->toPlainText().contains(QStringLiteral("脚本不存在")));

    // 没设置仓库根时也不能崩
    ModelPage bare;
    bare.startBenchmark(QStringLiteral("qwen_full"));
    QVERIFY(!bare.benchRunning());
    QVERIFY(bare.logView()->toPlainText().contains(QStringLiteral("没设置仓库根")));
}

void TestBenchRunner::latestReportIsShown()
{
    QTemporaryDir tmp;
    QVERIFY(tmp.isValid());
    const QString root = makeRepo(tmp, QStringLiteral("print('x')\n"));
    writeFile(root + QStringLiteral("/llm/qwen3.5_bench/Q4_K_M.md"),
              QStringLiteral("# Q4_K_M 报告\n首token 123ms\n维持 4.5 tok/s\n"));
    writeFile(root + QStringLiteral("/llm/multimodal_bench/compare.md"),
              QStringLiteral("# 多模态对比\n更快\n"));

    ModelPage page;
    page.setPaths(root + QStringLiteral("/gui/config/gui.yaml"), root);
    QVERIFY(page.showLatestReport());
    const QString text = page.logView()->toPlainText();
    QVERIFY(text.contains(QStringLiteral("最新报告")));
    QVERIFY(text.contains(QStringLiteral("Q4_K_M 报告")));

    // 没有报告时只提示，不崩
    ModelPage empty;
    empty.setPaths(root + QStringLiteral("/gui/config/gui.yaml"), root);
    // 删掉报告再试
    QFile::remove(root + QStringLiteral("/llm/qwen3.5_bench/Q4_K_M.md"));
    QFile::remove(root + QStringLiteral("/llm/multimodal_bench/compare.md"));
    QVERIFY(!empty.showLatestReport());
    QVERIFY(empty.logView()->toPlainText().contains(QStringLiteral("还没找到基准报告")));
}

QTEST_MAIN(TestBenchRunner)
#include "test_bench_runner.moc"
