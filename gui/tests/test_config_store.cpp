// ============================================================================
//  gui/tests/test_config_store.cpp — ConfigStore 单测（文本级键路径替换）
//
//  重点验证"保注释、保顺序、只动目标行"，以及出错时**拒绝写入**。
//  另加 T13-9 的一组：**载入模板之后的缺段/缺块新建**（与 settings_config.py 同口径）。
// ============================================================================
#include <QCoreApplication>
#include <QDir>
#include <QFile>
#include <QTemporaryDir>
#include <QtTest/QtTest>

#include "core/config_store.h"

using core::ConfigStore;
using core::Flavor;
using core::LineChange;

namespace {

bool writeFile(const QString& path, const QString& content)
{
    QFile file(path);
    if (!file.open(QIODevice::WriteOnly | QIODevice::Truncate)) {
        return false;
    }
    const QByteArray bytes = content.toUtf8();
    return file.write(bytes) == bytes.size();
}

QString readFile(const QString& path)
{
    QFile file(path);
    if (!file.open(QIODevice::ReadOnly)) {
        return QString();
    }
    return QString::fromUtf8(file.readAll());
}

const char* const kYaml = "# 顶层注释\n"
                          "llm:\n"
                          "  # 字段约定注释（必须保住）\n"
                          "  mode: disabled\n"
                          "  model_path: /old/path.rknn\n"
                          "  cloud:\n"
                          "    base: https://api.example.com/v1\n"
                          "    key: \"\"\n"
                          "\n"
                          "other:\n"
                          "  flag: true   # 行尾注释\n";

/// T13-9 的模板夹具（板端那份是 `config/config.example.yaml`，这里只要形状像：
/// 顶层段 + 嵌套块 + 键上面的说明注释 + 段末尾的"父级注释"）。
const char* const kTemplate =
    "# ---- 学习监督 ----\n"
    "# 干什么: 判“现在是不是学习内容”。\n"
    "study:\n"
    "  # 关掉它 = 不做学习监督\n"
    "  enabled: false\n"
    "  # 判成学习之后多久再看\n"
    "  focus_interval_min: 30\n"
    "  # 没把握带\n"
    "  relative_band: 0.05\n"
    "\n"
    "# ---- B 站 ----\n"
    "bilibili:\n"
    "  enabled: true\n"
    "  # ---- 画面 -> 游戏 ----\n"
    "  game_watch:\n"
    "    enabled: true\n"
    "    interval_s: 60\n"
    "  cookie_file: config/bilibili_cookie.json\n"
    "\n"
    "# ---- 画像 ----\n"
    "profile:\n"
    "  enabled: true\n"
    "  trigger_chars: 2000\n";

/// 写一份模板，返回模板路径。
QString writeTemplate(const QTemporaryDir& dir)
{
    const QString path = dir.filePath(QStringLiteral("config.example.yaml"));
    return writeFile(path, QString::fromUtf8(kTemplate)) ? path : QString();
}

} // namespace

class TestConfigStore : public QObject {
    Q_OBJECT

private slots:
    void yamlReadsNestedScalars();
    void yamlKeepsCommentsOnSave();
    void yamlBacksUpOriginal();
    void yamlInsertsMissingKeyUnderParent();
    void yamlRefusesWhenParentMissing();
    void yamlQuotesTrickyValues();
    void inMemorySetIsVisibleBeforeSave();
    void loadMissingFileFails();
    void envFlavorRoundTrip();

    // ---- T13-9: 模板驱动的缺段/缺块新建 ----
    void createsMissingSegmentFromTemplate();
    void createsMissingNestedBlockFromTemplate();
    void insertsMissingKeyWithTemplateComment();
    void typeFollowsTemplate();
    void refusesKeyWhoseBlockIsNotInTemplate();
    void refusesToCreateWithoutTemplate();
    void keepsTrailingCommentAlignment();
};

void TestConfigStore::yamlReadsNestedScalars()
{
    QTemporaryDir dir;
    QVERIFY(dir.isValid());
    const QString path = dir.filePath(QStringLiteral("a.yaml"));
    QVERIFY(writeFile(path, QString::fromUtf8(kYaml)));

    ConfigStore store;
    QString error;
    QVERIFY2(store.load(path, &error), qPrintable(error));

    QCOMPARE(store.value(QStringLiteral("llm.mode")), QStringLiteral("disabled"));
    QCOMPARE(store.value(QStringLiteral("llm.model_path")), QStringLiteral("/old/path.rknn"));
    QCOMPARE(store.value(QStringLiteral("llm.cloud.base")),
             QStringLiteral("https://api.example.com/v1"));
    QCOMPARE(store.value(QStringLiteral("llm.cloud.key")), QString());
    QCOMPARE(store.boolValue(QStringLiteral("other.flag"), false), true);
    QCOMPARE(store.value(QStringLiteral("不存在")), QString());
    QCOMPARE(store.value(QStringLiteral("llm.nope"), QStringLiteral("默认")),
             QStringLiteral("默认"));
}

void TestConfigStore::yamlKeepsCommentsOnSave()
{
    QTemporaryDir dir;
    QVERIFY(dir.isValid());
    const QString path = dir.filePath(QStringLiteral("a.yaml"));
    QVERIFY(writeFile(path, QString::fromUtf8(kYaml)));

    ConfigStore store;
    QVERIFY(store.load(path));
    store.set(QStringLiteral("llm.model_path"),
              QStringLiteral("/home/kickpi/model/Qwen3-0.6B-Q4_K_M.gguf"));
    QString error;
    QVERIFY2(store.save(&error), qPrintable(error));

    const QString text = readFile(path);
    // 只动了目标行（注意断言用"两空格 + 键名"，能抓住"把全路径写进文件"这类错误）
    QVERIFY(text.contains(QStringLiteral("\n  model_path: /home/kickpi/model/Qwen3-0.6B-Q4_K_M.gguf")));
    QVERIFY(!text.contains(QStringLiteral("llm.model_path")));        // 全路径不许泄漏进文件
    QVERIFY(text.contains(QStringLiteral("  # 字段约定注释（必须保住）")));
    QVERIFY(text.contains(QStringLiteral("# 顶层注释")));
    QVERIFY(text.contains(QStringLiteral("other:")));
    QVERIFY(text.contains(QStringLiteral("  flag: true   # 行尾注释")));   // 行尾注释也保住
    QVERIFY(text.contains(QStringLiteral("  mode: disabled")));           // 未涉及的键原样

    // 行数不变（替换而不是重排）
    QCOMPARE(text.count(QLatin1Char('\n')), QString::fromUtf8(kYaml).count(QLatin1Char('\n')));
    // 重新索引后能读到新值
    QCOMPARE(store.value(QStringLiteral("llm.model_path")),
             QStringLiteral("/home/kickpi/model/Qwen3-0.6B-Q4_K_M.gguf"));
    QVERIFY(!store.hasPending());
}

void TestConfigStore::yamlBacksUpOriginal()
{
    QTemporaryDir dir;
    QVERIFY(dir.isValid());
    const QString path = dir.filePath(QStringLiteral("a.yaml"));
    QVERIFY(writeFile(path, QString::fromUtf8(kYaml)));

    ConfigStore store;
    QVERIFY(store.load(path));
    store.set(QStringLiteral("llm.mode"), QStringLiteral("cloud"));
    QVERIFY(store.save());

    const QString bak = readFile(path + QStringLiteral(".bak"));
    QCOMPARE(bak, QString::fromUtf8(kYaml));       // 备份是改动前的原文
    QVERIFY(readFile(path).contains(QStringLiteral("  mode: cloud")));
}

void TestConfigStore::yamlInsertsMissingKeyUnderParent()
{
    QTemporaryDir dir;
    QVERIFY(dir.isValid());
    const QString path = dir.filePath(QStringLiteral("a.yaml"));
    QVERIFY(writeFile(path, QStringLiteral("llm:\n  mode: disabled\nother:\n  flag: true\n")));

    ConfigStore store;
    QVERIFY(store.load(path));
    store.set(QStringLiteral("llm.port"), QStringLiteral("9000"));

    QVector<LineChange> changes;
    QString error;
    QVERIFY2(store.planChanges(&changes, &error), qPrintable(error));
    QCOMPARE(changes.size(), 1);
    QCOMPARE(changes.first().lineNo, -1);                       // 新增
    QCOMPARE(changes.first().newLine, QStringLiteral("  port: 9000"));   // 父块缩进 + 2

    QVERIFY(store.save());
    const QString text = readFile(path);
    QVERIFY(text.contains(QStringLiteral("  port: 9000")));
    // 插在 llm 块里、other 之前
    QVERIFY(text.indexOf(QStringLiteral("  port: 9000"))
            < text.indexOf(QStringLiteral("other:")));
    QCOMPARE(store.value(QStringLiteral("llm.port")), QStringLiteral("9000"));
}

void TestConfigStore::yamlRefusesWhenParentMissing()
{
    QTemporaryDir dir;
    QVERIFY(dir.isValid());
    const QString path = dir.filePath(QStringLiteral("a.yaml"));
    const QString original = QStringLiteral("llm:\n  mode: disabled\n");
    QVERIFY(writeFile(path, original));

    ConfigStore store;
    QVERIFY(store.load(path));
    store.set(QStringLiteral("nosuchblock.key"), QStringLiteral("x"));

    QString error;
    QVERIFY(!store.save(&error));
    QVERIFY(error.contains(QStringLiteral("拒绝写入")));
    QCOMPARE(readFile(path), original);                          // 文件一个字节没动
    QVERIFY(!QFile::exists(path + QStringLiteral(".bak")));       // 也没留备份
}

void TestConfigStore::yamlQuotesTrickyValues()
{
    QTemporaryDir dir;
    QVERIFY(dir.isValid());
    const QString path = dir.filePath(QStringLiteral("a.yaml"));
    QVERIFY(writeFile(path, QStringLiteral("llm:\n  mode: disabled\n")));

    ConfigStore store;
    QVERIFY(store.load(path));
    store.set(QStringLiteral("llm.empty"), QString());
    store.set(QStringLiteral("llm.commentish"), QStringLiteral("a # b"));
    QVERIFY(store.save());

    ConfigStore reread;
    QVERIFY(reread.load(path));
    QCOMPARE(reread.value(QStringLiteral("llm.empty")), QString());
    QCOMPARE(reread.value(QStringLiteral("llm.commentish")), QStringLiteral("a # b"));
}

// set() 只是"写入意图"，但读取应该立刻看得到新值（--debug 这类内存覆盖依赖它）
void TestConfigStore::inMemorySetIsVisibleBeforeSave()
{
    QTemporaryDir dir;
    QVERIFY(dir.isValid());
    const QString path = dir.filePath(QStringLiteral("config.yaml"));
    QVERIFY(writeFile(path, QStringLiteral("debug: false\nwake:\n  idle_ms: 5000\n")));

    ConfigStore store;
    QVERIFY(store.load(path));
    QCOMPARE(store.boolValue(QStringLiteral("debug"), false), false);

    store.setBool(QStringLiteral("debug"), true);
    QCOMPARE(store.value(QStringLiteral("debug")), QStringLiteral("true"));
    QCOMPARE(store.boolValue(QStringLiteral("debug"), false), true);      // 未落盘也可见
    QVERIFY(store.contains(QStringLiteral("debug")));

    store.set(QStringLiteral("wake.idle_ms"), QStringLiteral("2500"));
    QCOMPARE(store.intValue(QStringLiteral("wake.idle_ms"), 5000), 2500);

    // 新增的键同样立刻可读
    store.set(QStringLiteral("theme"), QStringLiteral("grey"));
    QCOMPARE(store.value(QStringLiteral("theme")), QStringLiteral("grey"));

    // 文件此刻还没变
    QVERIFY(readFile(path).contains(QStringLiteral("debug: false")));

    QVERIFY(store.save());
    QVERIFY(readFile(path).contains(QStringLiteral("debug: true")));
    // T13-9: 顶层新键必须插成 **0 空格** —— 早先一律按"父块缩进 + 2"算，根块的缩进是 -1，
    // 于是写成 1 个空格，读回来就成了上一个块的子键（`wake.theme`）。
    QVERIFY2(readFile(path).contains(QStringLiteral("\ntheme: grey")), qPrintable(readFile(path)));
    QCOMPARE(store.value(QStringLiteral("theme")), QStringLiteral("grey"));
    QVERIFY(!store.contains(QStringLiteral("wake.theme")));
    QCOMPARE(store.boolValue(QStringLiteral("debug"), false), true);
}

void TestConfigStore::envFlavorRoundTrip()
{
    QTemporaryDir dir;
    QVERIFY(dir.isValid());
    const QString path = dir.filePath(QStringLiteral("llm.env"));
    const QString original = QStringLiteral(
        "# 分节注释\n"
        "LLM_MODEL_PATH=/home/kickpi/model/Qwen3-0.6B-Q4_K_M.gguf\n"
        "LLM_MODEL_NAME=qwen3-0.6b\n"
        "\n"
        "LLM_PORT=9000\n");
    QVERIFY(writeFile(path, original));

    ConfigStore env(Flavor::Env);
    QString error;
    QVERIFY2(env.load(path, &error), qPrintable(error));
    QCOMPARE(env.value(QStringLiteral("LLM_PORT")), QStringLiteral("9000"));

    env.set(QStringLiteral("LLM_MODEL_NAME"), QStringLiteral("qwen3.5-0.8b"));
    env.set(QStringLiteral("LLM_NEW"), QStringLiteral("1"));     // 新键 → 追加到末尾
    QVERIFY2(env.save(&error), qPrintable(error));

    const QString text = readFile(path);
    QVERIFY(text.contains(QStringLiteral("LLM_MODEL_NAME=qwen3.5-0.8b")));
    QVERIFY(text.contains(QStringLiteral("# 分节注释")));
    QVERIFY(text.contains(QStringLiteral("LLM_MODEL_PATH=/home/kickpi/model/Qwen3-0.6B-Q4_K_M.gguf")));
    QCOMPARE(text.count(QLatin1Char('\n')), original.count(QLatin1Char('\n')) + 1);
}

void TestConfigStore::loadMissingFileFails()
{
    // 不存在的路径必须**失败**：否则调用方 set+save 会造出只有个别键的残桩配置，
    // 把真正的 config/config.yaml 顶掉（T9 出图时真的发生过）。
    const QString path = QDir::tempPath() + QStringLiteral("/config_missing_%1.yaml")
                                             .arg(QCoreApplication::applicationPid());
    QFile::remove(path);
    core::ConfigStore store;
    QString error;
    QVERIFY(!store.load(path, &error));
    QVERIFY(!error.isEmpty());
    QVERIFY(!QFile::exists(path));          // 加载失败不许顺手创建文件

    // 只有真加载成功了，save 才可能写出文件
    store.set(QStringLiteral("k"), QStringLiteral("v"));   // set() 返回 void
    QVERIFY(!store.save(&error));           // 没加载过 → 不写
    QVERIFY(!QFile::exists(path));
}

// ---------------------------------------------------------------------------
//  T13-9: 载入模板之后的"缺段 / 缺块新建"（与 settings_config.py 同口径）
// ---------------------------------------------------------------------------
void TestConfigStore::createsMissingSegmentFromTemplate()
{
    QTemporaryDir dir;
    QVERIFY(dir.isValid());
    const QString templatePath = writeTemplate(dir);
    QVERIFY(!templatePath.isEmpty());
    const QString path = dir.filePath(QStringLiteral("config.yaml"));
    const QString original = QStringLiteral("gui:\n  debug: false\n");
    QVERIFY(writeFile(path, original));

    ConfigStore store;
    QVERIFY(store.load(path));
    QString error;
    QVERIFY2(store.loadTemplate(templatePath, &error), qPrintable(error));
    store.setBool(QStringLiteral("study.enabled"), true);
    store.set(QStringLiteral("study.focus_interval_min"), QStringLiteral("30"));   // 与模板相同
    store.set(QStringLiteral("study.relative_band"), QStringLiteral("0.07"));
    QVERIFY2(store.save(&error), qPrintable(error));

    const QString text = readFile(path);
    // 原有内容一个字节没动（新段追加在末尾）
    QVERIFY2(text.startsWith(original), qPrintable(text));
    // 模板那一整块（含说明注释）搬过来了，目标键的值改成用户要的
    QVERIFY(text.contains(QStringLiteral("# ---- 学习监督 ----")));
    QVERIFY(text.contains(QStringLiteral("study:\n")));
    QVERIFY(text.contains(QStringLiteral("  # 关掉它 = 不做学习监督\n  enabled: true\n")));
    QVERIFY(text.contains(QStringLiteral("  # 没把握带\n  relative_band: 0.07\n")));
    // 值本来就跟模板一致的键不许被写第二遍
    QCOMPARE(text.count(QStringLiteral("focus_interval_min")), 1);
    // 别的段一个都没搬（只新建缺的那一块）
    QVERIFY(!text.contains(QStringLiteral("bilibili:")));
    QVERIFY(!text.contains(QStringLiteral("profile:")));
    // 备份是改动前的原文
    QCOMPARE(readFile(path + QStringLiteral(".bak")), original);

    ConfigStore after;
    QVERIFY(after.load(path));
    QVERIFY(after.boolValue(QStringLiteral("study.enabled"), false));
    QCOMPARE(after.value(QStringLiteral("study.relative_band")), QStringLiteral("0.07"));
    QCOMPARE(after.value(QStringLiteral("study.focus_interval_min")), QStringLiteral("30"));
    QCOMPARE(after.value(QStringLiteral("gui.debug")), QStringLiteral("false"));
}

void TestConfigStore::createsMissingNestedBlockFromTemplate()
{
    QTemporaryDir dir;
    QVERIFY(dir.isValid());
    const QString templatePath = writeTemplate(dir);
    const QString path = dir.filePath(QStringLiteral("config.yaml"));
    QVERIFY(writeFile(path, QStringLiteral("bilibili:\n  enabled: true\n")));

    ConfigStore store;
    QVERIFY(store.load(path));
    QString error;
    QVERIFY2(store.loadTemplate(templatePath, &error), qPrintable(error));
    // 段在、**中间那块不在**：缺的是 bilibili.game_watch 这一块（不是整个 bilibili 段）
    store.set(QStringLiteral("bilibili.game_watch.interval_s"), QStringLiteral("90"));
    QVERIFY2(store.save(&error), qPrintable(error));

    const QString text = readFile(path);
    QVERIFY2(text.contains(QStringLiteral("  # ---- 画面 -> 游戏 ----\n"
                                          "  game_watch:\n"
                                          "    enabled: true\n"
                                          "    interval_s: 90\n")),
             qPrintable(text));
    // 段里原有的键一个没动
    QVERIFY(text.startsWith(QStringLiteral("bilibili:\n  enabled: true\n")));
    // 只搬了缺的那一块：段里别的兄弟键（模板里在 game_watch 之后的 cookie_file）没被拖进来
    QVERIFY(!text.contains(QStringLiteral("cookie_file")));
    // 缩进没错层：game_watch 在段里（2 空格），不是根上的顶层键
    QVERIFY(!text.contains(QStringLiteral("\ngame_watch:")));

    ConfigStore after;
    QVERIFY(after.load(path));
    QCOMPARE(after.value(QStringLiteral("bilibili.game_watch.interval_s")), QStringLiteral("90"));
    QCOMPARE(after.value(QStringLiteral("bilibili.enabled")), QStringLiteral("true"));
}

void TestConfigStore::insertsMissingKeyWithTemplateComment()
{
    QTemporaryDir dir;
    QVERIFY(dir.isValid());
    const QString templatePath = writeTemplate(dir);
    const QString path = dir.filePath(QStringLiteral("config.yaml"));
    QVERIFY(writeFile(path, QStringLiteral("study:\n  enabled: false\n")));

    ConfigStore store;
    QVERIFY(store.load(path));
    QVERIFY(store.loadTemplate(templatePath));
    store.set(QStringLiteral("study.relative_band"), QStringLiteral("0.09"));
    QVERIFY(store.save());

    // 段在、键不在：插到段末，并带上模板里这个键上面的注释
    const QString text = readFile(path);
    QVERIFY2(text.contains(QStringLiteral("  enabled: false\n  # 没把握带\n  relative_band: 0.09\n")),
             qPrintable(text));
}

void TestConfigStore::typeFollowsTemplate()
{
    QTemporaryDir dir;
    QVERIFY(dir.isValid());
    const QString templatePath = writeTemplate(dir);
    const QString path = dir.filePath(QStringLiteral("config.yaml"));
    const QString original = QStringLiteral("study:\n  enabled: false\n");
    QVERIFY(writeFile(path, original));

    ConfigStore store;
    QVERIFY(store.load(path));
    QVERIFY(store.loadTemplate(templatePath));
    QString error;

    // 模板里是浮点 -> 给文字必须拒绝（YAML 不报错，Agent 读出来却是另一个东西）
    store.set(QStringLiteral("study.relative_band"), QStringLiteral("abc"));
    QVERIFY(!store.save(&error));
    QVERIFY(error.contains(QStringLiteral("拒绝写入")));
    QCOMPARE(readFile(path), original);
    QVERIFY(!QFile::exists(path + QStringLiteral(".bak")));

    // 模板里是布尔
    store.clearPending();
    store.set(QStringLiteral("study.enabled"), QStringLiteral("maybe"));
    QVERIFY(!store.save(&error));

    // 模板里是整数 -> 小数也不行、文本也不行
    store.clearPending();
    store.set(QStringLiteral("study.focus_interval_min"), QStringLiteral("1.5"));
    QVERIFY(!store.save(&error));
    store.clearPending();
    store.set(QStringLiteral("study.focus_interval_min"), QStringLiteral("x"));
    QVERIFY(!store.save(&error));

    // 类型对了就写得进去（反空转：别让上面三条是因为别的原因失败的）
    store.clearPending();
    store.set(QStringLiteral("study.focus_interval_min"), QStringLiteral("45"));
    QVERIFY2(store.save(&error), qPrintable(error));
    QCOMPARE(readFile(path).count(QStringLiteral("45")), 1);
}

void TestConfigStore::refusesKeyWhoseBlockIsNotInTemplate()
{
    QTemporaryDir dir;
    QVERIFY(dir.isValid());
    const QString templatePath = writeTemplate(dir);
    const QString path = dir.filePath(QStringLiteral("config.yaml"));
    const QString original = QStringLiteral("gui:\n  debug: false\n");
    QVERIFY(writeFile(path, original));

    ConfigStore store;
    QVERIFY(store.load(path));
    QVERIFY(store.loadTemplate(templatePath));
    store.set(QStringLiteral("nosuchblock.key"), QStringLiteral("x"));
    QString error;
    QVERIFY(!store.save(&error));
    QVERIFY2(error.contains(QStringLiteral("没法新建")), qPrintable(error));
    QCOMPARE(readFile(path), original);       // 一个字节没动
}

void TestConfigStore::refusesToCreateWithoutTemplate()
{
    // 没载入模板时行为与 T13-9 之前**完全一致**：父块不存在就拒绝写入
    // （板端老配置 + 模板丢了的时候，宁可不写也不许凭空造残桩）。
    QTemporaryDir dir;
    QVERIFY(dir.isValid());
    const QString path = dir.filePath(QStringLiteral("config.yaml"));
    const QString original = QStringLiteral("gui:\n  debug: false\n");
    QVERIFY(writeFile(path, original));

    ConfigStore store;
    QVERIFY(store.load(path));
    QVERIFY(!store.hasTemplate());
    store.set(QStringLiteral("study.enabled"), QStringLiteral("true"));
    QString error;
    QVERIFY(!store.save(&error));
    QVERIFY(error.contains(QStringLiteral("找不到键")));
    QCOMPARE(readFile(path), original);
}

void TestConfigStore::keepsTrailingCommentAlignment()
{
    // 改一行的值时，行尾注释**连对齐空白**一起留在原地（与 settings_config.py 同一口径）。
    // 早先只留一个空格：写一次值，整段的注释对齐就散了（T13-9 冒烟时发现的）。
    QTemporaryDir dir;
    QVERIFY(dir.isValid());
    const QString path = dir.filePath(QStringLiteral("a.yaml"));
    QVERIFY(writeFile(path, QStringLiteral("llm:\n  mode: disabled      # 对齐的注释\n")));

    ConfigStore store;
    QVERIFY(store.load(path));
    store.set(QStringLiteral("llm.mode"), QStringLiteral("cloud"));
    QVERIFY(store.save());
    QCOMPARE(readFile(path), QStringLiteral("llm:\n  mode: cloud      # 对齐的注释\n"));
}

QTEST_APPLESS_MAIN(TestConfigStore)
#include "test_config_store.moc"