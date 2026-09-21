// ============================================================================
//  gui/tests/test_main_page.cpp — 主页面右区域控件级测试（T5）
//
//  这是**真的点控件**：找到真实的输入框/按钮，setText + click，断言信号与副作用。
//  需要 QApplication（Widgets），所以 ctest 里用 QT_QPA_PLATFORM=offscreen 跑。
// ============================================================================
#include <QBoxLayout>
#include <QLineEdit>
#include <QMenu>
#include <QPushButton>
#include <QToolButton>
#include <QtTest/QtTest>

#include "ui/chat_panel.h"
#include "ui/mode_panel.h"
#include "ui/pages.h"
#include "ui/region_host.h"     // rightRegion() 返回它（pages.h 里只有前置声明）
#include "ui/schedule_panel.h"

namespace {

/// 收集**当前提供**的模式按钮。
/// 用 !isHidden() 而不是 isVisible()：控件所在窗口没显示时 isVisible() 也是 false。
/// 按钮池会隐藏多余按钮，所以这里数的正是"用户看得见的几个"。
QVector<QPushButton*> modeButtons(ModePanel* panel)
{
    QVector<QPushButton*> out;
    const QList<QPushButton*> all = panel->findChildren<QPushButton*>();
    for (QPushButton* button : all) {
        if (button->objectName() == QLatin1String("ModeButton") && !button->isHidden()) {
            out.append(button);
        }
    }
    return out;
}

QPushButton* modeButtonFor(ModePanel* panel, const QString& target)
{
    for (QPushButton* button : modeButtons(panel)) {
        if (button->property("modeTarget").toString() == target) {
            return button;
        }
    }
    return nullptr;
}

} // namespace

class TestMainPage : public QObject {
    Q_OBJECT

private slots:
    void modePanelOffersThreeWhenIdleOrUnknown();
    void modePanelOffersOnlyExitWhenBusy();
    void chatSendEmitsTextAndClearsInput();
    void chatSendBlockedWhenLinkDown();
    void chatCapsMessageCount();
    void inputTypeButtonSwitchesThroughSamePath();
    void rightRegionHasChatAndScheduleAtThreeToTwo();
};

void TestMainPage::modePanelOffersThreeWhenIdleOrUnknown()
{
    ModePanel panel;

    // 还没收到 status（空字符串）→ 也给三个入口，否则用户没法切模式
    panel.setMode(QString());
    QCOMPARE(modeButtons(&panel).size(), 3);

    panel.setMode(QStringLiteral("IDLE"));
    QCOMPARE(modeButtons(&panel).size(), 3);
    QVERIFY(modeButtonFor(&panel, QStringLiteral("SLEEP")) != nullptr);
    QVERIFY(modeButtonFor(&panel, QStringLiteral("STUDY")) != nullptr);
    QVERIFY(modeButtonFor(&panel, QStringLiteral("GAME")) != nullptr);

    QSignalSpy spy(&panel, &ModePanel::modeRequested);
    modeButtonFor(&panel, QStringLiteral("STUDY"))->click();
    QCOMPARE(spy.count(), 1);
    QCOMPARE(spy.first().at(0).toString(), QStringLiteral("STUDY"));
}

void TestMainPage::modePanelOffersOnlyExitWhenBusy()
{
    ModePanel panel;
    panel.setMode(QStringLiteral("GAME"));
    QCOMPARE(modeButtons(&panel).size(), 1);

    QPushButton* exit = modeButtonFor(&panel, QStringLiteral("IDLE"));
    QVERIFY(exit != nullptr);
    QCOMPARE(exit->text(), QStringLiteral("退出当前模式"));

    QSignalSpy spy(&panel, &ModePanel::modeRequested);
    exit->click();
    QCOMPARE(spy.count(), 1);
    QCOMPARE(spy.first().at(0).toString(), QStringLiteral("IDLE"));

    // 切回 IDLE 后按钮组应该恢复成三个
    panel.setMode(QStringLiteral("IDLE"));
    QCOMPARE(modeButtons(&panel).size(), 3);
}

void TestMainPage::chatSendEmitsTextAndClearsInput()
{
    ChatPanel panel;
    panel.setLinkUp(true);
    QVERIFY(panel.input() != nullptr);
    QVERIFY(panel.sendButton() != nullptr);
    QVERIFY(panel.sendButton()->isEnabled());

    QSignalSpy spy(&panel, &ChatPanel::sendRequested);
    panel.input()->setText(QStringLiteral("  你好  "));
    panel.sendButton()->click();

    QCOMPARE(spy.count(), 1);
    QCOMPARE(spy.first().at(0).toString(), QStringLiteral("你好"));   // 去掉了首尾空白
    QVERIFY(panel.input()->text().isEmpty());                          // 发完清空
}

void TestMainPage::chatSendBlockedWhenLinkDown()
{
    ChatPanel panel;
    panel.setLinkUp(false);
    QVERIFY(!panel.sendButton()->isEnabled());   // 断连时按钮置灰

    QSignalSpy spy(&panel, &ChatPanel::sendRequested);
    panel.input()->setText(QStringLiteral("发不出去的消息"));
    panel.sendButton()->click();                 // 禁用按钮不会触发 clicked
    QCOMPARE(spy.count(), 0);
    QCOMPARE(panel.input()->text(), QStringLiteral("发不出去的消息"));  // 文本保留，别让用户白打

    // 空文本同样不发
    panel.setLinkUp(true);
    panel.input()->setText(QStringLiteral("   "));
    panel.sendButton()->click();
    QCOMPARE(spy.count(), 0);
}

void TestMainPage::chatCapsMessageCount()
{
    ChatPanel panel;
    panel.setLinkUp(true);
    for (int i = 0; i < ChatPanel::kMaxMessages + 5; ++i) {
        panel.appendAssistant(QStringLiteral("第 %1 条").arg(i));
    }
    QCOMPARE(panel.messageCount(), ChatPanel::kMaxMessages);
    QVERIFY(panel.messageCount() <= 200);
}

void TestMainPage::inputTypeButtonSwitchesThroughSamePath()
{
    ChatPanel panel;
    QVERIFY(panel.inputTypeButton() != nullptr);
    // 菜单里应该有**两个**输入源（keyboard / terminal），默认选中 config.yaml 的默认值（keyboard）
    // 原第三个 "PC"（宿主机键盘）已随主机输入方向一起移除 (Phase 6 C4)
    QMenu* menu = panel.inputTypeButton()->menu();
    QVERIFY(menu != nullptr);
    QCOMPARE(menu->actions().size(), 2);
    QCOMPARE(panel.inputType(), QStringLiteral("keyboard"));
    QCOMPARE(panel.inputTypeButton()->text(), QStringLiteral("键盘 ▾"));

    // "pc" 不该再出现在菜单里
    for (QAction* action : menu->actions()) {
        QVERIFY(action->data().toString() != QLatin1String("pc"));
    }

    QSignalSpy spy(&panel, &ChatPanel::inputTypeChanged);

    // 走"菜单项被触发"这条真实路径（不是直接 setInputType）
    QAction* terminalAction = nullptr;
    for (QAction* action : menu->actions()) {
        if (action->data().toString() == QLatin1String("terminal")) {
            terminalAction = action;
        }
    }
    QVERIFY(terminalAction != nullptr);
    terminalAction->trigger();

    QCOMPARE(spy.count(), 1);
    QCOMPARE(spy.first().at(0).toString(), QStringLiteral("terminal"));
    QCOMPARE(panel.inputType(), QStringLiteral("terminal"));
    QCOMPARE(panel.inputTypeButton()->text(), QStringLiteral("命令行 ▾"));
    QVERIFY(terminalAction->isChecked());
    // 同一时刻只有一个被选中
    int checked = 0;
    for (QAction* action : menu->actions()) {
        checked += action->isChecked() ? 1 : 0;
    }
    QCOMPARE(checked, 1);

    // 再切回键盘
    for (QAction* action : menu->actions()) {
        if (action->data().toString() == QLatin1String("keyboard")) {
            action->trigger();
        }
    }
    QCOMPARE(panel.inputType(), QStringLiteral("keyboard"));
    // 到这里一共两次切换: keyboard -> terminal, terminal -> keyboard
    QCOMPARE(spy.count(), 2);

    // 旧配置里的 "pc" 必须**回落到 keyboard**, 而不是显示幽灵标签 (Phase 6 C4)。
    // 这一条放在最后: 它自己会再发一次 inputTypeChanged (回落值), 免得影响上面的计数。
    panel.setInputType(QStringLiteral("pc"));
    QCOMPARE(panel.inputType(), QStringLiteral("keyboard"));
    QCOMPARE(panel.inputTypeButton()->text(), QStringLiteral("键盘 ▾"));
    QCOMPARE(spy.count(), 3);
    QCOMPARE(spy.last().at(0).toString(), QStringLiteral("keyboard"));
}

/// S5：右区域的三块结构与比例。这里断言的是**几何与布局**，不是像素：
///   · 对话区 / 日程区都在 rightRegion_ 里，日程区在对话区下面
///   · 两者的伸缩因子是 3 : 2
void TestMainPage::rightRegionHasChatAndScheduleAtThreeToTwo()
{
    MainPage page;
    page.resize(800, 1200);
    page.show();

    QWidget* chat = page.chatFrame();
    QWidget* schedule = page.scheduleFrame();
    QVERIFY(chat != nullptr);
    QVERIFY(schedule != nullptr);
    QVERIFY(page.schedulePanel() != nullptr);

    // 同一列（同一个父控件），且都属于右区域
    QCOMPARE(schedule->parentWidget(), chat->parentWidget());
    QVERIFY(page.rightRegion()->isAncestorOf(chat));
    QVERIFY(page.rightRegion()->isAncestorOf(schedule));
    QVERIFY(page.rightRegion()->isAncestorOf(page.schedulePanel()));

    // 日程区在对话区下面（同一个竖直布局，y 更大）
    QVERIFY2(schedule->y() > chat->y(),
             qPrintable(QStringLiteral("chat.y=%1 schedule.y=%2")
                            .arg(chat->y()).arg(schedule->y())));

    // 3 : 2
    auto* box = qobject_cast<QBoxLayout*>(chat->parentWidget()->layout());
    QVERIFY(box != nullptr);
    QCOMPARE(box->stretch(box->indexOf(chat)), 3);
    QCOMPARE(box->stretch(box->indexOf(schedule)), 2);

    // 高度大致按 3:2 分（布局做完之后；给一点容差，别把字体/边距算得太死）
    QVERIFY2(chat->height() > schedule->height(),
             qPrintable(QStringLiteral("chat.h=%1 schedule.h=%2")
                            .arg(chat->height()).arg(schedule->height())));

    page.hide();
}

QTEST_MAIN(TestMainPage)
#include "test_main_page.moc"