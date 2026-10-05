// ============================================================================
//  gui/tests/test_top_bar.cpp — 顶栏的链路三态（T15-16 遗留 ③ 的第 1 行）
//
//  为什么补这条（`docs/gui-style.md` §3.1 矩阵第 1 行原本**没有单测** ✗）
//  ---------------------------------------------------------------------------
//   矩阵第 1 行写的是"没连上 Agent ⇒ 顶栏三态 + 琥珀横幅" ✓ ——
//   而我 2026-10-05 搜遍 `gui/tests` 发现**根本没有顶栏用例** ✗（只有两处 `kTopBarHeight` 常数 ✓），
//   于是当场把那一格从"单测 ✓"改成"⚠ 本行无单测" ✗。这条就是来把它补上的 ✓。
//
//  为什么测 `TopBar` 而不是 `MainWindow` ✓
//  ---------------------------------------------------------------------------
//   `main_window.cpp` 里那处（`:573` 附近的对话区提示）需要 MainWindow 级测试 ✗ ——
//   而**整套 31 项里没有 MainWindow 级测试** ✓（G-B-4/B-6 都撞过这条限制 ✓）。
//   但顶栏是**纯 widget** ✓ ⇒ 可以像 `test_bottom_bar` 那样直接测 ✓✓，不必趟那个兔子洞 ✗。
//
//  ⚠ 控件怎么找：`linkDot_` 与 `linkText_` 的 objectName **都是** `TopBarText` ✗
//    ⇒ 按**创建顺序**取（圆点在前 ✓、文字在后 ✓），并且**先断言恰好两个** ✓ ——
//    否则将来布局一变，测试会**悄悄测错控件** ✗（那比红更糟 ✓）。
// ============================================================================
#include <QLabel>
#include <QtTest/QtTest>

#include "ui/theme.h"     // 断言圆点颜色来自主题常量 ✓（不写死十六进制 ✗）
#include "ui/top_bar.h"

class TestTopBar : public QObject {
    Q_OBJECT

private slots:
    /// 矩阵第 1 行：链路三态**在界面上真的看得出** ✓（文字 + 圆点颜色 ✓）
    void linkStatesAreVisibleOnTheTopBar()
    {
        TopBar bar;
        const auto labels = bar.findChildren<QLabel*>(QStringLiteral("TopBarText"));
        QCOMPARE(labels.size(), 2);                 // 恰好两个：圆点 + 文字 ✓
        QLabel* dot = labels.at(0);
        QLabel* text = labels.at(1);

        bar.setLinkState(TopBar::LinkState::Connected);
        QCOMPARE(text->text(), QStringLiteral("已连接"));
        QVERIFY2(dot->styleSheet().contains(QLatin1String(theme::kOk)),
                 qPrintable(QStringLiteral("已连接的圆点该是绿的 ✓，实际：%1").arg(dot->styleSheet())));

        bar.setLinkState(TopBar::LinkState::Disconnected);
        QVERIFY2(!text->text().isEmpty(), "未连接也要有话说 ✓（不能空着 ✗）");
        QVERIFY2(!dot->styleSheet().contains(QLatin1String(theme::kOk)),
                 "未连接却还是绿的 ✗ —— 那用户就看不出断线了");

        // ⚠ 关键一条：三态必须**看得出不一样** ✗
        //   （防"三种状态其实同一个样子"✗ —— 那三态就是摆设 ✓）
        const QString offText = text->text();
        const QString offStyle = dot->styleSheet();
        bar.setLinkState(TopBar::LinkState::Reconnecting);
        QVERIFY2(text->text() != offText || dot->styleSheet() != offStyle,
                 qPrintable(QStringLiteral("「重连中」与「未连接」看不出区别 ✗（文字 %1 / 样式 %2）")
                                .arg(text->text(), dot->styleSheet())));
    }
};

QTEST_MAIN(TestTopBar)
#include "test_top_bar.moc"
