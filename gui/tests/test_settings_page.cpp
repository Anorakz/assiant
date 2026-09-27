// ============================================================================
//  gui/tests/test_settings_page.cpp — 设置页测试（T13 / T14-3）
//
//  ⚠ T14-3 起本页**不写 config.yaml**：点「保存」只发出 `saveRequested(keys, credentials)`，
//    落盘由 Agent 做（docs/adr/0005）。所以这里盯的是**请求侧契约**：
//      · 载入配置 -> 界面照配置显示（读没坏）；
//      · 点保存 -> 发出请求；`buildKeys()` 里的键**按键盘点**只许是白名单那些；
//      · 凭据只带"这次真填了"的键；换行的值当场拒绝；
//      · 回执 -> 成功提示 / 失败显示 Agent 原话（"没连上"时露出「启动 Agent」）；
//      · 恢复默认只改界面。
//  全部不碰生产配置（只读一份临时 config.yaml）。
// ============================================================================
#include <QCheckBox>
#include <QComboBox>
#include <QDir>
#include <QDoubleSpinBox>
#include <QFile>
#include <QJsonObject>
#include <QLabel>
#include <QLineEdit>
#include <QPushButton>
#include <QSet>
#include <QSpinBox>
#include <QTemporaryDir>
#include <QtTest/QtTest>

#include "core/config_store.h"
#include "core/cookie_store.h"
#include "ui/settings_page.h"

namespace {

/// 设置页保存时**准许写**的键（T13-9 按键盘点，T14-3 变成"请求侧白名单"）。
const QStringList& whitelistExact()
{
    static const QStringList kExact = {
        QStringLiteral("study.enabled"),
        QStringLiteral("study.focus_interval_min"),
        QStringLiteral("study.recheck_interval_min"),
        QStringLiteral("study.max_failures"),
        QStringLiteral("study.cooldown_min"),
        QStringLiteral("study.cooldown_probe_min"),
        QStringLiteral("study.relative_band"),
        QStringLiteral("bilibili.cookie_file"),
        QStringLiteral("bilibili.game_watch.enabled"),
        QStringLiteral("bilibili.game_watch.interval_s"),
        QStringLiteral("bilibili.game_watch.confident_score"),
        QStringLiteral("profile.enabled"),
        QStringLiteral("profile.trigger_chars"),
        QStringLiteral("profile.trigger_turns"),
    };
    return kExact;
}

bool isWhitelistedKey(const QString& key)
{
    // gui.* / llm.* 整段允许（前者是本页原有的界面项，后者是模型页的）
    return key.startsWith(QStringLiteral("gui.")) || key.startsWith(QStringLiteral("llm."))
           || whitelistExact().contains(key);
}

} // namespace

class TestSettingsPage : public QObject {
    Q_OBJECT

private slots:
    void loadFromConfigFillsWidgets();
    void missingConfigDoesNotCrash();
    void saveButtonEmitsTheRequest();
    void requestOnlyTouchesWhitelistedKeys();
    void credentialsAreOnlyTheFilledOnes();
    void credentialWithNewlineIsRefused();
    void resultLabelShowsSuccess();
    void resultLabelShowsTheAgentsWordsAndOffersStart();
    void restoreDefaultsOnlyTouchesUi();

private:
    QString writeConfig(QTemporaryDir& tmp, const QString& extra = QString());
};

QString TestSettingsPage::writeConfig(QTemporaryDir& tmp, const QString& extra)
{
    const QString path = tmp.path() + QStringLiteral("/config.yaml");
    QFile file(path);
    file.open(QIODevice::WriteOnly);
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
    const QString path = writeConfig(tmp, QStringLiteral(
        "\n"
        "study:\n"
        "  enabled: true\n"
        "  focus_interval_min: 25\n"
        "  recheck_interval_min: 7\n"
        "  max_failures: 4\n"
        "  cooldown_min: 45\n"
        "  cooldown_probe_min: 2\n"
        "  relative_band: 0.08\n"
        "\n"
        "bilibili:\n"
        "  cookie_file: /tmp/t14-3-not-a-real-cookie.json\n"
        "  game_watch:\n"
        "    enabled: false\n"
        "    interval_s: 30\n"
        "    confident_score: 0.90\n"
        "\n"
        "profile:\n"
        "  enabled: false\n"
        "  trigger_chars: 500\n"
        "  trigger_turns: 5\n"));

    SettingsPage page;
    page.loadFromConfig(path);

    QVERIFY(page.debugCheck()->isChecked());
    QCOMPARE(page.regionMode(QStringLiteral("top"))->currentData().toString(),
             QStringLiteral("locked"));
    QCOMPARE(page.regionIdleSpin()->value(), 7000);
    QCOMPARE(page.overlayIdleSpin()->value(), 1500);
    QCOMPARE(page.inputSourceBox()->currentData().toString(), QStringLiteral("terminal"));

    QVERIFY(page.studyEnabledCheck()->isChecked());
    QCOMPARE(page.studyFocusSpin()->value(), 25);
    QCOMPARE(page.studyBandSpin()->value(), 0.08);
    QVERIFY(!page.gameEnabledCheck()->isChecked());
    QCOMPARE(page.gameIntervalSpin()->value(), 30);
    QVERIFY(!page.profileEnabledCheck()->isChecked());
    QCOMPARE(page.profileCharsSpin()->value(), 500);
    QCOMPARE(page.cookiePathEdit()->text(), QStringLiteral("/tmp/t14-3-not-a-real-cookie.json"));
    // 凭据框故意不预填（预填会把掩码串当新值发出去）
    QVERIFY(page.sessdataEdit()->text().isEmpty());
    QVERIFY(!page.pathLabel()->text().isEmpty());
}

void TestSettingsPage::missingConfigDoesNotCrash()
{
    SettingsPage page;
    page.loadFromConfig(QStringLiteral("/tmp/definitely-missing-config.yaml"));
    QVERIFY(page.resultLabel() != nullptr);       // 读不到也不崩，只是没东西可显示
    page.loadFromConfig(QString());               // 空路径同样不崩
    QVERIFY(page.regionIdleSpin()->value() >= page.regionIdleSpin()->minimum());
}

void TestSettingsPage::saveButtonEmitsTheRequest()
{
    QTemporaryDir tmp;
    QVERIFY(tmp.isValid());
    SettingsPage page;
    page.loadFromConfig(writeConfig(tmp));

    QSignalSpy spy(&page, &SettingsPage::saveRequested);
    page.regionIdleSpin()->setValue(9000);
    page.debugCheck()->setChecked(false);
    page.saveButton()->click();

    QCOMPARE(spy.count(), 1);
    const QList<QVariant> args = spy.first();
    const QJsonObject keys = args.at(0).toJsonObject();
    QVERIFY(!keys.isEmpty());
    QCOMPARE(keys.value(QStringLiteral("gui.wake.idle_ms")).toString(), QStringLiteral("9000"));
    QCOMPARE(keys.value(QStringLiteral("gui.debug")).toString(), QStringLiteral("false"));
}

void TestSettingsPage::requestOnlyTouchesWhitelistedKeys()
{
    // ⚠ T13-9 那条"逐行核对文件"的守卫，T14-3 起改成**请求侧**的：
    //    页面永远只发这几个键 —— 多一个都算回归（写到真源里的东西由 Agent 校验，
    //    但"界面想改什么"这件事在这里就该是封闭的）。
    SettingsPage page;
    QTemporaryDir tmp;
    QVERIFY(tmp.isValid());
    page.loadFromConfig(writeConfig(tmp));      // 载入一份配置（cookie 路径才有值）
    const QJsonObject keys = page.buildKeys();
    QVERIFY(!keys.isEmpty());

    QStringList offenders;
    for (auto it = keys.constBegin(); it != keys.constEnd(); ++it) {
        if (!isWhitelistedKey(it.key())) {
            offenders << it.key();
        }
    }
    QVERIFY2(offenders.isEmpty(), qPrintable(QStringLiteral("请求里出现了白名单之外的键: %1")
                                                 .arg(offenders.join(QStringLiteral(", ")))));

    // 反空转：三张卡片的关键项与 gui.* 都在请求里（否则上面那条是假绿）
    for (const QString& must : {QStringLiteral("study.enabled"),
                                QStringLiteral("study.relative_band"),
                                QStringLiteral("bilibili.game_watch.interval_s"),
                                QStringLiteral("bilibili.cookie_file"),
                                QStringLiteral("profile.trigger_chars"),
                                QStringLiteral("gui.wake.idle_ms"),
                                QStringLiteral("gui.input_source")}) {
        QVERIFY2(keys.contains(must), qPrintable(must));
    }
    QCOMPARE(keys.size(), 25);      // 15 个 gui.* + 10 个卡片键（少一个就说明有人把行删了）
}

void TestSettingsPage::credentialsAreOnlyTheFilledOnes()
{
    QTemporaryDir tmp;
    QVERIFY(tmp.isValid());
    SettingsPage page;
    page.loadFromConfig(writeConfig(tmp));

    QCOMPARE(page.buildCredentials().size(), 0);        // 都没填 -> 空（= 不改动凭据）
    page.sessdataEdit()->setText(QStringLiteral("  sess-123  "));
    page.biliJctEdit()->setText(QStringLiteral("jct-abc"));
    QJsonObject credentials = page.buildCredentials();
    QCOMPARE(credentials.size(), 2);                    // 空框不进请求
    QCOMPARE(credentials.value(QStringLiteral("SESSDATA")).toString(),
             QStringLiteral("sess-123"));               // 去掉首尾空白
    QVERIFY(!credentials.contains(QStringLiteral("DedeUserID")));

    // 成功回执之后：填过的框清掉（免得第二次保存把同一串再发一遍）
    QJsonObject ok;
    ok.insert(QStringLiteral("ok"), true);
    ok.insert(QStringLiteral("changed"), 3);
    page.onConfigResult(ok);
    QVERIFY(page.sessdataEdit()->text().isEmpty());
    QVERIFY(page.biliJctEdit()->text().isEmpty());
}

void TestSettingsPage::credentialWithNewlineIsRefused()
{
    QTemporaryDir tmp;
    QVERIFY(tmp.isValid());
    SettingsPage page;
    page.loadFromConfig(writeConfig(tmp));

    QSignalSpy spy(&page, &SettingsPage::saveRequested);
    page.sessdataEdit()->setText(QStringLiteral("a\nb"));
    page.saveButton()->click();

    QCOMPARE(spy.count(), 0);                            // 一个请求都不发
    QVERIFY(page.resultLabel()->text().contains(QStringLiteral("换行")));
}

void TestSettingsPage::resultLabelShowsSuccess()
{
    SettingsPage page;
    QJsonObject ok;
    ok.insert(QStringLiteral("ok"), true);
    ok.insert(QStringLiteral("changed"), 2);
    page.onConfigResult(ok);
    QVERIFY(page.resultLabel()->text().contains(QStringLiteral("2 行")));
    QVERIFY(page.startAgentButton()->isHidden());

    // 派生文件那一步失败要如实带上（但不影响"真源写成功"这句）
    QJsonObject env;
    env.insert(QStringLiteral("ok"), false);
    env.insert(QStringLiteral("error"), QStringLiteral("llm.env 不在"));
    QJsonObject ok2;
    ok2.insert(QStringLiteral("ok"), true);
    ok2.insert(QStringLiteral("changed"), 1);
    ok2.insert(QStringLiteral("llm_env"), env);
    page.onConfigResult(ok2);
    QVERIFY(page.resultLabel()->text().contains(QStringLiteral("llm.env 不在")));
}

void TestSettingsPage::resultLabelShowsTheAgentsWordsAndOffersStart()
{
    SettingsPage page;
    QJsonObject failure;
    failure.insert(QStringLiteral("ok"), false);
    failure.insert(QStringLiteral("error"), QStringLiteral("真源不在: /tmp/x/config.yaml"));
    page.onConfigResult(failure);
    QVERIFY(page.resultLabel()->text().contains(QStringLiteral("真源不在")));
    QVERIFY(page.startAgentButton()->isHidden());

    // "没连上"那一种：要露出「启动 Agent」（T14-7 的单元就位后真能起）
    QJsonObject offline;
    offline.insert(QStringLiteral("ok"), false);
    offline.insert(QStringLiteral("error"), QStringLiteral("Agent 没连上（命令没发出去）"));
    page.onConfigResult(offline);
    QVERIFY(page.resultLabel()->text().contains(QStringLiteral("没连上")));
    QVERIFY(!page.startAgentButton()->isHidden());
}

void TestSettingsPage::restoreDefaultsOnlyTouchesUi()
{
    QTemporaryDir tmp;
    QVERIFY(tmp.isValid());
    SettingsPage page;
    page.loadFromConfig(writeConfig(tmp));
    page.defaultsButton()->click();

    QVERIFY(!page.debugCheck()->isChecked());
    QCOMPARE(page.regionMode(QStringLiteral("top"))->currentData().toString(),
             QStringLiteral("locked"));
    QCOMPARE(page.regionIdleSpin()->value(), 5000);
    QCOMPARE(page.overlayIdleSpin()->value(), 3000);
    QVERIFY(!page.studyEnabledCheck()->isChecked());
    QCOMPARE(page.studyFocusSpin()->value(), 30);
    QCOMPARE(page.studyBandSpin()->value(), 0.05);
    QVERIFY(page.gameEnabledCheck()->isChecked());
    QCOMPARE(page.gameIntervalSpin()->value(), 60);
    QVERIFY(page.profileEnabledCheck()->isChecked());
    QCOMPARE(page.profileCharsSpin()->value(), 2000);
    QCOMPARE(page.cookiePathEdit()->text(), QStringLiteral("config/bilibili_cookie.json"));
}

QTEST_MAIN(TestSettingsPage)
#include "test_settings_page.moc"
