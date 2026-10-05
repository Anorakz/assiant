// ============================================================================
//  gui/tests/test_style_guard.cpp — 视觉一致性守卫（T15-16 / G-A-2）
//
//  它守什么（三条规则，都由 G-A-1b 之后的**实测现状**定下来 ✓）
//  ---------------------------------------------------------------------------
//    R1  `gui/src` 下（**theme.h 除外**）不许再出现 `#RRGGBB` 字面量 ✗
//        —— `ui/theme.h` 是色值的唯一来源 ✓（G-A-1b 已把散落的都收敛过去 ✓）。
//    R2  不许再出现 `font-size: <数字>px` ✗ —— 字号一律走 QSS 的令牌 ✓。
//    R3  裸 `QColor(0x…)` 只许出现在**白名单**里，而且**逐文件计数必须精确相等** ✗
//        —— 白名单是"棘轮"：G-C-1 每清一处就得**改小清单** ✓，否则这条会红 ✓✓
//        （如果只写"允许 video_panel 用裸色"✗，那清完之后守卫不会变紧 ✗ —— 那就白守了）。
//
//  ⚠ 为什么值得一条 C++ 测试来管"源码里有没有十六进制颜色"
//    G-A-1b 的教训：**样式散落会自然复发** ✓（这次就抓到 `#9AA1A9` 与 `#9AA0A6`
//    这种只差三位的漂移 ✗）。机械可查的规矩比"下次注意"可靠 ✓。
// ============================================================================
#include <QDir>
#include <QDirIterator>
#include <QFile>
#include <QFileInfo>
#include <QRegularExpression>
#include <QString>
#include <QStringList>
#include <QtTest/QtTest>

namespace {

/// 从 `__FILE__` 反推 `gui/src`（用例跑在 build 目录里，cwd 不可信 ✗）
QDir sourceDir()
{
    const QDir here(QFileInfo(QString::fromUtf8(__FILE__)).absolutePath());   // …/gui/tests
    return QDir(here.absoluteFilePath(QStringLiteral("../src")));
}

QStringList sourceFiles()
{
    QStringList files;
    QDirIterator it(sourceDir().absolutePath(),
                    {QStringLiteral("*.cpp"), QStringLiteral("*.h")}, QDir::Files,
                    QDirIterator::Subdirectories);
    while (it.hasNext()) {
        files << it.next();
    }
    files.sort();
    return files;
}

/// 命中行的可读描述（`文件:行号  内容`），失败时直接打出来 ✓
QStringList grep(const QRegularExpression& re, bool skipThemeHeader = false)
{
    QStringList hits;
    for (const QString& path : sourceFiles()) {
        if (skipThemeHeader && path.endsWith(QLatin1String("ui/theme.h"))) {
            continue;   // R1 的唯一豁免：色值的家 ✓
        }
        QFile file(path);
        if (!file.open(QIODevice::ReadOnly | QIODevice::Text)) {
            continue;
        }
        const QStringList lines = QString::fromUtf8(file.readAll()).split(QLatin1Char('\n'));
        for (int i = 0; i < lines.size(); ++i) {
            // ⚠ 跳过**注释行** ✗ —— 这条守卫管的是"代码里的色值"，不是散文 ✓。
            //   实测：`top_bar.cpp:9-11`、`main_window.cpp:64-65`、`icons.h:6` 都是正经文档注释，
            //   里面提到色值并不违规 ✓（第一版没跳过 ⇒ 报了 6 处假违规 ✗）。
            const QString trimmed = lines.at(i).trimmed();
            if (trimmed.startsWith(QLatin1String("//")) || trimmed.startsWith(QLatin1String("*"))
                || trimmed.startsWith(QLatin1String("/*"))) {
                continue;
            }
            if (re.match(lines.at(i)).hasMatch()) {
                hits << QStringLiteral("%1:%2  %3")
                            .arg(sourceDir().relativeFilePath(path)).arg(i + 1)
                            .arg(lines.at(i).trimmed());
            }
        }
    }
    return hits;
}

/// R3 的白名单（相对 `gui/src` 的路径 → 允许的**裸 QColor 次数**）
/// ⚠ 这是棘轮：G-C-1 清一处就改小一处 ✓（清单清空时这条规则才算真正白守 ✓）
const QHash<QString, int>& bareColourWhitelist()
{
    static const QHash<QString, int> kAllow = {
        {QStringLiteral("ui/video_panel.cpp"), 6},   // 视频控制条几个按钮的图标染色
        {QStringLiteral("main_window.cpp"), 1},      // 壁纸缺失时的兜底底色
    };
    return kAllow;
}

int totalAllowedBareColours()
{
    int total = 0;
    for (int n : bareColourWhitelist()) {
        total += n;
    }
    return total;
}

} // namespace

class TestStyleGuard : public QObject {
    Q_OBJECT

private slots:
    void sourceTreeIsFound()
    {
        QVERIFY2(sourceDir().exists(),
                 qPrintable(QStringLiteral("找不到 gui/src：%1").arg(sourceDir().absolutePath())));
        QVERIFY2(sourceFiles().size() > 20,
                 qPrintable(QStringLiteral("只扫到 %1 个源文件，路径大概不对 ✗")
                                .arg(sourceFiles().size())));
    }

    /// R1：`#RRGGBB` 只许活在 `ui/theme.h`
    void noHexColourLiteralsOutsideTheme()
    {
        const QStringList hits =
            grep(QRegularExpression(QStringLiteral("#[0-9A-Fa-f]{6}\\b")), true);
        QVERIFY2(hits.isEmpty(),
                 qPrintable(QStringLiteral("theme.h 之外还有 %1 处十六进制色值（应改走 theme:: 常量）：\n%2")
                                .arg(hits.size()).arg(hits.join(QLatin1Char('\n')))));
    }

    /// R2：字号一律走令牌，不许出现 `font-size: 15px` 这种
    void noFontSizeLiterals()
    {
        const QStringList hits =
            grep(QRegularExpression(QStringLiteral("font-size:\\s*[0-9]+px")));
        QVERIFY2(hits.isEmpty(),
                 qPrintable(QStringLiteral("还有 %1 处字号字面量（应走 theme 令牌）：\n%2")
                                .arg(hits.size()).arg(hits.join(QLatin1Char('\n')))));
    }

    /// R3：裸 `QColor(0x…)` 逐文件计数必须**等于**白名单（棘轮 ✓）
    void bareColoursShrinkOnlyThroughTheWhitelist()
    {
        QHash<QString, int> actual;
        for (const QString& hit : grep(QRegularExpression(QStringLiteral("QColor\\(0x")))) {
            const QString file = hit.section(QLatin1Char(':'), 0, 0);
            actual[file] += 1;
        }

        QStringList problems;
        int total = 0;
        for (auto it = bareColourWhitelist().cbegin(); it != bareColourWhitelist().cend(); ++it) {
            const int got = actual.value(it.key(), 0);
            total += got;
            if (got != it.value()) {
                problems << QStringLiteral("%1: 实测 %2 处，白名单写的是 %3（清掉了就把清单改小 ✓）")
                                .arg(it.key()).arg(got).arg(it.value());
            }
            actual.remove(it.key());
        }
        for (auto it = actual.cbegin(); it != actual.cend(); ++it) {
            problems << QStringLiteral("%1: 有 %2 处裸 QColor 但**不在**白名单里（新加的 ✗，"
                                       "请改用 theme:: 常量）")
                            .arg(it.key()).arg(it.value());
        }
        QVERIFY2(problems.isEmpty(),
                 qPrintable(QStringLiteral("裸 QColor 与白名单不一致：\n%1\n（白名单总数 %2）")
                                .arg(problems.join(QLatin1Char('\n'))).arg(totalAllowedBareColours())));
    }
};

QTEST_GUILESS_MAIN(TestStyleGuard)
#include "test_style_guard.moc"
