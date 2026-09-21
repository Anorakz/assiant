// ============================================================================
//  gui/tests/test_settings_page.cpp — 设置页测试（T13）
//
//  验：载入配置、保存写回 config.yaml 的 gui: 段（含**控制条独立时间**）、恢复默认只改界面不写文件、
//  路径缺失不崩。全部用临时 config.yaml，不碰生产配置。
// ============================================================================
#include <QCheckBox>
#include <QComboBox>
#include <QDir>
#include <QFile>
#include <QLabel>
#include <QPushButton>
#include <QSpinBox>
#include <QTemporaryDir>
#include <QtTest/QtTest>

#include "core/config_store.h"
#include "ui/settings_page.h"

class TestSettingsPage : public QObject {
    Q_OBJECT

private slots:
    void loadFromConfigFillsWidgets();
    void saveWritesConfigWithSeparateTimeouts();
    void saveKeepsAgentSectionsUntouched();
    void restoreDefaultsOnlyTouchesUi();
    void missingConfigIsReportedNotCrash();

private:
    QString writeConfig(QTemporaryDir& tmp, const QString& extra = QString());
};

QString TestSettingsPage::writeConfig(QTemporaryDir& tmp, const QString& extra)
{
    const QString path = tmp.path() + QStringLiteral("/config.yaml");
    QFile file(path);
    file.open(QIODevice::WriteOnly);
    // 归一化 D 系列: 界面项住在 config.yaml 的 gui: 段下 (键路径带 gui. 前缀)
    file.write(QStringLiteral("gui:\n"
                              "  theme: grey\n"
                              "  fullscreen: true\n"
                              "  start_page: system\n"
                              "  debug: true\n"
                              "  wake:\n"
                              "    top: locked\n"
                              "    bottom: active\n"
                              "    left: active\n"
                              "    right: locked\n"
                              "    idle_ms: 7000\n"
                              "  video_overlay:\n"
                              "    mode: locked\n"
                              "    idle_ms: 1500\n"
                              "  input_source: terminal\n").toUtf8());
    file.write(extra.toUtf8());
    file.close();
    return path;
}

void TestSettingsPage::loadFromConfigFillsWidgets()
{
    QTemporaryDir tmp;
    QVERIFY(tmp.isValid());
    const QString path = writeConfig(tmp);

    SettingsPage page;
    page.loadFromConfig(path);
    QVERIFY(page.debugCheck()->isChecked());
    QCOMPARE(page.regionMode(QStringLiteral("top"))->currentData().toString(),
             QStringLiteral("locked"));
    QCOMPARE(page.regionMode(QStringLiteral("bottom"))->currentData().toString(),
             QStringLiteral("active"));
    QCOMPARE(page.regionIdleSpin()->value(), 7000);
    QCOMPARE(page.overlayMode()->currentData().toString(), QStringLiteral("locked"));
    QCOMPARE(page.overlayIdleSpin()->value(), 1500);          // 控制条自己的时间
    QCOMPARE(page.startPageBox()->currentData().toString(), QStringLiteral("system"));
    QCOMPARE(page.inputSourceBox()->currentData().toString(), QStringLiteral("terminal"));
    QVERIFY(page.pathLabel()->text().contains(path));
    QVERIFY(page.aboutLabel()->text().contains(QStringLiteral("RK3568")));
}

void TestSettingsPage::saveWritesConfigWithSeparateTimeouts()
{
    QTemporaryDir tmp;
    QVERIFY(tmp.isValid());
    const QString path = writeConfig(tmp);

    SettingsPage page;
    page.loadFromConfig(path);
    QSignalSpy spy(&page, &SettingsPage::configSaved);

    // 改：上区域改活动、区域时间 9000、控制条时间 800（**两个时间互相独立**）
    page.regionMode(QStringLiteral("top"))->setCurrentIndex(
        page.regionMode(QStringLiteral("top"))->findData(QStringLiteral("active")));
    page.regionIdleSpin()->setValue(9000);
    page.overlayIdleSpin()->setValue(800);
    page.debugCheck()->setChecked(false);
    page.saveButton()->click();
    QCOMPARE(spy.count(), 1);

    core::ConfigStore after;
    QString error;
    QVERIFY(after.load(path, &error));
    QCOMPARE(after.value(QStringLiteral("gui.wake.top")), QStringLiteral("active"));
    QCOMPARE(after.value(QStringLiteral("gui.wake.idle_ms")), QStringLiteral("9000"));
    QCOMPARE(after.value(QStringLiteral("gui.video_overlay.idle_ms")), QStringLiteral("800"));
    QVERIFY(!after.boolValue(QStringLiteral("gui.debug"), true));
    // 没动过的项保持原样
    QCOMPARE(after.value(QStringLiteral("gui.wake.right")), QStringLiteral("locked"));
    QCOMPARE(after.value(QStringLiteral("gui.input_source")), QStringLiteral("terminal"));
}

void TestSettingsPage::saveKeepsAgentSectionsUntouched()
{
    // ⚠ 归一化 D 系列之后, GUI 和 Agent **共用同一个 config.yaml**。
    //    GUI 保存时只能动 gui.* / llm.*, 其它段(含注释与顺序)必须一个字节都不变 ——
    //    否则"改个休眠时间"就会顺手把 Agent 的配置重排掉。
    QTemporaryDir tmp;
    QVERIFY(tmp.isValid());
    const QString path = writeConfig(tmp);

    // 追加 Agent 的两段(带注释与空行)
    const QString agentPart = QStringLiteral(
        "\n"
        "llm:\n"
        "  mode: disabled\n"
        "  api_key: \"\"        # 这行注释必须原样留下\n"
        "\n"
        "scheduler:\n"
        "  commands: []\n");
    QFile append(path);
    QVERIFY(append.open(QIODevice::Append));
    append.write(agentPart.toUtf8());
    append.close();

    SettingsPage page;
    page.loadFromConfig(path);
    page.debugCheck()->setChecked(!page.debugCheck()->isChecked());   // 改一项 gui.*
    page.saveButton()->click();

    QFile after(path);
    QVERIFY(after.open(QIODevice::ReadOnly));
    const QString text = QString::fromUtf8(after.readAll());

    // Agent 的段逐字节还在(整体子串匹配: 顺序/注释/空行都对得上)
    QVERIFY2(text.contains(agentPart),
             qPrintable(QStringLiteral("Agent 的段被改动了:\n") + text));

    // 同时确认这次保存**确实**改了东西(不然这条用例是假绿)
    QVERIFY(page.debugCheck()->isChecked() ? text.contains(QStringLiteral("debug: true"))
                                           : text.contains(QStringLiteral("debug: false")));
}

void TestSettingsPage::restoreDefaultsOnlyTouchesUi()
{
    QTemporaryDir tmp;
    QVERIFY(tmp.isValid());
    const QString path = writeConfig(tmp);

    SettingsPage page;
    page.loadFromConfig(path);
    page.defaultsButton()->click();

    // 界面回到默认
    QVERIFY(!page.debugCheck()->isChecked());
    QCOMPARE(page.regionMode(QStringLiteral("top"))->currentData().toString(),
             QStringLiteral("locked"));
    QCOMPARE(page.regionIdleSpin()->value(), 5000);
    QCOMPARE(page.overlayIdleSpin()->value(), 3000);

    // **文件没被动**（避免点错就改掉生产配置）
    core::ConfigStore after;
    QString error;
    QVERIFY(after.load(path, &error));
    QCOMPARE(after.value(QStringLiteral("gui.wake.idle_ms")), QStringLiteral("7000"));
    QCOMPARE(after.value(QStringLiteral("gui.video_overlay.idle_ms")), QStringLiteral("1500"));
    QVERIFY(after.boolValue(QStringLiteral("gui.debug"), false));
}

void TestSettingsPage::missingConfigIsReportedNotCrash()
{
    SettingsPage page;
    // 没设置路径 → 保存明确失败
    QString error;
    QVERIFY(!page.saveToConfig(&error));
    QVERIFY(!error.isEmpty());

    // 指向不存在的文件 → 载入只警告、保存失败，都不崩
    page.loadFromConfig(QStringLiteral("/tmp/definitely-missing-config.yaml"));
    QVERIFY(!page.saveToConfig(&error));
    page.defaultsButton()->click();     // 只改界面
    QVERIFY(page.regionIdleSpin()->value() == 5000);
}

QTEST_MAIN(TestSettingsPage)
#include "test_settings_page.moc"
