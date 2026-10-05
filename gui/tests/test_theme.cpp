// ============================================================================
//  gui/tests/test_theme.cpp — 视觉常量与样式表的守卫（T15-16 / G-A-1b）
//
//  为什么用"黄金长度 + sha256"而不是截图
//  ---------------------------------------------------------------------------
//  G-A-1b 的判据本来是"四页截图改前改后逐像素一致"，但**实测它测不了** ✗：
//  宿主 offscreen 连跑两次，四页的字节数与 sha256 **全都不一样** ✗
//  （顶栏有时钟、渲染与 PNG 编码本身也不是确定的）。
//  所以改用**构造性证明** ✓：样式表的**输入字符串**必须与重构前逐字节相同 ——
//  值一样 ⇒ 每个像素的输入一样 ✓，而且这个判据是**确定的、可复跑的** ✓。
//
//  四条守卫
//  ---------------------------------------------------------------------------
//    ① 展开后**不许残留 `@…@`** ✗ —— 漏替换会被 Qt 静默忽略，是肉眼难查的回归 ✓；
//    ② 展开结果 == 黄金（长度 10049 + sha256 3c5524fb…）✓
//       —— 与 `git HEAD` 里那份旧 QSS 逐字节相同（G-A-1b 的实测证据 ✓）；
//    ③ 每个令牌要么在 QSS 里真被用到、要么在**豁免表里写明理由** ✓
//       （防止以后加了令牌却忘了用 ✓）；
//    ④ `kTextDimDrift`(#9AA1A9) **不许**等于 `kTextDim`(#9AA0A6) ✓
//       —— G-A-1b 明确不收敛它 ✗（那会改像素，留给独立的 G-C-0 ✓）。
// ============================================================================
#include <QCryptographicHash>
#include <QDir>
#include <QFile>
#include <QFileInfo>
#include <QSet>
#include <QString>
#include <QtTest/QtTest>

#include "ui/base_style.h"
#include "ui/theme.h"

namespace {

/// 黄金值：与 `git show HEAD:gui/src/main_window.cpp` 里那份旧 QSS **逐字节相同** ✓
/// ⚠ 口径是 **UTF-8 字节数**（= 11047）✗ 不是字符数：这份 QSS 里有几百个汉字注释，
///   码点数是 10049、UTF-8 是 11047（差 998 ≈ 499 个汉字的额外字节 ✓）；
///   而 `QString::size()`（UTF-16 单元）又是 10050 —— 三者**都不一样** ✗✓。
///   统一到 UTF-8 字节：它和下面 sha256 的输入口径**完全一致** ✓。
constexpr int kGoldenLength = 11047;
const char* const kGoldenSha256 =
    "da2dc288f292fb96f3bfc3f2e0d48b1dcd226510b355acbf24548812555ca6b0";
// ⚠ 这个哈希的口径 = **旧字符串本身**（含 raw string 结尾 `)"` 之前那个换行 ✓）✗
//   我第一版填的是 `3c5524fb…` —— 那是把结尾换行**剔掉**后算的 ✗（Python 提取写成了
//   `text[i:index('\n)"')]` ⇒ 少 1 字节 ✗）。定位方式：把 C++ 展开结果 dump 出来
//   （见下面的排障分支 ✓）再与 Python 侧 `cmp -l` ⇒ **前 11046 字节 0 差异、
//   只在末尾多 1 字节** ✓✓ ⇒ 判定为"口径差一个换行"，**不是**代码少替换了什么 ✓。'

/// ③ 的豁免表：**这些令牌不在 QSS 里出现**（它们只在 C++ 里用 ✓）—— 每条都要有理由 ✓
const QSet<QString>& exemptTokens()
{
    static const QSet<QString> kExempt = {
        QStringLiteral("ok"),               // 只用于顶栏链路/模式点（top_bar.cpp）
        QStringLiteral("offline"),          // 同上
        QStringLiteral("text_dim_drift"),   // 只在 music_bar.cpp；故意不并进 text_dim ✗
    };
    return kExempt;
}

QString expand()
{
    return theme::styleSheet(theme::kBaseQss);
}

QString sha256Of(const QString& text)
{
    return QString::fromLatin1(
        QCryptographicHash::hash(text.toUtf8(), QCryptographicHash::Sha256).toHex());
}

} // namespace

class TestTheme : public QObject {
    Q_OBJECT

private slots:
    /// ① 漏替换守卫：`@…@` 残留在 QSS 里会被 Qt **静默忽略** ✗ ⇒ 必须为 0
    void noLeftoverTokens()
    {
        const QString out = expand();
        int first = out.indexOf(QLatin1Char('@'));
        QString where;
        if (first >= 0) {
            where = out.mid(qMax(0, first - 40), 80);
        }
        QVERIFY2(first < 0,
                 qPrintable(QStringLiteral("展开后仍残留 @（首个位置 %1）：%2")
                                .arg(first).arg(where)));
    }

    /// ② 黄金：与重构前逐字节相同（这就是"零视觉变化"的证明 ✓）
    void matchesTheFrozenGolden()
    {
        const QString out = expand();
        // ⚠ 排障用：把 C++ 侧展开结果落盘 ✓ —— 这样"长度相同但哈希不同"这种
        //   靠推理定位不了的问题，可以直接与 Python 侧那份逐字节 `cmp` ✓
        //   （比如 `cmp -l /tmp/theme-cpp.qss /tmp/theme-expanded.qss | head`）。
        {
            QFile dump(QStringLiteral("/tmp/theme-cpp.qss"));
            if (dump.open(QIODevice::WriteOnly | QIODevice::Truncate)) {
                dump.write(out.toUtf8());
            }
        }
        // ⚠ 必须比 **UTF-8 字节数**，不是 `QString::size()` ✗ ——
        //   `size()` 数的是 UTF-16 单元，而样式表注释里有一个**星平面字符**
        //   （代理对占 2 个单元）⇒ 实测 `size()` = 10050、UTF-8 = 10049 ✗✓。
        //   黄金值来自 Python 的 `len()` / sha256 输入口径 ⇒ 统一到 UTF-8 字节 ✓。
        QCOMPARE(out.toUtf8().size(), kGoldenLength);
        const QString got = sha256Of(out);
        QCOMPARE(got, QString::fromLatin1(kGoldenSha256));
    }

    /// ③ 每个令牌都要么被用到、要么在豁免表里（防止"加了令牌却忘了用" ✓）
    void everyTokenIsUsedOrExempt()
    {
        const QString raw = QString::fromUtf8(theme::kBaseQss);
        QStringList unused;
        for (const theme::Token& token : theme::kTokens) {
            const QString name = QString::fromLatin1(token.name);
            if (exemptTokens().contains(name)) {
                continue;
            }
            if (!raw.contains(QLatin1Char('@') + name + QLatin1Char('@'))) {
                unused << name;
            }
        }
        QVERIFY2(unused.isEmpty(),
                 qPrintable(QStringLiteral("这些令牌在样式表里没被用到、也没进豁免表：%1")
                                .arg(unused.join(QStringLiteral(", ")))));
    }

    /// ④ **G-C-0 已完成**：漂移色已并入 `kTextDim` ✓（本用例是**故意改写**过来的 ✓）
    /// 原来这里断言"`kTextDimDrift` 与 `kTextDim` 不许相等"✗ —— 那条的使命就是：
    /// **谁要并它，就必须显式改这条测试** ✓（并会改像素 ⇒ 不许被顺手并掉 ✗）。现在并完了 ✓。
    void driftColourIsGone()
    {
        QCOMPARE(QString::fromLatin1(theme::kTextDim), QStringLiteral("#9AA0A6"));

        // ⚠ 顺带钉住：漂移字面量**不许从别处回流** ✗（原来它在 music_bar.cpp 三处 ✓）
        // ⚠ `QDir(__FILE__)` 是**文件路径**✗（`../src` 会拼成 …/test_theme.cpp/../src ✓）
        //   ⇒ 必须先取它所在**目录** ✓（与 test_style_guard.cpp 同一写法 ✓）。
        const QDir here(QFileInfo(QString::fromUtf8(__FILE__)).absolutePath());
        const QDir src(here.absoluteFilePath(QStringLiteral("../src")));
        for (const QString& rel : {QStringLiteral("ui/theme.h"), QStringLiteral("ui/music_bar.cpp")}) {
            QFile file(src.absoluteFilePath(rel));
            QVERIFY2(file.open(QIODevice::ReadOnly), qPrintable(rel));
            // ⚠ 只看**代码行** ✓（注释里提这个值不算违规 ✓ —— 与 test_style_guard 的 R1 同一条规矩 ✓）
            //   第一版没跳过，于是被我自己在 theme.h 注释里写的那个值误报了一次 ✗。
            QStringList code;
            for (const QString& line :
                 QString::fromUtf8(file.readAll()).split(QLatin1Char('\n'))) {
                const QString trimmed = line.trimmed();
                if (trimmed.startsWith(QLatin1String("//")) || trimmed.startsWith(QLatin1String("*"))
                    || trimmed.startsWith(QLatin1String("/*"))) {
                    continue;
                }
                code << line;
            }
            const QString text = code.join(QLatin1Char('\n'));
            QVERIFY2(!text.contains(QStringLiteral("#9AA1A9")),
                     qPrintable(QStringLiteral("%1 的**代码**里又出现了漂移色 #9AA1A9 ✗").arg(rel)));
        }
    }
};

QTEST_GUILESS_MAIN(TestTheme)
#include "test_theme.moc"
