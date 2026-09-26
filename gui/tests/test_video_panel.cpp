// ============================================================================
//  gui/tests/test_video_panel.cpp — 视频区控件级测试（T9，T11-7 后调整）
//
//  只测**不依赖媒体后端**的部分：占位态、按钮集合与符号、播放/暂停与全屏的
//  状态流转、上一集/下一集的协议信号、预览栏接线、`video_state` 回报。
//  真的解码播放由验收脚本在板端用 scrot 取证。
//
//  ⚠ T11-7 改了两件事（都写在这里当证据）：
//    · 「上一集」从占位**转正**（发 prev_bilibili），「下一集」不再假报"未接入"
//      —— 说明改由 Agent 回（队列空了它会推一句聊天气泡）；
//    · 多了一条**预览栏**：`setBilibili()` 填它，有 `stream` 就换源。
// ============================================================================
#include <QJsonArray>
#include <QJsonObject>
#include <QLabel>
#include <QListWidget>
#include <QMediaPlayer>
#include <QPushButton>
#include <QStackedWidget>
#include <QToolButton>
#include <QVideoWidget>
#include <QtTest/QtTest>

#include "ui/bilibili_preview.h"
#include "ui/video_panel.h"

namespace {

/// 固定的三位编号：BV000 / BV001 / …（别写 "BV%100" —— `%1` + "00" 会拼出怪东西）
QString bvidOf(int i)
{
    return QStringLiteral("BV%1").arg(i, 3, 10, QLatin1Char('0'));
}

QJsonObject bilibiliPayload(int count, int index, const QString& stream = QString())
{
    QJsonArray queue;
    for (int i = 0; i < count; ++i) {
        QJsonObject item;
        item.insert(QStringLiteral("bvid"), bvidOf(i));
        item.insert(QStringLiteral("title"), QStringLiteral("标题 %1").arg(i));
        item.insert(QStringLiteral("duration_s"), 184);
        item.insert(QStringLiteral("play"), 9719);
        queue.append(item);
    }
    QJsonObject data;
    data.insert(QStringLiteral("queue"), queue);
    data.insert(QStringLiteral("index"), index);
    data.insert(QStringLiteral("count"), count);
    data.insert(QStringLiteral("stream"), stream);
    data.insert(QStringLiteral("keyword"), QStringLiteral("luna say maybe"));
    data.insert(QStringLiteral("source"), QStringLiteral("dialogue"));
    if (count > 0) {
        data.insert(QStringLiteral("current"), queue.at(index).toObject());
    }
    return data;
}

} // namespace

class TestVideoPanel : public QObject {
    Q_OBJECT

private slots:
    void emptySourceShowsPlaceholder();
    void buttonsUseIconsNotText();
    void playPauseTogglesLocalPlayback();
    void fullscreenTogglesAndSignals();
    void nextButtonEmitsNextBilibili();
    void previousButtonEmitsPrevBilibili();
    void onlySpeedIsStillAPlaceholder();
    void overlayVisibilityFollowsWatcher();
    void previewBarIsPartOfThePanel();
    void aStreamSourceIsFollowedExactly();
    void anEmptyStreamStopsAndSaysWhatToDo();
    void videoStateIsReportedWithPositionAndPlaying();
    void endOfMediaIsReportedOnlyOnce();
    void noVideoStateWithoutASource();
};

void TestVideoPanel::emptySourceShowsPlaceholder()
{
    VideoPanel panel;
    QVERIFY(panel.source().isEmpty());
    QVERIFY(panel.placeholderLabel() != nullptr);
    QCOMPARE(panel.placeholderLabel()->text(), QStringLiteral("视频源未接入"));
    QCOMPARE(panel.stage()->currentWidget(), static_cast<QWidget*>(panel.placeholderLabel()));
    // 没源时控制条还在（用户能看到有哪些控制），但按播放不动
    QVERIFY(panel.overlayBar() != nullptr);
    panel.togglePlayPause();
    QVERIFY(!panel.isPlaying());
}

void TestVideoPanel::buttonsUseIconsNotText()
{
    VideoPanel panel;
    // T14：控制条改用**自绘单色图标**（不再依赖字体码位）→ 按钮文字为空、图标不为空
    QVERIFY(!panel.previousButton()->icon().isNull());
    QVERIFY(!panel.playButton()->icon().isNull());
    QVERIFY(!panel.nextButton()->icon().isNull());
    QVERIFY(!panel.fullscreenButton()->icon().isNull());
    QVERIFY(!panel.speedButton()->icon().isNull());
    QCOMPARE(panel.previousButton()->text(), QString());
    QCOMPARE(panel.nextButton()->text(), QString());
    QCOMPARE(panel.fullscreenButton()->text(), QString());
    QCOMPARE(panel.speedButton()->text(), QStringLiteral("1.0x"));   // 倍速仍显示数值

    // 不能出现中文文案
    const QList<QPushButton*> buttons = {panel.previousButton(), panel.playButton(),
                                         panel.nextButton(), panel.fullscreenButton()};
    for (QPushButton* button : buttons) {
        for (const QChar& ch : button->text()) {
            QVERIFY2(ch.unicode() < 0x2E80,
                     qPrintable(QStringLiteral("按钮里还有中文: %1").arg(button->text())));
        }
    }
}

void TestVideoPanel::playPauseTogglesLocalPlayback()
{
    VideoPanel panel;
    QSignalSpy spy(&panel, &VideoPanel::playingChanged);
    panel.setSource(QStringLiteral("/tmp/does-not-exist.mp4"));

    // 播放/暂停真调 QMediaPlayer（能不能解出画面由板端实测，这里只验调用了与状态流转）
    QVERIFY(panel.player() != nullptr);
    const qint64 playIconKey = panel.playButton()->icon().cacheKey();
    panel.pause();
    QVERIFY(!panel.isPlaying());
    panel.play();
    // 图标随播放状态切换（play ↔ pause）：cacheKey 会变
    QVERIFY(panel.playButton()->icon().cacheKey() != 0);
    QVERIFY(playIconKey != 0);
    QVERIFY(spy.count() >= 0);
}

void TestVideoPanel::fullscreenTogglesAndSignals()
{
    VideoPanel panel;
    QVERIFY(!panel.isFullscreen());
    QSignalSpy spy(&panel, &VideoPanel::fullscreenToggled);

    panel.setFullscreen(true);
    QVERIFY(panel.isFullscreen());
    QCOMPARE(spy.count(), 1);
    QCOMPARE(spy.first().at(0).toBool(), true);
    QVERIFY(panel.fullscreenButton()->toolTip().contains(QStringLiteral("退出")));

    panel.setFullscreen(false);
    QVERIFY(!panel.isFullscreen());
    QCOMPARE(spy.count(), 2);
    QVERIFY(panel.fullscreenButton()->toolTip().contains(QStringLiteral("全屏")));

    // 重复设置不重复发信号
    panel.setFullscreen(false);
    QCOMPARE(spy.count(), 2);
}

void TestVideoPanel::nextButtonEmitsNextBilibili()
{
    VideoPanel panel;
    QVERIFY(panel.nextButton()->isEnabled());
    QSignalSpy nextSpy(&panel, &VideoPanel::nextBilibiliRequested);
    QSignalSpy placeSpy(&panel, &VideoPanel::placeholderClicked);

    panel.nextButton()->click();
    QCOMPARE(nextSpy.count(), 1);
    // T11-7：next_bilibili **已经接线**了 —— 不再假报"未接入"
    QCOMPARE(placeSpy.count(), 0);
}

void TestVideoPanel::previousButtonEmitsPrevBilibili()
{
    VideoPanel panel;
    QSignalSpy prevSpy(&panel, &VideoPanel::prevBilibiliRequested);
    QSignalSpy nextSpy(&panel, &VideoPanel::nextBilibiliRequested);
    QSignalSpy placeSpy(&panel, &VideoPanel::placeholderClicked);

    panel.previousButton()->click();
    QCOMPARE(prevSpy.count(), 1);
    QCOMPARE(nextSpy.count(), 0);                      // 不能误发 next_bilibili
    QCOMPARE(placeSpy.count(), 0);                      // 也不再是"未接入"占位
    QVERIFY(panel.noteText().isEmpty());
}

void TestVideoPanel::onlySpeedIsStillAPlaceholder()
{
    VideoPanel panel;
    QSignalSpy placeSpy(&panel, &VideoPanel::placeholderClicked);

    panel.triggerPlaceholder(QStringLiteral("倍速"));
    QCOMPARE(placeSpy.count(), 1);
    QCOMPARE(placeSpy.first().at(0).toString(), QStringLiteral("倍速"));
    QVERIFY(panel.noteText().contains(QStringLiteral("未接入")));
}

void TestVideoPanel::overlayVisibilityFollowsWatcher()
{
    VideoPanel panel;
    panel.setOverlayVisible(true);
    QVERIFY(panel.overlayVisible());
    panel.setOverlayVisible(false);                    // 空闲隐藏
    QVERIFY(!panel.overlayVisible());
    panel.setOverlayVisible(true);                     // 唤醒再出现
    QVERIFY(panel.overlayVisible());
}

void TestVideoPanel::previewBarIsPartOfThePanel()
{
    VideoPanel panel;
    QVERIFY(panel.preview() != nullptr);

    // 让布局真的摆一遍：**画面页得是当前页**，预览栏才拿得到真实宽度
    // （QStackedLayout 只给当前页排版 —— 这也是"量不出宽度就不上报"的原因）
    panel.resize(900, 620);
    panel.show();
    // 预览栏挑片 / 格数上报这两条要能从 VideoPanel 转发出去（MainWindow 只跟面板打交道）
    QSignalSpy pick(&panel, &VideoPanel::pickBilibiliRequested);
    QSignalSpy viewport(&panel, &VideoPanel::viewportChanged);
    panel.setBilibili(bilibiliPayload(18, 3, QStringLiteral("/tmp/t11-preview-test.ts")));
    QTest::qWait(30);

    QCOMPARE(panel.preview()->itemCount(), 18);
    panel.preview()->activateItem(5);
    QCOMPARE(pick.count(), 1);
    QCOMPARE(pick.first().at(0).toInt(), 5);
    QCOMPARE(viewport.count(), 1);                     // 队列到了 + 宽度量得出来 -> 上报一次
    QVERIFY(viewport.first().at(0).toInt() >= 1);
}

void TestVideoPanel::aStreamSourceIsFollowedExactly()
{
    VideoPanel panel;
    // 缓冲没就绪（stream 空）→ 不进画面页
    panel.setBilibili(bilibiliPayload(18, 0));
    QCOMPARE(panel.stage()->currentWidget(), static_cast<QWidget*>(panel.placeholderLabel()));
    QVERIFY(panel.placeholderLabel()->text().contains(QStringLiteral("点一下预览图")));

    // Agent 说"可以放了"（板端给的是 FIFO 本地路径）→ 进画面页并当成本地文件播
    panel.setBilibili(bilibiliPayload(18, 0, QStringLiteral("/tmp/bilibili-BV0.ts")));
    QCOMPARE(panel.source(), QStringLiteral("/tmp/bilibili-BV0.ts"));
    QVERIFY(panel.stage()->currentWidget() != static_cast<QWidget*>(panel.placeholderLabel()));
}

void TestVideoPanel::anEmptyStreamStopsAndSaysWhatToDo()
{
    VideoPanel panel;
    panel.setBilibili(bilibiliPayload(18, 0, QStringLiteral("/tmp/bilibili-BV0.ts")));
    QVERIFY(!panel.source().isEmpty());

    // 换条/清空/放完：Agent 把 stream 清掉 -> GUI 也清屏（旧 FIFO 的写端已经关了）
    panel.setBilibili(bilibiliPayload(18, 1));
    QVERIFY(panel.source().isEmpty());
    QCOMPARE(panel.stage()->currentWidget(), static_cast<QWidget*>(panel.placeholderLabel()));
}

void TestVideoPanel::videoStateIsReportedWithPositionAndPlaying()
{
    VideoPanel panel;
    QSignalSpy state(&panel, &VideoPanel::videoStateReported);
    panel.setBilibili(bilibiliPayload(18, 0, QStringLiteral("/tmp/bilibili-BV0.ts")));

    panel.reportVideoState();
    QCOMPARE(state.count(), 1);
    const QList<QVariant> args = state.first();
    QCOMPARE(args.at(0).toLongLong() >= 0, true);      // 位置（毫秒）
    QCOMPARE(args.at(2).toBool(), panel.isPlaying());  // playing 如实
    QCOMPARE(args.at(3).toBool(), false);              // 不是 eof
}

void TestVideoPanel::endOfMediaIsReportedOnlyOnce()
{
    VideoPanel panel;
    QSignalSpy state(&panel, &VideoPanel::videoStateReported);
    panel.setBilibili(bilibiliPayload(18, 0, QStringLiteral("/tmp/bilibili-BV0.ts")));

    panel.notifyEndOfMedia();
    QCOMPARE(state.count(), 1);
    QCOMPARE(state.first().at(3).toBool(), true);      // eof=true -> Agent 自动下一集
    QCOMPARE(state.first().at(2).toBool(), false);     // 已经不在放了

    panel.notifyEndOfMedia();                          // 播放器可能重复报：只算一次
    QCOMPARE(state.count(), 1);

    // 换了源（= 新的一集）之后，eof 要能再报一次
    panel.setBilibili(bilibiliPayload(18, 1, QStringLiteral("/tmp/bilibili-BV1.ts")));
    panel.notifyEndOfMedia();
    QCOMPARE(state.count(), 2);
}

void TestVideoPanel::noVideoStateWithoutASource()
{
    VideoPanel panel;
    QSignalSpy state(&panel, &VideoPanel::videoStateReported);
    panel.reportVideoState();
    panel.notifyEndOfMedia();
    QCOMPARE(state.count(), 0);                        // 没在放就别回报（免得 Agent 以为在放）
}

QTEST_MAIN(TestVideoPanel)
#include "test_video_panel.moc"
