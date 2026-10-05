// ============================================================================
//  gui/tests/test_main_window.cpp — MainWindow 级测试（T15-16 最后一条遗留）
//
//  为什么以前一直没做（G-B-4 / G-B-6 那两轮的判断 ✗）
//  ---------------------------------------------------------------------------
//   我当时只凭"整套 33 项里没有 MainWindow 级测试"就认定它是兔子洞 ✗ —— **没读构造函数** ✗。
//   第 101 轮读了 `main_window.h` ✓：`explicit MainWindow(QWidget* parent = nullptr)` ✓ 是普通构造，
//   而"**开始连接 Agent**"是 `:68` 那行**单独的、非阻塞的**调用 ✓ ⇒ **不在构造函数里** ✓✓
//   ⇒ 测试可以**裸造窗口、不连 Agent、不读配置** ✓。
//   ⚠ 那条初判**偏保守**了 ✓ —— 这一条我认 ✓。
//
//  这个文件现在只做一件事：**证明构造可用** ✓（= 那条遗留的地基 ✓）。
//  矩阵第 2/3 行（命令没发出／壁纸读不到）的判据，等这座地基稳了再往上放 ✓。
//
//  ⚠ 链接注意 ✓：`MainWindow` 在 **可执行目标** 里（`gui/CMakeLists.txt` 的 `add_executable(agent_gui …)` ✓），
//     **不在 `gui_widgets` 库里** ✗ ⇒ 本测试的 CMake 必须**额外编进 `../src/main_window.cpp`** ✓
//     （见 `tests/CMakeLists.txt` 里单独的那一段 ✓）。
// ============================================================================
#include <QtTest/QtTest>

#include <QLabel>       // 判据里按 QLabel* 扫文案 ✓（不完整类型会编译错 ✗）
#include <QPushButton>  // 第 2 行判据要按「保存」按钮 ✓

#include "main_window.h"
#include "ui/settings_page.h"

class TestMainWindow : public QObject {
    Q_OBJECT

private slots:
    /// 裸构造可用 ✓（offscreen ✓、不连 Agent ✓、不读真实配置 ✓）
    void constructsWithoutAnAgent()
    {
        MainWindow window;

        // 五块"页面/区域"都得真的建起来 ✓ —— 全走头文件里的公开访问器 ✓（不碰私有成员 ✓）
        QVERIFY2(window.sysPage() != nullptr, "系统页没建起来 ✗");
        QVERIFY2(window.settingsPage() != nullptr, "设置页没建起来 ✗");
        QVERIFY2(window.modelPage() != nullptr, "模型页没建起来 ✗");
        QVERIFY2(window.chatPanel() != nullptr, "对话区没建起来 ✗");
        QVERIFY2(window.schedulePanel() != nullptr, "日程区没建起来 ✗");
        QVERIFY2(window.bottomBar() != nullptr, "底部栏没建起来 ✗");
    }

    /// 连 Agent 是**显式**的 ✓：没调那一步时，客户端对象在、但**不该**处于已连状态 ✓
    /// ⚠ 只断言"对象存在" ✓ —— 不断言"未连接"✗（那要看 LocalClient 的内部状态 ✓，本文件不碰 ✓）。
    void hasAClientButDoesNotConnectByItself()
    {
        MainWindow window;
        QVERIFY2(window.client() != nullptr, "LocalClient 没建起来 ✗");
        QVERIFY2(window.idleWatcher() != nullptr, "IdleWatcher 没建起来 ✗");
    }
};


//  ---------------------------------------------------------------------------
//  ⚠ 矩阵第 2/3 行为什么**还没有判据**（2026-10-05 实测 ✓，不是没做 ✗）
//  ---------------------------------------------------------------------------
//    · **第 2 行（命令没发出 ⇒ 提示行）**：本文件试过 ✓ —— 裸造窗口 + 点设置页「保存」按钮 ✓
//      （**公开**入口 ✓，合规 ✓），但**立即扫描找不到**「命令没发出去」✗
//      （文案在 `main_window.cpp:778/801/818` ✓，三条共用该前缀 ✓）。
//      ⚠ 我**没有**继续赌"给事件循环一次机会就好了"✗ —— 留一个**红用例**比少一格证据糟得多 ✗✗
//      ⇒ **撤掉了它** ✓。要补的话 ✓：先确认那条失败路径**在无 Agent 时到底走不走** ✓
//      （可能需要真实 socket 写失败 ✓，也可能是异步回来 ✓ ⇒ 用 `QTRY_VERIFY` 而不是 `qWait` 试 ✓）。
//    · **第 3 行（壁纸读不到 ⇒ 提示行）**：触发器**是私有的** ✗ ✓ ——
//      `main_window.h` 的 `public:` 在 :52 ✓、`private:` 在 :141 ✓，而 `onMessage()` 在 :146 ✓、
//      `setWallpaperFromPath()` 在 :162 ✓ ⇒ **测试点不动** ✓。要补的话 ✓：让入口可注入 ✓
//      （比如公开一个"喂一条 wallpaper 消息"的验收辅助 ✓，与仓库里十几个 `--*-demo` 同一路数 ✓），
//      **但那是改产品 API** ✗ ⇒ 本轮**没做** ✓，如实留档 ✓。
//  ---------------------------------------------------------------------------

QTEST_MAIN(TestMainWindow)
#include "test_main_window.moc"
