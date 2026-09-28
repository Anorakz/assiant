// ============================================================================
//  gui/tests/test_keyboard_inset.cpp — 虚拟键盘不能压住对话输入行（T15-1 1b）
//
//  板端实测（EGLFS，无 X，产品形态）
//  ---------------------------------------------------------------------------
//      Qt 虚拟键盘 = 独立窗口（DesktopInputPanel），它**不改我们的窗口大小**：
//          IM_RECT      = 0,400 1280x400      （键盘占屏下半截）
//          INPUT_RECT   = 961,496 82x48       （对话输入行在 y=496..544）
//      -> 键盘**整个压住**输入行：打字时看不见自己打的字。
//
//  这个测试钉三件事（都是踩过的坑）
//  ---------------------------------------------------------------------------
//  1) 让位之后，对话卡的下沿必须在"键盘上沿"以内（输入行在卡片底部）；
//  2) 让位**不许**把页面的最小高度撑大 —— 第一版用"给布局加下边距"，结果窗口
//     从 800 涨到 1063（布局最小高度 = 内容最小高度 + 下边距），输入行反而更往里掉。
//     板端 EGLFS 会把窗口撑到最小尺寸，于是底部直接跑到屏幕外；
//  3) 收起键盘（inset=0）必须完全复原：高度上限/可见性/弹簧都回到原样。
//
//  跑法（板端或任何有 Qt5 的地方）
//  ---------------------------------------------------------------------------
//      cd gui/build && QT_QPA_PLATFORM=offscreen ctest -R test_keyboard_inset --output-on-failure
// ============================================================================

#include <QtTest>
#include <QLineEdit>
#include <QSpacerItem>
#include <QVBoxLayout>

#include "ui/chat_panel.h"
#include "ui/mode_panel.h"
#include "ui/pages.h"

namespace {

/// 面板逻辑尺寸（1280×800 转屏之后）+ 顶部通栏 72（与 test_page_heights 同一套常数）
constexpr int kPanelHeight = 800;
constexpr int kTopBarHeight = 72;
constexpr int kContentHeight = kPanelHeight - kTopBarHeight;   // 728
/// 板端实测的键盘高度（QVK 默认样式：屏宽 × 800/2560 = 400）
constexpr int kKeyboardHeight = 400;
/// 键盘上沿在内容区里的 y
constexpr int kKeyboardTop = kContentHeight - kKeyboardHeight;  // 328

/// 让布局真的跑一遍（offscreen 下没有窗口管理器，得自己 processEvents）
void settle(QWidget& page)
{
    page.resize(1280, kContentHeight);
    page.show();
    QCoreApplication::processEvents();
    page.layout()->activate();
    QCoreApplication::processEvents();
}

} // namespace

class TestKeyboardInset : public QObject {
    Q_OBJECT

private slots:
    /// 反空转：把上面那套常数跟板端实测的数字对上，免得改常数把下面变成废话
    void numbersMatchTheBoard()
    {
        QCOMPARE(kContentHeight, 728);
        QCOMPARE(kKeyboardTop, 328);
        QCOMPARE(kKeyboardHeight, 400);
    }

    /// 核心不变式：让位后对话卡下沿 ≤ 键盘上沿（输入行就露在键盘上方）
    void chatCardEndsAboveTheKeyboard()
    {
        MainPage page;
        settle(page);

        const int minBefore = page.minimumSizeHint().height();
        page.setKeyboardInset(kKeyboardHeight);
        settle(page);

        QWidget* chat = page.chatFrame();
        QVERIFY2(chat != nullptr, "MainPage 没有对话卡");
        const int bottom = chat->mapTo(&page, QPoint(0, chat->height())).y();
        QVERIFY2(bottom <= kKeyboardTop,
                 qPrintable(QStringLiteral("对话卡下沿 y=%1 > 键盘上沿 %2：输入行会被键盘压住")
                                .arg(bottom).arg(kKeyboardTop)));

        // 输入行就在卡片底部：卡片下沿在键盘上方 => 输入行也在键盘上方
        QWidget* input = page.chatPanel() != nullptr ? page.chatPanel()->input() : nullptr;
        QVERIFY2(input != nullptr, "对话区没有输入框");
        const int inputBottom = input->mapTo(&page, QPoint(0, input->height())).y();
        QVERIFY2(inputBottom <= kKeyboardTop,
                 qPrintable(QStringLiteral("输入框下沿 y=%1 > 键盘上沿 %2").arg(inputBottom)
                                .arg(kKeyboardTop)));

        // ⚠ 第 2 条不变式：让位**不能**把最小高度撑大（撑大 -> EGLFS 把窗口撑出屏幕）。
        //   收紧（模式卡/日程卡让位）是好事，所以判据是"不涨"，不是"相等"。
        QVERIFY2(page.minimumSizeHint().height() <= minBefore,
                 qPrintable(QStringLiteral("让位把页面最小高度从 %1 撑大到了 %2（窗口会被撑出屏幕）")
                                .arg(minBefore).arg(page.minimumSizeHint().height())));
    }

    /// 打字时右区域只留对话卡：模式卡/日程卡收起来（它们本来也在键盘底下/放不下）
    void otherCardsGiveWayWhileTyping()
    {
        MainPage page;
        settle(page);
        QWidget* chat = page.chatFrame();
        QWidget* mode = page.modePanel() != nullptr ? page.modePanel()->parentWidget() : nullptr;
        QVERIFY2(mode != nullptr && chat != nullptr, "找不到模式卡/对话卡");

        page.setKeyboardInset(kKeyboardHeight);
        settle(page);
        QVERIFY2(mode->isHidden(), "打字时模式卡没有让位（还占着列里的高度）");
        QVERIFY2(chat->maximumHeight() < QWIDGETSIZE_MAX, "对话卡没有被压上限");
    }

    /// 收起键盘：高度上限、可见性、弹簧都要回到原样
    void hideRestoresEverything()
    {
        MainPage page;
        settle(page);
        QWidget* chat = page.chatFrame();
        QWidget* mode = page.modePanel() != nullptr ? page.modePanel()->parentWidget() : nullptr;
        QWidget* schedule = page.scheduleFrame();
        QVERIFY2(chat != nullptr && mode != nullptr && schedule != nullptr, "找不到右区域三张卡");
        const int chatHeightBefore = chat->height();

        page.setKeyboardInset(kKeyboardHeight);
        settle(page);
        page.setKeyboardInset(0);
        settle(page);

        QCOMPARE(chat->maximumHeight(), QWIDGETSIZE_MAX);
        QVERIFY2(!mode->isHidden(), "收键盘后模式卡没回来");
        QVERIFY2(!schedule->isHidden(), "收键盘后日程卡没回来");
        QCOMPARE(chat->height(), chatHeightBefore);
    }

    /// 值没变就别折腾布局（键盘矩形会连着报好几次同样的值）
    void repeatedSameInsetIsIgnored()
    {
        MainPage page;
        settle(page);
        QWidget* chat = page.chatFrame();
        QVERIFY(chat != nullptr);
        page.setKeyboardInset(kKeyboardHeight);
        settle(page);
        const int first = chat->maximumHeight();
        chat->setMaximumHeight(first - 7);              // 手动破坏，看第二次会不会被"重置"
        page.setKeyboardInset(kKeyboardHeight);         // 同一个值 -> 应当直接返回
        QCOMPARE(chat->maximumHeight(), first - 7);
    }
};

QTEST_MAIN(TestKeyboardInset)
#include "test_keyboard_inset.moc"
