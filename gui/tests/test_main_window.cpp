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
#include "core/config_store.h"   // 判据要构造一份配置交给 applyConfig() ✓
#include <QSpinBox>     // 判据 1/3 设 regionIdleSpin() ✓
#include "core/idle_watcher.h"   // 判据 1/3 读 idleWatcher()->idleMs() ✓
#include <QComboBox>    // 四区域模式框与输入源 ✓
#include <QDir>
#include <QFile>
#include <QTemporaryDir>   // ★ 判据 3 要一份真的 configPath ✓
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

    /// T15-17 / T5-1 ✓：**改设置页 ⇒ 重放真的跑了** ✓
    /// 做法：改四区域模式 ⇒ 设置页的回显 `WakeEcho` 文本**跟着变** ✓
    /// （回显是在 T3 的 `notifyEdited` 里刷新的 ✓ ⇒ 它变 = 信号发了 ✓ = MainWindow 接了 ✓）。
    void liveReplayHappensWhenTheSettingsPageChanges()
    {
        MainWindow window;
        // ⚠⚠ **必须像生产那样先接线** ✗→✓：T3 的 settingsPage_→MainWindow 接线住在 applyConfig() 里 ✓
        //   （生产是 main.cpp 调的 ✓）⇒ 不调它，信号就**没人接** ✓ ⇒ 重放不跑 ✓
        //   ⇒ 判据 1/3 当场红 ✓（**这条判据真的在测东西** ✓ —— 它自己把接线缺口照出来了 ✓）。
        core::ConfigStore store;
        window.applyConfig(store);
        SettingsPage* page = window.settingsPage();
        QVERIFY(page != nullptr);
        QLabel* echo = page->findChild<QLabel*>(QStringLiteral("WakeEcho"));
        QVERIFY2(echo != nullptr, "找不到回显 WakeEcho ✗");
        const QString before = echo->text();

        // ★ 盯**重放的直接副作用** ✓：四区域休眠时间 ⇒ `IdleWatcher::idleMs()` ✓
        //   （`idleWatcher()` 与 `idleMs()` 都是**公开**的 ✓ ⇒ 不用给测试开洞 ✓）。
        //   ⚠ 我第一版只盯"回显变没变" ✗ —— 而回显是在**同一个 lambda 里**刷新的 ✓
        //   ⇒ 注掉 `emit` 它照样变 ⇒ 判据测错了对象 ✗（牙齿当场没咬 ✓，是牙齿救了我 ✓）。
        QVERIFY(window.idleWatcher() != nullptr);
        const int beforeIdle = window.idleWatcher()->idleMs();
        const int newIdle = beforeIdle == 4200 ? 5100 : 4200;   // 一定与当前值不同 ✓
        QVERIFY(page->regionIdleSpin() != nullptr);
        page->regionIdleSpin()->setValue(newIdle);

        QCOMPARE(window.idleWatcher()->idleMs(), newIdle);       // ★ 重放真的跑了 ✓
        QVERIFY2(echo->text() != before, "回显没跟着变 ✗");
    }

    /// T15-17 / T5-2 ✓：回显里**确实**是"当前生效值"的样子 ✓（四个区域 + 共用毫秒 ✓）
    void wakeEchoShowsTheCurrentlyAppliedValues()
    {
        MainWindow window;
        SettingsPage* page = window.settingsPage();
        QVERIFY(page != nullptr);
        QLabel* echo = page->findChild<QLabel*>(QStringLiteral("WakeEcho"));
        QVERIFY(echo != nullptr);
        // ⚠ 必须是**真的改变** ✓：先前我写 `setCurrentIndex(0)` ✗ —— 若它本来就是 0 ⇒
        //   信号**不触发** ✗ ⇒ 回显停在"(待刷新)" ✓ ⇒ 判据 2 当场红 ✓（**这正是判据在防的假绿** ✓）。
        QComboBox* left = page->regionMode(QStringLiteral("left"));
        QVERIFY(left != nullptr);
        left->setCurrentIndex(left->currentIndex() == 0 ? 1 : 0);
        const QString text = echo->text();
        for (const QString& needle : {QStringLiteral("上="), QStringLiteral("下="),
                                      QStringLiteral("左="), QStringLiteral("右="),
                                      QStringLiteral("ms")}) {
            QVERIFY2(text.contains(needle), qPrintable(QStringLiteral("回显缺 %1 ✗：%2").arg(needle, text)));
        }
    }

    /// T15-17 / T5-3 ★：**重放不许冲掉正在编辑的内容** ✓✓ —— 这条钉的是 T1 的靶心 ✓
    /// 做法：先改一个**与四区域无关**的编辑项（输入源 ✓），再改四区域触发重放 ✓，
    /// 断言那个编辑项**没被改回去** ✓。⚠ 反过来（把 `loadFromConfig` 塞进重放 ✗）它会**红** ✓。
    void theLiveReplayDoesNotClobberWhatYouAreEditing()
    {
        // ⚠⚠ 这条判据**第一版是空过的** ✗：没有 configPath ⇒ MainWindow 里那段重放
        //    `if (ok && !configPath_.isEmpty())` 根本不执行 ✗ ⇒ 等于什么都没测 ✓。
        //    ⇒ 这里造一份**临时最小配置**并 `setConfigPath()` ✓（它是公开的 ✓）
        //    ⇒ 重放路径必然执行 ✓，这条判据才**真的**在看着"重放会不会冲掉编辑内容" ✓。
        QTemporaryDir dir;
        QVERIFY(dir.isValid());
        const QString cfgPath = dir.filePath(QStringLiteral("config.yaml"));
        // ⚠ 第一版我手写了一份极简 yaml ✗ ⇒ `ConfigStore::load()` 很可能返回 false ✓
        //   ⇒ 重放整段又被跳过 ⇒ 判据**空过** ✗✗ ⇒ 这里改成**拷贝仓库那份真模板** ✓（保证能 load ✓）。
#ifdef SETTINGS_TEMPLATE_PATH
        QVERIFY2(QFile::copy(QStringLiteral(SETTINGS_TEMPLATE_PATH), cfgPath),
                 "拷不到 config.example.yaml ✗（路径宏没定义对？）");
#else
        QSKIP("没有 SETTINGS_TEMPLATE_PATH 宏 ✗ —— 本判据不敢假绿 ✓");
#endif
        MainWindow window;
        // ⚠ 接线同样要照生产来 ✓（见判据 1 的注释 ✓）
        core::ConfigStore base;
        window.applyConfig(base);
        window.setConfigPath(cfgPath);          // ★ 关键：让重放真的会跑 ✓
        SettingsPage* page = window.settingsPage();
        QVERIFY(page != nullptr);
        QVERIFY(page->inputSourceBox() != nullptr);
        QVERIFY(page->regionMode(QStringLiteral("left")) != nullptr);

        page->inputSourceBox()->setCurrentIndex(1);                     // 比如切到「命令行」✓
        const QString editing = page->inputSourceBox()->currentText();
        const int editingIndex = page->inputSourceBox()->currentIndex();

        QVERIFY(window.idleWatcher() != nullptr);
        const int beforeIdle = window.idleWatcher()->idleMs();
        page->regionIdleSpin()->setValue(beforeIdle == 4200 ? 5100 : 4200);   // ★ 触发重放 ✓

        // ★★ **先证明重放真的跑了** ✓ —— 上一版就是缺这一条才空过 ✗（`configPath_` 空 ⇒ 整段跳过 ✓）。
        QVERIFY2(window.idleWatcher()->idleMs() != beforeIdle,
                 "重放根本没跑 ✗（configPath 没生效？）—— 这条判据不许假绿 ✓");

        QCOMPARE(page->inputSourceBox()->currentIndex(), editingIndex);
        QCOMPARE(page->inputSourceBox()->currentText(), editing);
    }
};


//  ---------------------------------------------------------------------------
//  ⚠ 矩阵第 2/3 行为什么**还没有判据**（2026-10-05 实测 ✓，不是没做 ✗）
//  ---------------------------------------------------------------------------
//    · **第 2 行（命令没发出 ⇒ 提示行）**：本文件试过 ✓ —— 裸造窗口 + 点设置页「保存」按钮 ✓
//      （**公开**入口 ✓，合规 ✓），但**立即扫描找不到**「命令没发出去」✗；**又试了轮询 8 秒**（`QTRY_VERIFY_WITH_TIMEOUT(saysIt(), 8000)` ✓）**仍然等不到** ✗；**第三条**：改用 WiFi「扫描」按钮 ✓（它会真的抛 `wifiRequested` ✓，见 `test_settings_page::wifiScanButtonAsksForAScan` ✓）+ 轮询 8 秒 ✓ —— **三条都等不到** ✗ ⇒ 这几个公开触发点在**无 Agent 时不会产生那句话** ✓ ⇒ 要补得**另找触发点** ✓（比如直灌一条注定失败的命令 ✓）**或改 API** ✓
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
