// ============================================================================
//  gui/tests/test_bottom_bar.cpp — 下区域（音乐条 / 封面）控件级测试（T7 / T15-16）
//
//  覆盖：music 载荷 → 曲目名 / 歌手 / 专辑 / 进度（T15-16 起全部点亮）；
//        三个控制按钮 → **各自的信号**（由主窗口转成现成命令）；
//        断连时按钮禁用；歌词两行（真 `TimedLyrics` + `TimedLyricsProvider`）与
//        "为什么没有歌词"的三种态；GAME ↔ 非游戏 的页面互斥切换；封面页那几条。
// ============================================================================
#include <QJsonArray>
#include <QJsonObject>
#include <QLabel>
#include <QProgressBar>
#include <QPushButton>
#include <QtTest/QtTest>

#include "core/lyrics.h"
#include "ui/bilibili_cover.h"
#include "ui/bottom_bar.h"
#include "ui/music_bar.h"

namespace {

/// 一条 `music` 载荷的替身（只带测试要看的字段；其余省略 = 部分更新）
QJsonObject music(const QString& title, bool playing, const QString& artist = QString(),
                  const QString& album = QString(), double position = -1.0,
                  double duration = -1.0)
{
    QJsonObject out;
    out.insert(QStringLiteral("title"), title);
    out.insert(QStringLiteral("playing"), playing);
    if (!artist.isNull()) {
        out.insert(QStringLiteral("artist"), artist);
    }
    if (!album.isNull()) {
        out.insert(QStringLiteral("album"), album);
    }
    if (position >= 0.0) {
        out.insert(QStringLiteral("position_s"), position);
    }
    if (duration >= 0.0) {
        out.insert(QStringLiteral("duration_s"), duration);
    }
    return out;
}

QJsonObject lyricRow(double t, const QString& text, const QString& tr = QString())
{
    QJsonObject row;
    row.insert(QStringLiteral("t"), t);
    row.insert(QStringLiteral("text"), text);
    if (!tr.isEmpty()) {
        row.insert(QStringLiteral("tr"), tr);
    }
    return row;
}

} // namespace

class TestBottomBar : public QObject {
    Q_OBJECT

private slots:
    void musicUpdatesTitleAndState();
    void playButtonIsBiggerAndShowsAction();
    void musicLightsUpArtistAlbumAndProgress();
    void controlButtonsEmitTheirOwnSignals();
    void controlButtonsAreDisabledWhileDisconnected();
    void lyricsShowTwoLinesFromTheProvider();
    void lyricsExplainWhyTheyAreMissing();
    void gameModeSwitchesToCoverPage();
    void nullLyricsProviderIsUnavailable();
    void coverPageIsTheRealWidgetAndSaysWhereItComesFrom();
    void coverTitleIsElidedInsteadOfStretchingTheLayout();
    void coverPageFallsBackToAPlaceholderWithoutACurrentItem();
};

void TestBottomBar::musicUpdatesTitleAndState()
{
    MusicBar bar;
    // 还没收到 music：显示"未播放"，大按钮给播放图标
    QCOMPARE(bar.title(), QStringLiteral("未播放"));
    QVERIFY(!bar.hasMusic());
    QVERIFY(!bar.playButton()->icon().isNull());
    QCOMPARE(bar.playButton()->text(), QString());

    bar.setMusic(music(QStringLiteral("夜曲"), true));
    QVERIFY(bar.hasMusic());
    QVERIFY(bar.playing());
    QCOMPARE(bar.title(), QStringLiteral("夜曲"));
    QVERIFY(!bar.playButton()->icon().isNull());                  // 在播 -> 图标是"暂停"

    bar.setMusic(music(QStringLiteral("夜曲"), false));
    QVERIFY(!bar.playing());
    QVERIFY(!bar.playButton()->icon().isNull());

    // 显式空标题 -> 回到"未播放"，不崩
    bar.setMusic(music(QString(), false));
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

void TestBottomBar::musicLightsUpArtistAlbumAndProgress()
{
    MusicBar bar;
    bar.setMusic(music(QStringLiteral("晴天"), true, QStringLiteral("周杰伦"),
                       QStringLiteral("叶惠美"), 70.3, 269.0));
    QCOMPARE(bar.artistLabel()->text(), QStringLiteral("歌手 周杰伦"));
    QCOMPARE(bar.albumLabel()->text(), QStringLiteral("专辑 叶惠美"));
    QCOMPARE(bar.timeLabel()->text(), QStringLiteral("1:10 / 4:29"));
    QVERIFY(bar.progressBar()->isEnabled());
    // 千分比：70.3 / 269 = 26.1%
    QVERIFY(bar.progressBar()->value() >= 255 && bar.progressBar()->value() <= 265);

    // 歌手/专辑**字段在但值为空** -> 回到"—"（部分更新：不把上一次的值留着骗人）
    QJsonObject cleared = music(QStringLiteral("晴天"), true);
    cleared.insert(QStringLiteral("artist"), QString());
    cleared.insert(QStringLiteral("album"), QString());
    bar.setMusic(cleared);
    QCOMPARE(bar.artistLabel()->text(), QStringLiteral("歌手 —"));
    QCOMPARE(bar.albumLabel()->text(), QStringLiteral("专辑 —"));

    // 没有时长 -> 不可交互 + "—:—"
    bar.setProgress(0.0, 0.0);
    QVERIFY(!bar.progressBar()->isEnabled());
    QCOMPARE(bar.timeLabel()->text(), QStringLiteral("—:— / —:—"));
}

void TestBottomBar::controlButtonsEmitTheirOwnSignals()
{
    MusicBar bar;
    bar.setMusic(music(QStringLiteral("夜曲"), true));
    bar.setConnected(true);

    QSignalSpy prev(&bar, &MusicBar::prevClicked);
    QSignalSpy play(&bar, &MusicBar::playPauseClicked);
    QSignalSpy next(&bar, &MusicBar::nextClicked);

    bar.previousButton()->click();
    QCOMPARE(prev.count(), 1);
    QCOMPARE(play.count(), 0);
    bar.playButton()->click();
    QCOMPARE(play.count(), 1);
    bar.nextButton()->click();
    QCOMPARE(next.count(), 1);
    QCOMPARE(prev.count(), 1);

    // 点按钮**不改**播放状态（真状态由 Agent 推回来）
    QVERIFY(bar.playing());
}

void TestBottomBar::controlButtonsAreDisabledWhileDisconnected()
{
    MusicBar bar;
    bar.setMusic(music(QStringLiteral("夜曲"), true));
    QVERIFY(!bar.connected());
    QVERIFY(!bar.previousButton()->isEnabled());
    QVERIFY(!bar.playButton()->isEnabled());
    QVERIFY(!bar.nextButton()->isEnabled());

    QSignalSpy next(&bar, &MusicBar::nextClicked);
    bar.nextButton()->click();
    QCOMPARE(next.count(), 0);                    // 禁用的按钮点不动

    bar.setConnected(true);
    QVERIFY(bar.previousButton()->isEnabled());
    QVERIFY(bar.playButton()->isEnabled());
    QVERIFY(bar.nextButton()->isEnabled());
}

void TestBottomBar::lyricsShowTwoLinesFromTheProvider()
{
    MusicBar bar;
    QCOMPARE(bar.lyricsLabel()->text(), QStringLiteral("歌词未接入"));

    core::TimedLyrics lyrics;
    QJsonArray rows;
    rows.append(lyricRow(0.0, QStringLiteral("原文一"), QStringLiteral("译文一")));
    rows.append(lyricRow(10.0, QStringLiteral("原文二")));
    lyrics.setLines(rows, 1, true);
    core::TimedLyricsProvider provider(&lyrics);

    bar.setLyricsProvider(&provider);
    bar.setPosition(0.0);
    QCOMPARE(bar.lyricsLabel()->text(), QStringLiteral("译文一"));      // 优先译文
    QCOMPARE(bar.nextLyricsLabel()->text(), QStringLiteral("原文二"));

    bar.setPosition(12.0);
    QCOMPARE(bar.lyricsLabel()->text(), QStringLiteral("原文二"));      // 没译文的那行
    QCOMPARE(bar.nextLyricsLabel()->text(), QString());
}

void TestBottomBar::lyricsExplainWhyTheyAreMissing()
{
    MusicBar bar;

    // ① 有说法的（纯音乐 / 取不到）：把 Agent 那句话**原样**显示
    core::TimedLyrics none;
    none.setLines(QJsonArray(), 2, false, QStringLiteral("没有歌词"));
    core::TimedLyricsProvider provider(&none);
    bar.setLyricsProvider(&provider);
    QCOMPARE(bar.lyricsLabel()->text(), QStringLiteral("没有歌词"));
    QCOMPARE(bar.nextLyricsLabel()->text(), QString());

    // ② 还没取到（reason 空）：给一个灰点，别让人以为界面坏了
    core::TimedLyrics pending;
    core::TimedLyricsProvider pendingProvider(&pending);
    bar.setLyricsProvider(&pendingProvider);
    QCOMPARE(bar.lyricsLabel()->text(), QStringLiteral("♪"));

    // ③ 没有任何来源（老 Agent / 演示）：回到老文案
    bar.setLyricsProvider(nullptr);
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
