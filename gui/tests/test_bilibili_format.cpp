// ============================================================================
//  gui/tests/test_bilibili_format.cpp — 纯函数单测（T11-7）
//
//  这些函数决定"预览栏里那一格写什么字"和"回报给 Agent 的进度长什么样"，
//  与控件无关，所以能逐条钉死（也顺手挡住"0 秒写成 -1"这类丑事）。
// ============================================================================
#include <QJsonObject>
#include <QtTest/QtTest>

#include "core/bilibili_format.h"

class TestBilibiliFormat : public QObject {
    Q_OBJECT

private slots:
    void playCountUsesWanOnlyAboveTenThousand();
    void playCountHandlesZeroAndGarbage();
    void durationFormatsMinutesAndHours();
    void durationHandlesZero();
    void videoUrlIsTheBilibiliWatchPage();
    void videoUrlIsEmptyWithoutABvid();
    void previewMetaJoinsWhatItHas();
    void visibleCellsIsAtLeastOne();
    void visibleCellsCountsWhatFits();
    void videoStatePayloadUsesSeconds();
};

void TestBilibiliFormat::playCountUsesWanOnlyAboveTenThousand()
{
    QCOMPARE(bilibili::formatPlayCount(9719), QStringLiteral("9719"));
    QCOMPARE(bilibili::formatPlayCount(10000), QStringLiteral("1.0万"));
    QCOMPARE(bilibili::formatPlayCount(1289221), QStringLiteral("128.9万"));
    QCOMPARE(bilibili::formatPlayCount(262836), QStringLiteral("26.3万"));
}

void TestBilibiliFormat::playCountHandlesZeroAndGarbage()
{
    // 宁可显示 0，也不要 "-1" 或者 "nan"
    QCOMPARE(bilibili::formatPlayCount(0), QStringLiteral("0"));
    QCOMPARE(bilibili::formatPlayCount(-5), QStringLiteral("0"));
}

void TestBilibiliFormat::durationFormatsMinutesAndHours()
{
    QCOMPARE(bilibili::formatDuration(184), QStringLiteral("3:04"));
    QCOMPARE(bilibili::formatDuration(59), QStringLiteral("0:59"));
    // B 站自己的文字是"233:45"（分钟可以超过 60）；我们显示成 3:53:45 —— 同一段时间，
    // 只是过了一小时就换成 h:mm:ss（比"233:45"更不容易看错）
    QCOMPARE(bilibili::formatDuration(233 * 60 + 45), QStringLiteral("3:53:45"));
    QCOMPARE(bilibili::formatDuration(3723), QStringLiteral("1:02:03"));
}

void TestBilibiliFormat::durationHandlesZero()
{
    // 流式播放拿不到时长（板端实测 dur=0ms）→ 显示 0:00，不显示空白
    QCOMPARE(bilibili::formatDuration(0), QStringLiteral("0:00"));
    QCOMPARE(bilibili::formatDuration(-1), QStringLiteral("0:00"));
}

void TestBilibiliFormat::videoUrlIsTheBilibiliWatchPage()
{
    QCOMPARE(bilibili::videoUrl(QStringLiteral("BV1xx411c7mD")),
             QStringLiteral("https://www.bilibili.com/video/BV1xx411c7mD"));
    QCOMPARE(bilibili::videoUrl(QStringLiteral("  BV1xx411c7mD  ")),
             QStringLiteral("https://www.bilibili.com/video/BV1xx411c7mD"));
}

void TestBilibiliFormat::videoUrlIsEmptyWithoutABvid()
{
    QVERIFY(bilibili::videoUrl(QString()).isEmpty());
    QVERIFY(bilibili::videoUrl(QStringLiteral("   ")).isEmpty());
}

void TestBilibiliFormat::previewMetaJoinsWhatItHas()
{
    QCOMPARE(bilibili::previewMeta(184, 1289221), QStringLiteral("3:04 · 128.9万"));
    QCOMPARE(bilibili::previewMeta(184, 0), QStringLiteral("3:04"));
    QCOMPARE(bilibili::previewMeta(0, 9719), QStringLiteral("9719"));
    QVERIFY(bilibili::previewMeta(0, 0).isEmpty());
}

void TestBilibiliFormat::visibleCellsIsAtLeastOne()
{
    // 布局还没算出来时宽度是 0 —— 上报 0 会让 Agent 那边夹到 1，不如这里就给 1
    QCOMPARE(bilibili::visibleCells(0, 120, 4), 1);
    QCOMPARE(bilibili::visibleCells(-10, 120, 4), 1);
    QCOMPARE(bilibili::visibleCells(400, 0, 4), 1);      // 格子宽度非法
}

void TestBilibiliFormat::visibleCellsCountsWhatFits()
{
    // 一格 120 + 间隔 4 → 124；840 宽放得下 6 格多一点 → 6
    QCOMPARE(bilibili::visibleCells(840, 120, 4), 6);
    QCOMPARE(bilibili::visibleCells(124, 120, 4), 1);
    QCOMPARE(bilibili::visibleCells(248, 120, 4), 2);
    QCOMPARE(bilibili::visibleCells(2000, 120, 4), 16);
}

void TestBilibiliFormat::videoStatePayloadUsesSeconds()
{
    const QJsonObject payload =
        bilibili::videoStatePayload(23840, 0, true, false);
    QCOMPARE(payload.value(QStringLiteral("position_s")).toDouble(), 23.84);
    QCOMPARE(payload.value(QStringLiteral("duration_s")).toDouble(), 0.0);   // 流式没时长
    QCOMPARE(payload.value(QStringLiteral("playing")).toBool(), true);
    QCOMPARE(payload.value(QStringLiteral("eof")).toBool(), false);

    const QJsonObject eof = bilibili::videoStatePayload(-100, 62000, false, true);
    QCOMPARE(eof.value(QStringLiteral("position_s")).toDouble(), 0.0);       // 负数夹到 0
    QCOMPARE(eof.value(QStringLiteral("duration_s")).toDouble(), 62.0);
    QCOMPARE(eof.value(QStringLiteral("playing")).toBool(), false);
    QCOMPARE(eof.value(QStringLiteral("eof")).toBool(), true);
}

QTEST_MAIN(TestBilibiliFormat)
#include "test_bilibili_format.moc"
