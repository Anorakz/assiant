// ============================================================================
//  gui/tests/test_bilibili_preview.cpp — 预览栏 + 地址栏单测（T11-7）
//
//  钉的是你定的那几条：
//    · 队列一到就**填满预览图与地址栏**（标题/时长/播放量都在格子里）；
//    · 点第 N 格 = `bilibili_pick{index:N}`（代码移动高亮**不算**点击）；
//    · 可见格数变化才上报 `bilibili_viewport{visible:N}`，**队列为空时不上报**
//      （否则 B 站没开时会白弹一句"B 站视频还没开"的气泡）；
//    · 地址栏是**只读**的；
//    · 这一层**没有清晰度字段**（你定的：清晰度只走聊天气泡）。
// ============================================================================
#include <QBuffer>
#include <QImage>
#include <QJsonArray>
#include <QJsonObject>
#include <QLabel>
#include <QLineEdit>
#include <QListWidget>
#include <QStackedWidget>
#include <QUrl>
#include <QtTest/QtTest>

#include <memory>

#include "ui/bilibili_preview.h"
#include "ui/cover_loader.h"

namespace {

/// 固定的三位编号：BV000 / BV001 / …（别写 "BV%100" —— `%1` + "00" 会拼出怪东西）
QString bvidOf(int i)
{
    return QStringLiteral("BV%1").arg(i, 3, 10, QLatin1Char('0'));
}

QJsonObject makeItem(int i)
{
    QJsonObject item;
    item.insert(QStringLiteral("bvid"), bvidOf(i));
    item.insert(QStringLiteral("title"), QStringLiteral("标题 %1").arg(i));
    item.insert(QStringLiteral("author"), QStringLiteral("作者 %1").arg(i));
    item.insert(QStringLiteral("duration_s"), 184);
    item.insert(QStringLiteral("play"), 1289221);
    item.insert(QStringLiteral("cover"), QStringLiteral("https://i0.hdslb.com/%1.jpg").arg(i));
    item.insert(QStringLiteral("url"), QStringLiteral("https://www.bilibili.com/video/%1").arg(bvidOf(i)));
    return item;
}

QJsonObject makePayload(int count, int index, const QString& keyword = QStringLiteral("luna say maybe"),
                        const QString& source = QStringLiteral("dialogue"),
                        const QString& stream = QString())
{
    QJsonArray queue;
    for (int i = 0; i < count; ++i) {
        queue.append(makeItem(i));
    }
    QJsonObject data;
    data.insert(QStringLiteral("queue"), queue);
    data.insert(QStringLiteral("index"), index);
    data.insert(QStringLiteral("count"), count);
    data.insert(QStringLiteral("target"), count * 3);
    data.insert(QStringLiteral("keyword"), keyword);
    data.insert(QStringLiteral("source"), source);
    data.insert(QStringLiteral("stream"), stream);
    if (count > 0) {
        data.insert(QStringLiteral("current"), makeItem(index));
    }
    data.insert(QStringLiteral("ok"), true);
    return data;
}

/// 一张能认的小 PNG（给取图器回）
QByteArray pngBytes()
{
    QImage image(2, 2, QImage::Format_RGB32);
    image.fill(Qt::blue);
    QByteArray bytes;
    QBuffer buffer(&bytes);
    buffer.open(QIODevice::WriteOnly);
    image.save(&buffer, "PNG");
    return bytes;
}

/// 让控件真的被布局过（offscreen 平台下 show() 就够了）
void layOut(BilibiliPreview& preview, int width = 840)
{
    preview.resize(width, BilibiliPreview::barHeight());
    preview.show();
    QTest::qWait(20);
}

} // namespace

class TestBilibiliPreview : public QObject {
    Q_OBJECT

private slots:
    void emptyQueueShowsAHintNotAList();
    void emptyQueueNeverReportsAViewport();
    void aQueueFillsTheListAndTheAddressBar();
    void theCurrentItemIsHighlighted();
    void clickingACellAsksForThatIndex();
    void movingTheHighlightIsNotAClick();
    void outOfRangePickIsRefused();
    void theAddressBarIsReadOnly();
    void viewportIsReportedOncePerChange();
    void aShrinkingWindowClampsTheIndex();
    void theSourceLineSaysWhereTheQueueCameFrom();
    void coversAreRequestedAroundTheCurrentItem();
    void noClarityFieldAnywhere();
};

void TestBilibiliPreview::emptyQueueShowsAHintNotAList()
{
    BilibiliPreview preview;
    preview.setData(QJsonObject());

    QCOMPARE(preview.itemCount(), 0);
    QCOMPARE(preview.pages()->currentWidget(), static_cast<QWidget*>(preview.hintLabel()));
    QVERIFY(preview.hintLabel()->text().contains(QStringLiteral("队列是空的")));
    QVERIFY(preview.addressBar()->text().isEmpty());
    QVERIFY(preview.currentBvid().isEmpty());
}

void TestBilibiliPreview::emptyQueueNeverReportsAViewport()
{
    BilibiliPreview preview;
    layOut(preview);
    QSignalSpy viewport(&preview, &BilibiliPreview::viewportChanged);

    preview.setData(QJsonObject());
    preview.setData(makePayload(0, 0, QString()));

    QCOMPARE(viewport.count(), 0);
    QCOMPARE(preview.reportedCells(), 0);
}

void TestBilibiliPreview::aQueueFillsTheListAndTheAddressBar()
{
    BilibiliPreview preview;
    layOut(preview);
    preview.setData(makePayload(18, 2));

    QCOMPARE(preview.itemCount(), 18);
    QCOMPARE(preview.currentBvid(), QStringLiteral("BV002"));
    QCOMPARE(preview.addressBar()->text(),
             QStringLiteral("https://www.bilibili.com/video/BV002"));
    QCOMPARE(preview.list()->count(), 18);

    // 格子里要有标题，也要有"时长 · 播放量"
    const QString text = preview.list()->item(0)->text();
    QVERIFY(text.contains(QStringLiteral("标题 0")));
    QVERIFY(text.contains(QStringLiteral("3:04")));
    QVERIFY(text.contains(QStringLiteral("128.9万")));
    // tooltip 里有作者与真地址（验收时一眼能核对）
    QVERIFY(preview.list()->item(0)->toolTip().contains(QStringLiteral("作者 0")));
    QVERIFY(preview.list()->item(0)->toolTip().contains(QStringLiteral("bilibili.com/video")));
}

void TestBilibiliPreview::theCurrentItemIsHighlighted()
{
    BilibiliPreview preview;
    layOut(preview);
    preview.setData(makePayload(18, 5));

    QCOMPARE(preview.list()->currentRow(), 5);
    QCOMPARE(preview.currentIndex(), 5);
    QVERIFY(preview.currentBvid() == QStringLiteral("BV005"));
}

void TestBilibiliPreview::clickingACellAsksForThatIndex()
{
    BilibiliPreview preview;
    layOut(preview);
    preview.setData(makePayload(18, 0));
    QSignalSpy pick(&preview, &BilibiliPreview::pickRequested);

    QCOMPARE(preview.activateItem(7), 7);          // 与真点击同一个入口
    QCOMPARE(pick.count(), 1);
    QCOMPARE(pick.first().at(0).toInt(), 7);
}

void TestBilibiliPreview::movingTheHighlightIsNotAClick()
{
    BilibiliPreview preview;
    layOut(preview);
    preview.setData(makePayload(18, 0));
    QSignalSpy pick(&preview, &BilibiliPreview::pickRequested);

    preview.setData(makePayload(18, 9));           // Agent 说"现在放到第 9 条"
    QCOMPARE(preview.list()->currentRow(), 9);
    QCOMPARE(pick.count(), 0);                     // ⚠ 绝不能当成"用户点了第 9 格"
}

void TestBilibiliPreview::outOfRangePickIsRefused()
{
    BilibiliPreview preview;
    layOut(preview);
    preview.setData(makePayload(3, 0));
    QSignalSpy pick(&preview, &BilibiliPreview::pickRequested);

    QCOMPARE(preview.activateItem(9), -1);
    QCOMPARE(preview.activateItem(-1), -1);
    QCOMPARE(pick.count(), 0);
}

void TestBilibiliPreview::theAddressBarIsReadOnly()
{
    BilibiliPreview preview;
    preview.setData(makePayload(3, 0));
    QVERIFY(preview.addressBar()->isReadOnly());   // 关键词只从对话/画面来
    QVERIFY(!preview.addressBar()->isEnabled() || preview.addressBar()->isReadOnly());
}

void TestBilibiliPreview::viewportIsReportedOncePerChange()
{
    BilibiliPreview preview;
    layOut(preview, 840);
    QSignalSpy viewport(&preview, &BilibiliPreview::viewportChanged);

    preview.setData(makePayload(18, 0));
    QCOMPARE(viewport.count(), 1);
    const int first = viewport.first().at(0).toInt();
    QVERIFY(first >= 1);
    QCOMPARE(preview.reportedCells(), first);

    preview.setData(makePayload(18, 1));           // 同一尺寸再来一条：不重复上报
    QCOMPARE(viewport.count(), 1);

    // 窗口变窄 → 格数变了 → 上报一次新的
    layOut(preview, 400);
    preview.setData(makePayload(18, 1));
    QCOMPARE(viewport.count(), 2);
    QVERIFY(viewport.at(1).at(0).toInt() < first);
}

void TestBilibiliPreview::aShrinkingWindowClampsTheIndex()
{
    BilibiliPreview preview;
    layOut(preview);
    preview.setData(makePayload(18, 12));
    QCOMPARE(preview.currentIndex(), 12);

    preview.setData(makePayload(4, 12));           // 窗口只剩 4 条、索引还是 12
    QCOMPARE(preview.itemCount(), 4);
    QCOMPARE(preview.currentIndex(), 3);           // 夹到最后一条，不越界
}

void TestBilibiliPreview::theSourceLineSaysWhereTheQueueCameFrom()
{
    BilibiliPreview preview;
    layOut(preview);

    preview.setData(makePayload(3, 0, QStringLiteral("luna say maybe"), QStringLiteral("dialogue")));
    QCOMPARE(preview.sourceLabel()->text(), QStringLiteral("对话：luna say maybe"));

    preview.setData(makePayload(3, 0, QStringLiteral("white album"), QStringLiteral("screen")));
    QCOMPARE(preview.sourceLabel()->text(), QStringLiteral("画面：white album"));
}

void TestBilibiliPreview::coversAreRequestedAroundTheCurrentItem()
{
    auto log = std::make_shared<int>(0);
    CoverLoader loader([log](const QUrl&, const QHash<QByteArray, QByteArray>&,
                             std::function<void(const QByteArray&, bool)> done) {
        ++(*log);
        done(pngBytes(), true);
    });
    BilibiliPreview preview;
    preview.setCoverLoader(&loader);
    layOut(preview);
    preview.setData(makePayload(60, 30));          // 窗口 60 条，但只取看得见的那一圈

    QVERIFY(*log > 0);
    QVERIFY2(*log <= 3 * 8, "只该取当前附近的那几格，不能一口气下 60 张");
    QVERIFY(loader.cacheSize() > 0);
    // 当前那一格的图已经到了（图标不是空的）
    QVERIFY(!preview.list()->item(30)->icon().isNull());
}

void TestBilibiliPreview::noClarityFieldAnywhere()
{
    // 你定的：清晰度**只走聊天气泡**，GUI 一个字段都不加。
    // 所以就算 Agent 多推了 quality，这里也不该有任何地方显示它。
    BilibiliPreview preview;
    layOut(preview);
    QJsonObject data = makePayload(3, 0);
    data.insert(QStringLiteral("quality"), QStringLiteral("360P"));
    preview.setData(data);

    const QStringList surfaces = {preview.addressBar()->text(), preview.sourceLabel()->text(),
                                  preview.hintLabel()->text(), preview.list()->item(0)->text(),
                                  preview.list()->item(0)->toolTip()};
    for (const QString& text : surfaces) {
        QVERIFY2(!text.contains(QStringLiteral("360P")),
                 qPrintable(QStringLiteral("界面上出现了清晰度: %1").arg(text)));
        QVERIFY2(!text.contains(QStringLiteral("清晰度")), qPrintable(text));
    }
}

QTEST_MAIN(TestBilibiliPreview)
#include "test_bilibili_preview.moc"
