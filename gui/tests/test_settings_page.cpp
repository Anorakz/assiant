// ============================================================================
//  gui/tests/test_settings_page.cpp — 设置页测试（T13 / T13-9）
//
//  验：载入配置、保存写回 config.yaml 的 gui: 段（含**控制条独立时间**）、恢复默认只改界面不写文件、
//  路径缺失不崩。全部用临时 config.yaml，不碰生产配置。
//
//  T13-9 另加：三张卡片（学习监督 / 游戏检测含 B 站凭据 / 画像压缩）载入与保存、
//  **缺段就按模板新建**、以及"保存只许动白名单里的键"的逐行契约测试
//  （拿仓库里那份 config.example.yaml 当真源）。
// ============================================================================
#include <QCheckBox>
#include <QComboBox>
#include <QDir>
#include <QDoubleSpinBox>
#include <QFile>
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

QString readText(const QString& path)
{
    QFile file(path);
    if (!file.open(QIODevice::ReadOnly)) {
        return QString();
    }
    return QString::fromUtf8(file.readAll());
}

int indentOfLine(const QString& line)
{
    int n = 0;
    while (n < line.size() && line.at(n) == QLatin1Char(' ')) {
        ++n;
    }
    return n;
}

/// 一行的键名（`  relative_band: 0.05` -> `relative_band`）。
QString keyOfLine(const QString& line)
{
    const QString trimmed = line.trimmed();
    const int colon = trimmed.indexOf(QLatin1Char(':'));
    return colon > 0 ? trimmed.left(colon).trimmed() : QString();
}

/// 某一行在 YAML 里的**点号键路径**（靠缩进往上走；够用于"逐行 diff 出键名"）。
QString dottedKey(const QStringList& lines, int index)
{
    QStringList parts;
    parts.prepend(keyOfLine(lines.at(index)));
    int want = indentOfLine(lines.at(index)) - 2;
    for (int i = index - 1; i >= 0 && want >= 0; --i) {
        const QString line = lines.at(i);
        const QString trimmed = line.trimmed();
        if (trimmed.isEmpty() || trimmed.startsWith(QLatin1Char('#'))) {
            continue;
        }
        const int indent = indentOfLine(line);
        if (indent == want) {
            parts.prepend(keyOfLine(line));
            want -= 2;
        } else if (indent < want) {
            break;
        }
    }
    return parts.join(QLatin1Char('.'));
}

/// 设置页保存时**准许动的键**（T13-9 验收：白名单按**键**列出来，不是按段）。
bool isWhitelistedKey(const QString& key)
{
    static const QStringList kPrefixes = {QStringLiteral("gui."), QStringLiteral("llm.")};
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
    for (const QString& prefix : kPrefixes) {
        if (key.startsWith(prefix)) {
            return true;
        }
    }
    return kExact.contains(key);
}

} // namespace

class TestSettingsPage : public QObject {
    Q_OBJECT

private slots:
    void loadFromConfigFillsWidgets();
    void saveWritesConfigWithSeparateTimeouts();
    void saveKeepsAgentSectionsUntouched();
    void restoreDefaultsOnlyTouchesUi();
    void missingConfigIsReportedNotCrash();

    // ---- T13-9: 三张卡片 ----
    void cardsLoadFromConfig();
    void cardsCreateMissingSections();
    void cookieFieldsWriteCredentialFile();
    void cardsRefuseToBeWrittenWithoutTemplate();
    void saveOnlyTouchesWhitelistedKeys();
    void restoreDefaultsAlsoResetsCards();

private:
    QString writeConfig(QTemporaryDir& tmp, const QString& extra = QString());
    QString writeCardsConfig(QTemporaryDir& tmp);
    /// 把仓库里那份真模板抄进 tmp（`<config 同目录>/config.example.yaml`）。
    bool copyTemplate(const QTemporaryDir& tmp);
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

QString TestSettingsPage::writeCardsConfig(QTemporaryDir& tmp)
{
    // T13-9 的三段（板端真源里现在**没有** study: 段，所以这两种形状都要覆盖）
    return writeConfig(tmp, QStringLiteral(
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
        "  cookie_file: /tmp/t13-9-not-a-real-cookie.json\n"
        "  game_watch:\n"
        "    enabled: false\n"
        "    interval_s: 30\n"
        "    confident_score: 0.90\n"
        "\n"
        "profile:\n"
        "  enabled: false\n"
        "  trigger_chars: 500\n"
        "  trigger_turns: 5\n"));
}

bool TestSettingsPage::copyTemplate(const QTemporaryDir& tmp)
{
#ifdef SETTINGS_TEMPLATE_PATH
    const QString source = QStringLiteral(SETTINGS_TEMPLATE_PATH);
    if (!QFile::exists(source)) {
        return false;
    }
    return QFile::copy(source, tmp.path() + QStringLiteral("/config.example.yaml"));
#else
    Q_UNUSED(tmp);
    return false;
#endif
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
    // T13-9 起本页也管 study./bilibili.game_watch./profile. 那几段：真源里没有就按模板新建，
    // 所以这条老用例也得把模板放到位（板端它一直在仓库里）。
    QVERIFY2(copyTemplate(tmp), "拿不到仓库里那份 config.example.yaml");
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
    //    GUI 保存时只能动白名单里的键, 其它段(含注释与顺序)必须一个字节都不变 ——
    //    否则"改个休眠时间"就会顺手把 Agent 的配置重排掉。
    //    T13-9 起白名单**按键**列出来（多了 study./bilibili.game_watch./profile. 那几个），
    //    逐行的机械闸门在 `saveOnlyTouchesWhitelistedKeys`；这条用例是粗粒度的老底。
    QTemporaryDir tmp;
    QVERIFY(tmp.isValid());
    QVERIFY2(copyTemplate(tmp), "拿不到仓库里那份 config.example.yaml");
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

// ---------------------------------------------------------------------------
//  T13-9: 三张卡片（学习监督 / 游戏检测含 B 站凭据 / 画像压缩）
// ---------------------------------------------------------------------------
void TestSettingsPage::cardsLoadFromConfig()
{
    QTemporaryDir tmp;
    QVERIFY(tmp.isValid());
    const QString path = writeCardsConfig(tmp);

    SettingsPage page;
    page.loadFromConfig(path);

    QVERIFY(page.studyEnabledCheck()->isChecked());
    QCOMPARE(page.studyFocusSpin()->value(), 25);
    QCOMPARE(page.studyRecheckSpin()->value(), 7);
    QCOMPARE(page.studyFailuresSpin()->value(), 4);
    QCOMPARE(page.studyCooldownSpin()->value(), 45);
    QCOMPARE(page.studyProbeSpin()->value(), 2);
    QCOMPARE(page.studyBandSpin()->value(), 0.08);

    QVERIFY(!page.gameEnabledCheck()->isChecked());
    QCOMPARE(page.gameIntervalSpin()->value(), 30);
    QCOMPARE(page.gameScoreSpin()->value(), 0.90);

    QVERIFY(!page.profileEnabledCheck()->isChecked());
    QCOMPARE(page.profileCharsSpin()->value(), 500);
    QCOMPARE(page.profileTurnsSpin()->value(), 5);

    QCOMPARE(page.cookiePathEdit()->text(), QStringLiteral("/tmp/t13-9-not-a-real-cookie.json"));
    // 三个凭据框**故意不预填**：预填会在下次保存时把掩码串/旧值当成新值写回凭据文件
    QVERIFY(page.sessdataEdit()->text().isEmpty());
    QVERIFY(page.biliJctEdit()->text().isEmpty());
    QVERIFY(page.dedeUserEdit()->text().isEmpty());
    QVERIFY(page.cookieStatusLabel()->text().contains(QStringLiteral("不在（匿名）")));
}

void TestSettingsPage::cardsCreateMissingSections()
{
    // 板端真源现在**没有** study: 段 —— 保存要能按 config.example.yaml 把整段新建出来
    QTemporaryDir tmp;
    QVERIFY(tmp.isValid());
    QVERIFY2(copyTemplate(tmp), "拿不到仓库里那份 config.example.yaml，这条用例验不了缺段新建");
    const QString path = writeConfig(tmp);            // 只有 gui: 段
    const QString before = readText(path);

    SettingsPage page;
    page.loadFromConfig(path);
    page.studyEnabledCheck()->setChecked(true);
    page.studyBandSpin()->setValue(0.07);
    page.gameIntervalSpin()->setValue(45);
    page.profileCharsSpin()->setValue(1234);
    page.cookiePathEdit()->setText(tmp.path() + QStringLiteral("/cookie.json"));
    QSignalSpy spy(&page, &SettingsPage::configSaved);
    page.saveButton()->click();
    QCOMPARE(spy.count(), 1);

    const QString after = readText(path);
    QVERIFY2(after.startsWith(before), qPrintable(after));        // 原有内容一个字节没动
    QVERIFY(after.contains(QStringLiteral("study:\n")));
    QVERIFY(after.contains(QStringLiteral("  enabled: true\n")));
    QVERIFY(after.contains(QStringLiteral("  relative_band: 0.07")));
    QVERIFY(after.contains(QStringLiteral("bilibili:\n")));
    QVERIFY(after.contains(QStringLiteral("  game_watch:\n")));
    QVERIFY(after.contains(QStringLiteral("    interval_s: 45")));
    QVERIFY(after.contains(QStringLiteral("profile:\n")));
    QVERIFY(after.contains(QStringLiteral("  trigger_chars: 1234")));
    // 整段搬过来时，模板里那些没被界面改到的键也一起进来（默认值 + 它的说明注释）
    QVERIFY(after.contains(QStringLiteral("  cooldown_probe_min: 1")));
    QVERIFY(after.contains(QStringLiteral("  max_failures: 3")));
    QVERIFY(after.contains(QStringLiteral("    code.exe: code\n")));   // 结构级映射也照搬

    core::ConfigStore store;
    QVERIFY(store.load(path));
    QVERIFY(store.boolValue(QStringLiteral("study.enabled"), false));
    QCOMPARE(store.value(QStringLiteral("study.relative_band")), QStringLiteral("0.07"));
    QCOMPARE(store.value(QStringLiteral("bilibili.game_watch.interval_s")), QStringLiteral("45"));
    QCOMPARE(store.value(QStringLiteral("bilibili.cookie_file")),
             tmp.path() + QStringLiteral("/cookie.json"));
    QCOMPARE(store.value(QStringLiteral("profile.trigger_chars")), QStringLiteral("1234"));
    QCOMPARE(store.value(QStringLiteral("gui.wake.idle_ms")), QStringLiteral("7000"));   // gui: 段没被碰
}

void TestSettingsPage::cookieFieldsWriteCredentialFile()
{
    QTemporaryDir tmp;
    QVERIFY(tmp.isValid());
    QVERIFY(copyTemplate(tmp));
    const QString path = writeConfig(tmp);
    const QString cookiePath = tmp.path() + QStringLiteral("/creds/cookie.json");

    SettingsPage page;
    page.loadFromConfig(path);
    page.cookiePathEdit()->setText(cookiePath);
    page.sessdataEdit()->setText(QStringLiteral("sess-1234567890"));
    page.biliJctEdit()->setText(QStringLiteral("jct-abcdef"));
    QSignalSpy spy(&page, &SettingsPage::configSaved);
    page.saveButton()->click();
    QCOMPARE(spy.count(), 1);

    // 凭据写进的是**另一个文件**（`bilibili.cookie_file` 指的那个 JSON）
    core::CookieStore cookie;
    QVERIFY(cookie.load(cookiePath));
    QCOMPARE(cookie.value(QStringLiteral("SESSDATA")), QStringLiteral("sess-1234567890"));
    QCOMPARE(cookie.value(QStringLiteral("bili_jct")), QStringLiteral("jct-abcdef"));
    QVERIFY(!cookie.keys().contains(QStringLiteral("DedeUserID")));   // 没填的键不许凭空出现

    core::ConfigStore store;
    QVERIFY(store.load(path));
    QCOMPARE(store.value(QStringLiteral("bilibili.cookie_file")), cookiePath);

    // 填过的框清空；状态标签只给掩码，不回显原值
    QVERIFY(page.sessdataEdit()->text().isEmpty());
    QVERIFY2(page.cookieStatusLabel()->text().contains(QStringLiteral("se…90（15 位）")),
             qPrintable(page.cookieStatusLabel()->text()));
    QVERIFY(!page.cookieStatusLabel()->text().contains(QStringLiteral("sess-1234567890")));

    // 再保存一次：凭据框是空的 -> 不改动那个键，也不留多余的 .bak
    QFile::remove(cookiePath + QStringLiteral(".bak"));
    page.saveButton()->click();
    QVERIFY(!QFile::exists(cookiePath + QStringLiteral(".bak")));
    core::CookieStore again;
    QVERIFY(again.load(cookiePath));
    QCOMPARE(again.value(QStringLiteral("SESSDATA")), QStringLiteral("sess-1234567890"));
}

void TestSettingsPage::cardsRefuseToBeWrittenWithoutTemplate()
{
    // 模板不在（老部署 / 被人删了）：缺段一律**拒绝**，绝不许凭空造一份只剩几个键的残桩
    QTemporaryDir tmp;
    QVERIFY(tmp.isValid());
    const QString path = writeConfig(tmp);
    const QString before = readText(path);

    SettingsPage page;
    page.loadFromConfig(path);
    page.studyEnabledCheck()->setChecked(true);
    QString error;
    QVERIFY(!page.saveToConfig(&error));
    QVERIFY2(error.contains(QStringLiteral("拒绝写入")), qPrintable(error));
    QCOMPARE(readText(path), before);                          // 一个字节没动
    QVERIFY(!QFile::exists(path + QStringLiteral(".bak")));     // 也没留 .bak
}

void TestSettingsPage::saveOnlyTouchesWhitelistedKeys()
{
#ifdef SETTINGS_TEMPLATE_PATH
    // 拿**仓库里那份真模板**当真源：它上面有 Agent 的全部段（llm/sunshine/ipc/scheduler/…）。
    // 保存只许动白名单里的键，别的行必须逐字节不变 —— 这是"改个休眠时间顺手把 Agent
    // 配置重排掉"这类事故的机械闸门（T13-9 验收要求白名单**按键**列出来）。
    QTemporaryDir tmp;
    QVERIFY(tmp.isValid());
    QVERIFY2(copyTemplate(tmp), "拿不到仓库里那份 config.example.yaml");
    const QString path = tmp.path() + QStringLiteral("/config.yaml");
    QVERIFY(QFile::copy(QStringLiteral(SETTINGS_TEMPLATE_PATH), path));
    const QString before = readText(path);
    QVERIFY(!before.isEmpty());

    SettingsPage page;
    page.loadFromConfig(path);
    page.studyEnabledCheck()->setChecked(true);        // 每张卡片都动一下
    page.studyBandSpin()->setValue(0.08);
    page.gameIntervalSpin()->setValue(45);
    page.cookiePathEdit()->setText(tmp.path() + QStringLiteral("/cookie.json"));
    page.profileCharsSpin()->setValue(1500);
    QSignalSpy spy(&page, &SettingsPage::configSaved);
    page.saveButton()->click();
    QCOMPARE(spy.count(), 1);

    const QString after = readText(path);
    const QStringList beforeLines = before.split(QLatin1Char('\n'));
    const QStringList afterLines = after.split(QLatin1Char('\n'));
    // 这次一个键都不用新建（真源里三段都在）-> 行数必须一致（不插行、不重排）
    QCOMPARE(afterLines.size(), beforeLines.size());

    QSet<QString> changed;
    for (int i = 0; i < beforeLines.size(); ++i) {
        if (beforeLines.at(i) == afterLines.at(i)) {
            continue;
        }
        const QString key = dottedKey(afterLines, i);
        QVERIFY2(!key.isEmpty(), qPrintable(QStringLiteral("解析不出键名: ") + afterLines.at(i)));
        changed.insert(key);
    }
    QVERIFY2(!changed.isEmpty(), "一个键都没改 -> 这条用例是假绿");
    for (const QString& key : changed) {
        QVERIFY2(isWhitelistedKey(key),
                 qPrintable(QStringLiteral("保存动了白名单之外的键: %1（改动: %2）")
                                .arg(key, changed.values().join(QStringLiteral(", ")))));
    }

    // 最要紧的几处：结构级的东西与别的段原样
    QVERIFY(after.contains(QStringLiteral("  classes:\n    code: study\n    doc: study\n")));
    QVERIFY(after.contains(QStringLiteral("    WHITE ALBUM Memories like Falling Snow.exe: white album\n")));
    QVERIFY(after.contains(QStringLiteral("  commands: []\n")));
    QVERIFY(after.contains(QStringLiteral("ipc:\n")));
    QVERIFY(after.contains(QStringLiteral("sunshine:\n")));
#else
    QSKIP("没有编译期模板路径（SETTINGS_TEMPLATE_PATH），跳过白名单契约测试");
#endif
}

void TestSettingsPage::restoreDefaultsAlsoResetsCards()
{
    QTemporaryDir tmp;
    QVERIFY(tmp.isValid());
    const QString path = writeCardsConfig(tmp);      // 全是非默认值

    SettingsPage page;
    page.loadFromConfig(path);
    page.defaultsButton()->click();

    QVERIFY(!page.studyEnabledCheck()->isChecked());
    QCOMPARE(page.studyFocusSpin()->value(), 30);
    QCOMPARE(page.studyRecheckSpin()->value(), 5);
    QCOMPARE(page.studyFailuresSpin()->value(), 3);
    QCOMPARE(page.studyCooldownSpin()->value(), 30);
    QCOMPARE(page.studyProbeSpin()->value(), 1);
    QCOMPARE(page.studyBandSpin()->value(), 0.05);
    QVERIFY(page.gameEnabledCheck()->isChecked());
    QCOMPARE(page.gameIntervalSpin()->value(), 60);
    QCOMPARE(page.gameScoreSpin()->value(), 0.82);
    QVERIFY(page.profileEnabledCheck()->isChecked());
    QCOMPARE(page.profileCharsSpin()->value(), 2000);
    QCOMPARE(page.profileTurnsSpin()->value(), 12);
    QCOMPARE(page.cookiePathEdit()->text(), QStringLiteral("config/bilibili_cookie.json"));

    // 只改界面：文件一个字都没动（避免点错就改掉生产配置）
    core::ConfigStore store;
    QVERIFY(store.load(path));
    QCOMPARE(store.value(QStringLiteral("study.relative_band")), QStringLiteral("0.08"));
    QCOMPARE(store.value(QStringLiteral("profile.trigger_chars")), QStringLiteral("500"));
}

QTEST_MAIN(TestSettingsPage)
#include "test_settings_page.moc"
