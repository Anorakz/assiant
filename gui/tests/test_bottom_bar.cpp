// ============================================================================
//  gui/tests/test_bottom_bar.cpp — 下区域（音乐条 / 封面）控件级测试（T7）
//
//  覆盖：music 数据 → 曲目名与播放徽标；占位块点击 → 说明文案 + 信号；
//        GAME ↔ 非游戏 的页面互斥切换；歌词接口的占位实现。
//        T11-7：封面页从"未接入"占位换成真控件（BilibiliCover）后的那几条。
// ============================================================================
#include <QJsonObject>
#include <QLabel>
#include <QProgressBar>
#include <QPushButton>
#include <QtTest/QtTest>

#include "core/lyrics.h"
#include "ui/bilibili_cover.h"
#include "ui/bottom_bar.h"
#include "ui/music_bar.h"

class TestBottomBar : public QObject {
    Q_OBJECT

private slots:
    void musicUpdatesTitleAndState();
    void playButtonIsBiggerAndShowsAction();
    void controlButtonsArePlaceholders();
    void placeholderClickShowsNote();
    void gameModeSwitchesToCoverPage();
    void nullLyricsProviderIsUnavailable();
    void coverPageIsTheRealWidgetAndSaysWhereItComesFrom();
    void coverTitleIsElidedInsteadOfStretchingTheLayout();
    void coverPageFallsBackToAPlaceholderWithoutACurrentItem();
};

void TestBottomBar::musicUpdatesTitleAndState()
{
    MusicBar bar;
    // 还没收到 music：显示"未播放"，大按钮显示 —
    QCOMPARE(bar.title(), QStringLiteral("未播放"));
    QVERIFY(!bar.hasMusic());
    // T14 起播放键是**图标**（不再是 — / || / ▶ 文字符号）
    QVERIFY(!bar.playButton()->icon().isNull());
    QCOMPARE(bar.playButton()->text(), QString());

    bar.setMusic(QStringLiteral("夜曲"), true);
    QVERIFY(bar.hasMusic());
    QCOMPARE(bar.title(), QStringLiteral("夜曲"));
    QVERIFY(!bar.playButton()->icon().isNull());      // 在播 → 按钮给"暂停"动作
    QCOMPARE(bar.artistLabel()->text(), QStringLiteral("歌手 —")); // 歌手仍是占位
    QCOMPARE(bar.albumLabel()->text(), QStringLiteral("专辑 —"));
    QVERIFY(bar.progressBar() != nullptr);
    QVERIFY(!bar.progressBar()->isEnabled());                     // 进度未接入：不可交互

    bar.setMusic(QStringLiteral("夜曲"), false);
    QVERIFY(!bar.playButton()->icon().isNull());                // 已暂停 → 播放图标

    // title 缺失（空）→ 回到"未播放"，不崩
    bar.setMusic(QString(), false);
    QVERIFY(!bar.hasMusic());
    QCOMPARE(bar.title(), QStringLiteral("未播放"));
}

void TestBottomBar::playButtonIsBiggerAndShowsAction()
{
    MusicBar bar;
    QVERIFY(bar.playButton() != nullptr);
    QVERIFY(bar.previousButton() != nullptr);
    QVERIFY(bar.nextButton() != nullptr);

    // 验收要求：播放/暂停按钮略大（56×56），两侧按钮小一些
    QCOMPARE(bar.playButton()->minimumWidth(), 56);
    QCOMPARE(bar.playButton()->minimumHeight(), 56);
    QCOMPARE(bar.playButton()->maximumWidth(), 56);
    QCOMPARE(bar.previousButton()->minimumHeight(), 44);
    QVERIFY(bar.playButton()->minimumHeight() > bar.previousButton()->minimumHeight());
}

void TestBottomBar::controlButtonsArePlaceholders()
{
    MusicBar bar;
    bar.setMusic(QStringLiteral("夜曲"), true);

    QSignalSpy spy(&bar, &MusicBar::placeholderClicked);
    // 协议没有音乐控制命令：点这三颗按钮只应给出说明，不发任何协议
    bar.previousButton()->click();
    QCOMPARE(spy.count(), 1);
    QCOMPARE(spy.first().at(0).toString(), QStringLiteral("控制"));
    QVERIFY(bar.noteText().contains(QStringLiteral("上一首")));
    QVERIFY(bar.noteText().contains(QStringLiteral("未接入")));

    bar.nextButton()->click();
    QCOMPARE(spy.count(), 2);
    bar.playButton()->click();
    QCOMPARE(spy.count(), 3);
    // 点了占位按钮后，播放状态不受影响（图标仍是"在播"对应的暂停图标）
    QVERIFY(!bar.playButton()->icon().isNull());

    // 新的 music 数据到达 → 说明清掉
    bar.setMusic(QStringLiteral("晴天"), true);
    QVERIFY(bar.noteText().isEmpty());
}

void TestBottomBar::placeholderClickShowsNote()
{
    MusicBar bar;
    QVERIFY(bar.noteText().isEmpty());
    QCOMPARE(bar.lyricsLabel()->text(), QStringLiteral("歌词未接入"));

    QSignalSpy spy(&bar, &MusicBar::placeholderClicked);
    bar.triggerPlaceholder(QStringLiteral("歌词"));           // 与点击走同一入口
    QCOMPARE(spy.count(), 1);
    QCOMPARE(spy.first().at(0).toString(), QStringLiteral("歌词"));
    QVERIFY(bar.noteText().contains(QStringLiteral("LyricsProvider")));
    QVERIFY(bar.lyricsLabel()->text().contains(QStringLiteral("未接入")));

    bar.triggerPlaceholder(QStringLiteral("歌手"));
    QVERIFY(bar.noteText().contains(QStringLiteral("歌手")));
    bar.triggerPlaceholder(QStringLiteral("专辑"));
    QVERIFY(bar.noteText().contains(QStringLiteral("专辑")));
    bar.triggerPlaceholder(QStringLiteral("进度"));
    QVERIFY(bar.noteText().contains(QStringLiteral("进度")));

    // 新的 music 数据到达 → 说明清掉，回到正常显示
    bar.setMusic(QStringLiteral("晴天"), true);
    QVERIFY(bar.noteText().isEmpty());
    QCOMPARE(bar.lyricsLabel()->text(), QStringLiteral("歌词未接入"));
}

void TestBottomBar::gameModeSwitchesToCoverPage()
{
    BottomBar bar;
    QCOMPARE(bar.pageName(), QStringLiteral("music"));    // 初始/未知 → 音乐条
    bar.setMode(QStringLiteral("IDLE"));
    QCOMPARE(bar.pageName(), QStringLiteral("music"));
    bar.setMode(QStringLiteral("STUDY"));
    QCOMPARE(bar.pageName(), QStringLiteral("music"));

    bar.setMode(QStringLiteral("GAME"));
    QCOMPARE(bar.pageName(), QStringLiteral("cover"));    // 游戏 → 封面（互斥）

    bar.setMode(QStringLiteral("IDLE"));
    QCOMPARE(bar.pageName(), QStringLiteral("music"));    // 切回来
}

void TestBottomBar::nullLyricsProviderIsUnavailable()
{
    core::NullLyricsProvider provider;
    QVERIFY(!provider.available());
    QVERIFY(provider.currentLine().isEmpty());
}

void TestBottomBar::coverPageIsTheRealWidgetAndSaysWhereItComesFrom()
{
    BottomBar bar;
    QVERIFY(bar.bilibiliCover() != nullptr);
    QCOMPARE(bar.coverPage(), static_cast<QWidget*>(bar.bilibiliCover()));   // 老名字还是同一个控件
    QVERIFY(!bar.bilibiliCover()->hasVideo());                              // 还没收到队列
    // 让布局真的摆一遍：先切到封面页（QStackedLayout 只给当前页排版），
    // 标题是按**实际宽度**截断的（见下面那条"不许撑开版面"）
    bar.setMode(QStringLiteral("GAME"));
    bar.resize(900, 190);
    bar.show();
    QTest::qWait(20);

    QJsonObject current;
    current.insert(QStringLiteral("bvid"), QStringLiteral("BV1xx411c7mD"));
    current.insert(QStringLiteral("title"), QStringLiteral("Luna say maybe"));
    current.insert(QStringLiteral("author"), QStringLiteral("某 UP"));
    current.insert(QStringLiteral("duration_s"), 184);
    current.insert(QStringLiteral("play"), 1289221);

    QJsonObject data;
    data.insert(QStringLiteral("current"), current);
    data.insert(QStringLiteral("index"), 2);
    data.insert(QStringLiteral("count"), 18);
    data.insert(QStringLiteral("target"), 18);
    data.insert(QStringLiteral("keyword"), QStringLiteral("luna say maybe"));
    data.insert(QStringLiteral("source"), QStringLiteral("dialogue"));
    bar.bilibiliCover()->setData(data);
    QTest::qWait(20);

    QVERIFY(bar.bilibiliCover()->hasVideo());
    QCOMPARE(bar.bilibiliCover()->currentBvid(), QStringLiteral("BV1xx411c7mD"));
    QCOMPARE(bar.bilibiliCover()->titleLabel()->text(), QStringLiteral("Luna say maybe"));
    QCOMPARE(bar.bilibiliCover()->titleLabel()->toolTip(), QStringLiteral("Luna say maybe"));
    QVERIFY(bar.bilibiliCover()->metaLabel()->text().contains(QStringLiteral("某 UP")));
    QVERIFY(bar.bilibiliCover()->metaLabel()->text().contains(QStringLiteral("128.9万")));
    QVERIFY(bar.bilibiliCover()->positionLabel()->text().contains(QStringLiteral("第 3 / 18 条")));
    QCOMPARE(bar.bilibiliCover()->sourceLabel()->text(), QStringLiteral("你说的：luna say maybe"));

    // 画面认出来的游戏：来源那一行说清是画面，不是用户说的
    QJsonObject screen = data;
    screen.insert(QStringLiteral("keyword"), QStringLiteral("white album"));
    screen.insert(QStringLiteral("source"), QStringLiteral("screen"));
    bar.bilibiliCover()->setData(screen);
    QCOMPARE(bar.bilibiliCover()->sourceLabel()->text(), QStringLiteral("画面认出：white album"));

    // ⚠ 清晰度**不进这一层**（你定的：只走聊天气泡）
    QJsonObject withQuality = data;
    withQuality.insert(QStringLiteral("quality"), QStringLiteral("360P"));
    bar.bilibiliCover()->setData(withQuality);
    const QStringList texts = {bar.bilibiliCover()->titleLabel()->text(),
                               bar.bilibiliCover()->metaLabel()->text(),
                               bar.bilibiliCover()->positionLabel()->text(),
                               bar.bilibiliCover()->sourceLabel()->text()};
    for (const QString& text : texts) {
        QVERIFY2(!text.contains(QStringLiteral("360P")), qPrintable(text));
    }
}

void TestBottomBar::coverTitleIsElidedInsteadOfStretchingTheLayout()
{
    // 板端实测的坑：B 站标题一行能到 1280+ 像素，QLabel 会**按文字要宽度** →
    // 整个主区被推宽、地址栏与预览栏一起被挤出屏幕。现在一律按宽度截断（tooltip 给全文）。
    BottomBar bar;
    bar.setMode(QStringLiteral("GAME"));       // 封面页要是当前页，标签才拿得到宽度
    bar.resize(520, 190);
    bar.show();
    QTest::qWait(20);

    const QString longTitle = QStringLiteral("初星学園「Luna say maybe」Official Music Video ")
                              + QString(120, QLatin1Char('长'));
    QJsonObject current;
    current.insert(QStringLiteral("bvid"), QStringLiteral("BV1xx411c7mD"));
    current.insert(QStringLiteral("title"), longTitle);
    QJsonObject data;
    data.insert(QStringLiteral("current"), current);
    data.insert(QStringLiteral("index"), 0);
    data.insert(QStringLiteral("count"), 18);
    bar.bilibiliCover()->setData(data);
    QTest::qWait(20);

    const QString shown = bar.bilibiliCover()->titleLabel()->text();
    QVERIFY2(shown.size() < longTitle.size(), "长标题必须被截断，不能整条塞进去");
    QVERIFY2(shown.endsWith(QStringLiteral("\u2026")), qPrintable(shown.right(8)));
    QCOMPARE(bar.bilibiliCover()->titleLabel()->toolTip(), longTitle);   // 全文在 tooltip 里
}

void TestBottomBar::coverPageFallsBackToAPlaceholderWithoutACurrentItem()
{
    BottomBar bar;
    bar.setMode(QStringLiteral("GAME"));
    bar.resize(900, 190);
    bar.show();
    QTest::qWait(20);

    QJsonObject data;                       // 队列空 / 没接上 Agent
    bar.bilibiliCover()->setData(data);

    QVERIFY(!bar.bilibiliCover()->hasVideo());
    QVERIFY(bar.bilibiliCover()->titleLabel()->toolTip().contains(QStringLiteral("未接入")));
    QVERIFY(!bar.bilibiliCover()->titleLabel()->text().isEmpty());
    QVERIFY(bar.bilibiliCover()->metaLabel()->text().isEmpty());
    QVERIFY(bar.bilibiliCover()->positionLabel()->text().isEmpty());
}

QTEST_MAIN(TestBottomBar)
#include "test_bottom_bar.moc"
