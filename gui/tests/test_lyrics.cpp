// ============================================================================
//  gui/tests/test_lyrics.cpp — 歌词时间轴单测（T15-16-4）
//
//  这一层**不认识时钟、也不认识控件**：给一份载荷 + 一个播放位置，问"现在显示哪句、
//  下一句是什么、下一次换行在几秒"。所以这里能把它钉得很死：
//    · 前奏（位置在第一行之前）/ 正好等于某行时间戳 / 最后一行之后
//    · **优先译文**：`tr` 非空取译文，空则退回原文（用户 2026-10-04 定的口径）
//    · 坏行跳过、乱序输入排序、`lyric_ok=true` 却一行都没有 -> 按没有处理
//    · `lyric_rev` 没变就不重设时间轴；缺字段时保留旧 rev（协议是部分更新）
//    · `TimedLyricsProvider`（D5 预留接口的真实现）跟着喂进来的位置走
// ============================================================================
#include <QJsonArray>
#include <QJsonObject>
#include <QString>
#include <QtTest/QtTest>

#include <initializer_list>

#include "core/lyrics.h"

using core::LyricLine;
using core::NullLyricsProvider;
using core::TimedLyrics;
using core::TimedLyricsProvider;

namespace {

QJsonObject row(double t, const QString& text, const QString& tr = QString())
{
    QJsonObject out;
    out.insert(QStringLiteral("t"), t);
    out.insert(QStringLiteral("text"), text);
    if (!tr.isEmpty()) {
        out.insert(QStringLiteral("tr"), tr);
    }
    return out;
}

QJsonArray array(std::initializer_list<QJsonObject> items)
{
    QJsonArray out;
    for (const QJsonObject& item : items) {
        out.append(item);
    }
    return out;
}

QJsonObject payload(const QJsonArray& lines, int rev, bool ok = true,
                    const QString& reason = QString())
{
    QJsonObject music;
    music.insert(QStringLiteral("lyric_ok"), ok);
    music.insert(QStringLiteral("lyric_lines"), lines);
    music.insert(QStringLiteral("lyric_rev"), rev);
    music.insert(QStringLiteral("lyric_reason"), reason);
    return music;
}

/// 两句歌词：0.0 秒（带译文）与 10.0 秒（没译文）
QJsonArray twoLines()
{
    return array({row(0.0, QStringLiteral("原文一"), QStringLiteral("译文一")),
                  row(10.0, QStringLiteral("原文二"))});
}

} // namespace

class TestLyrics : public QObject {
    Q_OBJECT

private slots:
    void emptyByDefault();
    void applyPayloadTakesTheFields();
    void applyPayloadIgnoresAnUnchangedPayload();
    void aMissingRevisionKeepsTheOldOne();
    void noLineBeforeTheFirstOne();
    void picksTheLineAtItsExactTimestamp();
    void picksTheLastLinePastItsStart();
    void translationWinsOverTheOriginal();
    void nextLineWalksForward();
    void nextChangeAtTellsWhenToWakeUp();
    void badRowsAreSkipped();
    void outOfOrderRowsAreSorted();
    void claimingLyricsWithoutLinesCountsAsNone();
    void clearResetsEverything();
    void providerFollowsThePosition();
    void providerWithoutASourceIsUnavailable();
    void nullProviderStaysUnavailable();
};

void TestLyrics::emptyByDefault()
{
    TimedLyrics lyrics;
    QVERIFY(!lyrics.hasLyric());
    QVERIFY(lyrics.isEmpty());
    QCOMPARE(lyrics.count(), 0);
    QCOMPARE(lyrics.rev(), 0);
    QCOMPARE(lyrics.reason(), QString());
    QCOMPARE(lyrics.lineAt(3.0), QString());
    QCOMPARE(lyrics.indexAt(3.0), -1);
    QCOMPARE(lyrics.nextChangeAt(3.0), -1.0);
}

void TestLyrics::applyPayloadTakesTheFields()
{
    TimedLyrics lyrics;
    QVERIFY(lyrics.applyPayload(payload(twoLines(), 7)));
    QVERIFY(lyrics.hasLyric());
    QCOMPARE(lyrics.count(), 2);
    QCOMPARE(lyrics.rev(), 7);
    QCOMPARE(lyrics.reason(), QString());
    QCOMPARE(lyrics.lines().at(1).t, 10.0);
    QCOMPARE(lyrics.lines().at(1).text, QStringLiteral("原文二"));
}

void TestLyrics::applyPayloadIgnoresAnUnchangedPayload()
{
    TimedLyrics lyrics;
    QVERIFY(lyrics.applyPayload(payload(twoLines(), 7)));
    QVERIFY(!lyrics.applyPayload(payload(twoLines(), 7)));   // 同一份 -> 没换
    QVERIFY(lyrics.applyPayload(payload(twoLines(), 8)));    // rev 变了 -> 换了
}

void TestLyrics::aMissingRevisionKeepsTheOldOne()
{
    // 协议是**部分更新**（ipc-protocol.md §2 规则 2）：没带的字段保持原值
    TimedLyrics lyrics;
    QVERIFY(lyrics.applyPayload(payload(twoLines(), 4)));
    QJsonObject partial;
    partial.insert(QStringLiteral("lyric_ok"), true);
    partial.insert(QStringLiteral("lyric_lines"), twoLines());
    QVERIFY2(!lyrics.applyPayload(partial), "rev 与内容都没变 -> 不算换");
    QCOMPARE(lyrics.rev(), 4);
}

void TestLyrics::noLineBeforeTheFirstOne()
{
    TimedLyrics lyrics;
    lyrics.setLines(twoLines(), 1, true);
    QCOMPARE(lyrics.indexAt(-1.0), -1);
    QCOMPARE(lyrics.lineAt(0.0 - 0.001), QString());
    QCOMPARE(lyrics.nextLineAt(-5.0), QStringLiteral("译文一"));   // 前奏里下一句就是第一句
    QCOMPARE(lyrics.nextChangeAt(-5.0), 0.0);                     // 定时器该定到第一句那一刻
}

void TestLyrics::picksTheLineAtItsExactTimestamp()
{
    TimedLyrics lyrics;
    lyrics.setLines(twoLines(), 1, true);
    QCOMPARE(lyrics.indexAt(0.0), 0);
    QCOMPARE(lyrics.lineAt(0.0), QStringLiteral("译文一"));
    QCOMPARE(lyrics.indexAt(10.0), 1);
    QCOMPARE(lyrics.lineAt(10.0), QStringLiteral("原文二"));
}

void TestLyrics::picksTheLastLinePastItsStart()
{
    TimedLyrics lyrics;
    lyrics.setLines(twoLines(), 1, true);
    QCOMPARE(lyrics.indexAt(9.999), 0);            // 10 秒之前还是第一句
    QCOMPARE(lyrics.indexAt(999.0), 1);            // 最后一行之后一直显示最后一句
    QCOMPARE(lyrics.nextLineAt(999.0), QString());
}

void TestLyrics::translationWinsOverTheOriginal()
{
    TimedLyrics lyrics;
    lyrics.setLines(twoLines(), 1, true);
    QCOMPARE(lyrics.lineAt(0.0), QStringLiteral("译文一"));
    QCOMPARE(lyrics.lineAt(10.0), QStringLiteral("原文二"));   // 没有译文的那行退回原文
    QCOMPARE(lyrics.lines().at(0).text, QStringLiteral("原文一"));   // 原文照样留着
    QCOMPARE(lyrics.lines().at(1).tr, QString());
}

void TestLyrics::nextLineWalksForward()
{
    TimedLyrics lyrics;
    lyrics.setLines(twoLines(), 1, true);
    QCOMPARE(lyrics.nextLineAt(0.0), QStringLiteral("原文二"));
    QCOMPARE(lyrics.nextLineAt(10.0), QString());
}

void TestLyrics::nextChangeAtTellsWhenToWakeUp()
{
    TimedLyrics lyrics;
    lyrics.setLines(array({row(0.0, QStringLiteral("甲")), row(10.0, QStringLiteral("乙")),
                           row(20.0, QStringLiteral("丙"))}), 1, true);
    QCOMPARE(lyrics.nextChangeAt(0.0), 10.0);
    QCOMPARE(lyrics.nextChangeAt(15.0), 20.0);
    QCOMPARE(lyrics.nextChangeAt(25.0), -1.0);     // 最后一句之后没有下一次换行
}

void TestLyrics::badRowsAreSkipped()
{
    QJsonArray rows = twoLines();
    rows.append(QStringLiteral("不是对象"));                       // 非 object
    QJsonObject noStamp;
    noStamp.insert(QStringLiteral("text"), QStringLiteral("没有 t"));
    rows.append(noStamp);                                        // 缺时间戳
    QJsonObject badStamp;
    badStamp.insert(QStringLiteral("t"), QStringLiteral("十秒"));
    rows.append(badStamp);                                       // t 不是数字

    TimedLyrics lyrics;
    lyrics.setLines(rows, 3, true);
    QCOMPARE(lyrics.count(), 2);                   // 坏行被跳过，好行照收
    QVERIFY(lyrics.hasLyric());
}

void TestLyrics::outOfOrderRowsAreSorted()
{
    TimedLyrics lyrics;
    lyrics.setLines(array({row(30.0, QStringLiteral("丙")), row(10.0, QStringLiteral("乙")),
                           row(0.0, QStringLiteral("甲"))}), 1, true);
    QCOMPARE(lyrics.lines().at(0).text, QStringLiteral("甲"));
    QCOMPARE(lyrics.lines().at(2).text, QStringLiteral("丙"));
    QCOMPARE(lyrics.lineAt(15.0), QStringLiteral("乙"));
}

void TestLyrics::claimingLyricsWithoutLinesCountsAsNone()
{
    // 防御：Agent 说"有歌词"却一行都没给 —— 当成没有，而不是显示空白
    TimedLyrics lyrics;
    lyrics.setLines(QJsonArray(), 2, true);
    QVERIFY(!lyrics.hasLyric());
    QCOMPARE(lyrics.lineAt(1.0), QString());
}

void TestLyrics::clearResetsEverything()
{
    TimedLyrics lyrics;
    lyrics.setLines(twoLines(), 5, true);
    lyrics.clear();
    QVERIFY(!lyrics.hasLyric());
    QVERIFY(lyrics.isEmpty());
    QCOMPARE(lyrics.lineAt(0.0), QString());
    QCOMPARE(lyrics.reason(), QString());
}

void TestLyrics::providerFollowsThePosition()
{
    TimedLyrics lyrics;
    lyrics.setLines(twoLines(), 1, true);
    TimedLyricsProvider provider(&lyrics);
    QVERIFY(provider.available());

    provider.setPosition(0.0);
    QCOMPARE(provider.currentLine(), QStringLiteral("译文一"));
    QCOMPARE(provider.nextLine(), QStringLiteral("原文二"));

    provider.setPosition(12.0);
    QCOMPARE(provider.currentLine(), QStringLiteral("原文二"));
    QCOMPARE(provider.nextLine(), QString());
}

void TestLyrics::providerWithoutASourceIsUnavailable()
{
    TimedLyricsProvider provider;
    QVERIFY(!provider.available());
    QCOMPARE(provider.currentLine(), QString());
    QCOMPARE(provider.nextLine(), QString());
    QCOMPARE(provider.reason(), QString());
}

void TestLyrics::nullProviderStaysUnavailable()
{
    // D5 的占位实现不变（旧断言在 test_bottom_bar.cpp 里也有一份）
    NullLyricsProvider provider;
    QVERIFY(!provider.available());
    QCOMPARE(provider.currentLine(), QString());
    QCOMPARE(provider.nextLine(), QString());
}

QTEST_APPLESS_MAIN(TestLyrics)
#include "test_lyrics.moc"
