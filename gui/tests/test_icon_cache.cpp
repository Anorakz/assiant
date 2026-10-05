// ============================================================================
//  gui/tests/test_icon_cache.cpp — 图标染色缓存守卫（T15-16 / G-B-2）
//
//  判据（机械、可复跑 ✓）
//  ---------------------------------------------------------------------------
//    · 同一 `(图标, 颜色)` 调 3 次 ⇒ **只渲染 1 次** ✓（改前是 3 次 ✗）；
//    · 换个颜色 ⇒ **必须重渲染** ✓（缓存键含颜色 ✓，不能把颜色吞掉 ✗）；
//    · **同色不同 alpha** ⇒ 也算不同图标 ✓（键用 `HexArgb` ✓ —— 这条最容易被写漏 ✗）；
//    · 一条 `theCounterActuallyMoves`：防止上面几条退化成"0 == 0"的空断言 ✗。
//
//  ⚠ 它依赖 `../resources/icons.qrc`（`:/icons/*.svg` ✓）—— 注册在
//    `gui/tests/CMakeLists.txt` 那个 foreach 里，那个 foreach 会一并编进 qrc ✓。
// ============================================================================
#include <QColor>
#include <QIcon>
#include <QtTest/QtTest>

#include "ui/icons.h"

class TestIconCache : public QObject {
    Q_OBJECT

private slots:
    void sameNameAndColourOnlyRendersOnce()
    {
        ui::resetTintedIconCache();
        const QColor text(0xE6, 0xE6, 0xE6);

        const QIcon first = ui::tintedIcon(QStringLiteral("play"), text);
        QVERIFY2(!first.isNull(), "图标取不到 —— qrc 没编进来？✗");

        ui::tintedIcon(QStringLiteral("play"), text);
        ui::tintedIcon(QStringLiteral("play"), text);
        QCOMPARE(ui::tintedIconRenders(), 1);      // 三次调用、一次渲染 ✓

        // 不同颜色 ⇒ 必须重渲染 ✓（缓存键含颜色 ✓）
        ui::tintedIcon(QStringLiteral("play"), QColor(0x12, 0x14, 0x1A));
        QCOMPARE(ui::tintedIconRenders(), 2);

        // 同色不同 alpha ⇒ 也算另一种图标 ✓（HexArgb 键 ✓）
        QColor semi(0xE6, 0xE6, 0xE6);
        semi.setAlpha(128);
        ui::tintedIcon(QStringLiteral("play"), semi);
        QCOMPARE(ui::tintedIconRenders(), 3);

        // 不同名字 ⇒ 各自一份 ✓
        ui::tintedIcon(QStringLiteral("next"), text);
        QCOMPARE(ui::tintedIconRenders(), 4);
    }

    void differentNamesShareNothing()
    {
        ui::resetTintedIconCache();
        const QColor text(0xE6, 0xE6, 0xE6);
        ui::tintedIcon(QStringLiteral("home"), text);
        ui::tintedIcon(QStringLiteral("model"), text);
        QCOMPARE(ui::tintedIconRenders(), 2);
        ui::tintedIcon(QStringLiteral("home"), text);      // 命中缓存 ✓
        QCOMPARE(ui::tintedIconRenders(), 2);
    }

    /// 防"0 == 0"：计数本身必须真的会动 ✓
    void theCounterActuallyMoves()
    {
        ui::resetTintedIconCache();
        QCOMPARE(ui::tintedIconRenders(), 0);
        ui::tintedIcon(QStringLiteral("system"), QColor(0x7A, 0xA2, 0xF7));
        QVERIFY2(ui::tintedIconRenders() >= 1,
                 "渲染计数没动 —— 要么缓存把首次渲染也吞了 ✗，要么计数没接上 ✗");
    }
};

QTEST_MAIN(TestIconCache)
#include "test_icon_cache.moc"
