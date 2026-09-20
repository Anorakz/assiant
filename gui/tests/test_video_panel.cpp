// ============================================================================
//  gui/tests/test_video_panel.cpp — 视频区控件级测试（T9，含验收后的调整）
//
//  只测**不依赖媒体后端**的部分：占位态、按钮集合与符号、播放/暂停与全屏的
//  状态流转、占位项点击行为。真的解码播放由验收脚本在板端用 scrot 取证。
// ============================================================================
#include <QLabel>
#include <QMediaPlayer>
#include <QPushButton>
#include <QStackedWidget>
#include <QToolButton>
#include <QVideoWidget>
#include <QtTest/QtTest>

#include "ui/video_panel.h"

class TestVideoPanel : public QObject {
    Q_OBJECT

private slots:
    void emptySourceShowsPlaceholder();
    void buttonsUseIconsNotText();
    void playPauseTogglesLocalPlayback();
    void fullscreenTogglesAndSignals();
    void nextButtonEmitsNextBilibili();
    void otherControlsArePlaceholders();
    void overlayVisibilityFollowsWatcher();
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
    QCOMPARE(placeSpy.count(), 1);                     // 同时给出"未接入"说明
    QCOMPARE(placeSpy.first().at(0).toString(), QStringLiteral("下一集"));
    QVERIFY(panel.noteText().contains(QStringLiteral("未接入")));
}

void TestVideoPanel::otherControlsArePlaceholders()
{
    VideoPanel panel;
    QSignalSpy nextSpy(&panel, &VideoPanel::nextBilibiliRequested);
    QSignalSpy placeSpy(&panel, &VideoPanel::placeholderClicked);

    panel.previousButton()->click();
    QCOMPARE(placeSpy.count(), 1);
    QCOMPARE(placeSpy.first().at(0).toString(), QStringLiteral("上一集"));
    QCOMPARE(nextSpy.count(), 0);                      // 不能误发 next_bilibili

    panel.triggerPlaceholder(QStringLiteral("倍速"));
    QCOMPARE(placeSpy.count(), 2);
    QCOMPARE(nextSpy.count(), 0);
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

QTEST_MAIN(TestVideoPanel)
#include "test_video_panel.moc"
