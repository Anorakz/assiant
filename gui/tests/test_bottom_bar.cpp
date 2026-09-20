// ============================================================================
//  gui/tests/test_bottom_bar.cpp — 下区域（音乐条 / 封面）控件级测试（T7）
//
//  覆盖：music 数据 → 曲目名与播放徽标；占位块点击 → 说明文案 + 信号；
//        GAME ↔ 非游戏 的页面互斥切换；歌词接口的占位实现。
// ============================================================================
#include <QLabel>
#include <QProgressBar>
#include <QPushButton>
#include <QtTest/QtTest>

#include "core/lyrics.h"
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

QTEST_MAIN(TestBottomBar)
#include "test_bottom_bar.moc"
