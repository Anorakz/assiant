// ============================================================================
//  gui/tests/test_page_heights.cpp — 每一页都必须装得进 1280×800 的面板（T14-7b）
//
//  为什么要有这个测试
//  ---------------------------------------------------------------------------
//  板端实测：窗口在 1280×800 的屏上被撑成 **1280×883**，底部 83px 永远在屏幕外。
//  原因不是"没全屏"（`_NET_WM_STATE_FULLSCREEN` 是有的），而是**应用自己的最小尺寸
//  比屏幕高**：`xprop WM_NORMAL_HINTS` 写着 `program specified minimum size: 935 by 883`。
//
//  用 `--dump-layout` 逐控件量出来是这么条链：
//      MainWindow min 883 = 顶栏 72 + 页面栈 811
//      页面栈 min 811 = max(各页 min) = **ModelPage 的 811**（它当时还是隐藏页！）
//      —— `QStackedWidget` 的最小尺寸取**所有页**的最大值，隐藏页也算
//      而真正显示着的 MainPage 只要 602。
//
//  所以这里把"面板尺寸 - 顶栏"这条不变式钉住：**任何**页面的 minimumSizeHint
//  都必须装得进可见内容区（1280×800 屏 → 高 800-72=728、宽 1280）。
//  页面内容真的比这高，就学设置页/模型页把它放进 QScrollArea（那样 min 会掉到 ~68），
//  而不是让整个窗口长到屏幕外面去。
//
//  跑法（板端或任何有 Qt5 的地方）
//  ---------------------------------------------------------------------------
//      cd gui/build && QT_QPA_PLATFORM=offscreen ctest -R test_page_heights --output-on-failure
// ============================================================================

#include <QtTest>
#include <QScrollArea>
#include <QStackedWidget>

#include "ui/model_page.h"
#include "ui/pages.h"

namespace {

/// 面板逻辑尺寸（板端 DSI 转屏后就是 1280×800，见 docs/gui.md §2）
constexpr int kPanelWidth = 1280;
constexpr int kPanelHeight = 800;
/// 顶部通栏高度（ui/top_bar.cpp 里 setFixedHeight(72)；板端 --dump-layout 实测 72）
constexpr int kTopBarHeight = 72;
constexpr int kContentHeight = kPanelHeight - kTopBarHeight;   // 728

} // namespace

class TestPageHeights : public QObject {
    Q_OBJECT

private slots:
    /// 反空转：先把"面板尺寸"这套常数本身钉住，免得改了常数让下面变成废话
    void panelNumbersMatchTheBoard()
    {
        QCOMPARE(kPanelHeight - kTopBarHeight, 728);
        QCOMPARE(kPanelWidth, 1280);
    }

    /// 每一页都要装得进内容区（就是 883 那个 bug 的正面断言）
    void everyPageFitsTheScreen_data()
    {
        QTest::addColumn<QString>("key");
        for (const PageEntry& entry : pageEntries()) {
            QTest::newRow(qPrintable(entry.key)) << entry.key;
        }
    }

    void everyPageFitsTheScreen()
    {
        QFETCH(QString, key);
        QWidget* page = createPage(key, nullptr);
        QVERIFY2(page != nullptr, qPrintable(QStringLiteral("createPage(%1) 返回空").arg(key)));
        page->setParent(nullptr);                  // 独立顶层，量它自己的最小尺寸

        const QSize minSize = page->minimumSizeHint();
        QVERIFY2(minSize.height() <= kContentHeight,
                 qPrintable(QStringLiteral("页面 %1 的最小高度 %2 > 内容区 %3"
                                           "（会把窗口撑到屏幕外；内容高就放进 QScrollArea）")
                                .arg(key).arg(minSize.height()).arg(kContentHeight)));
        QVERIFY2(minSize.width() <= kPanelWidth,
                 qPrintable(QStringLiteral("页面 %1 的最小宽度 %2 > 面板宽 %3")
                                .arg(key).arg(minSize.width()).arg(kPanelWidth)));
        delete page;
    }

    /// 模型页是"内容比页面本身高"的那个页 —— 它必须靠滚动区把内容兜住，
    /// 而不是让页面（进而整个窗口）长到内容那么高。
    ///
    /// 注意别把这里的判据写成"内容 > 728"：`--dump-layout` 量到模型页 min=811 是在
    /// **最小宽度**下量的（QLabel 开了 wordWrap，宽度越小折行越多、最小高度越大），
    /// 按真实宽度铺开之后内容只要 510。真正要钉的是"页面 min 远小于内容 min"这个关系 ——
    /// 没有滚动区时这两个数就是同一个（那才是把窗口撑到 883 的原因）。
    void modelPageScrollsInsteadOfGrowing()
    {
        ModelPage page;
        QScrollArea* scroll = page.scrollArea();
        QVERIFY2(scroll != nullptr, "模型页没有滚动区：那它就会把窗口撑过屏幕（T14-7b）");
        QCOMPARE(scroll->widgetResizable(), true);
        QCOMPARE(scroll->horizontalScrollBarPolicy(), Qt::ScrollBarAlwaysOff);  // 不许左右滑
        QWidget* content = scroll->widget();
        QVERIFY2(content != nullptr, "滚动区里没有内容控件");

        const int pageMin = page.minimumSizeHint().height();
        const int contentMin = content->minimumSizeHint().height();
        QVERIFY2(contentMin > pageMin * 2,
                 qPrintable(QStringLiteral("内容 min=%1 与页面 min=%2 差不多 —— 滚动区没在起作用"
                                           "（页面就该是内容那么高，窗口会被撑过屏幕）")
                                .arg(contentMin).arg(pageMin)));
    }
};

QTEST_MAIN(TestPageHeights)
#include "test_page_heights.moc"
