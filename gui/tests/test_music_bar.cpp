// ============================================================================
//  gui/tests/test_music_bar.cpp — 音乐条"别白写样式"的守卫（T15-16 / G-B-1）
//
//  为什么用**计数**而不是 CPU 数字
//  ---------------------------------------------------------------------------
//  G-B-1 省掉的是"每秒几次 setStyleSheet"（样式重算 + 重绘 ✗）—— 在 1280×800 上那是**极小**的开销 ✓，
//  用 CPU% 很可能**落在噪声里** ✗（板端基线 1.42%~1.50% ✓，波动就有 ±0.1 ✓）。
//  而且"改前的计数"根本不存在 ✗（打点是 G-B-1 才加的 ✓）。
//  ⇒ 所以主判据是**这个计数** ✓：同一状态连调两次，`styleWrites()` **不该增长** ✓；
//    牙齿演示：把 `applyStyle` 里的缓存判断注掉 ⇒ 本用例**必红** ✓。
// ============================================================================
#include <QLabel>
#include <QProgressBar>
#include <QtTest/QtTest>

#include "ui/music_bar.h"

class TestMusicBar : public QObject {
    Q_OBJECT

private slots:
    /// 进度状态没变 ⇒ 一次样式都不许再写 ✓（位置在变不算"状态变" ✓）
    void progressStyleIsOnlyWrittenWhenTheStateChanges()
    {
        MusicBar bar;
        bar.setProgress(12.0, 180.0);          // 有进度：样式串 = QString()（清空内联样式 ✓）
        const int afterFirst = bar.styleWrites();

        bar.setProgress(13.0, 180.0);          // 只是位置前进 ⇒ 状态没变 ⇒ 不该再写 ✓
        bar.setProgress(14.0, 180.0);
        QCOMPARE(bar.styleWrites(), afterFirst);

        bar.setProgress(0.0, 0.0);             // 变成"没有进度" ⇒ 状态变了 ⇒ **要**写一次 ✓
        QCOMPARE(bar.styleWrites(), afterFirst + 1);

        bar.setProgress(0.0, 0.0);             // 同一状态repeat ⇒ 不再写 ✓
        QCOMPARE(bar.styleWrites(), afterFirst + 1);

        bar.setProgress(1.0, 180.0);           // 又有进度了 ⇒ 再写一次 ✓
        QCOMPARE(bar.styleWrites(), afterFirst + 2);
    }

    /// 歌词侧：同一状态重复刷新 ⇒ 不增长 ✓（`setLyricsProvider` 会走 refreshLyrics ✓）
    void lyricsStyleIsOnlyWrittenWhenTheStateChanges()
    {
        MusicBar bar;
        bar.setLyricsProvider(nullptr);        // "歌词未接入"分支 ✓
        const int after = bar.styleWrites();
        bar.setLyricsProvider(nullptr);
        bar.setLyricsProvider(nullptr);
        QCOMPARE(bar.styleWrites(), after);    // 状态没变 ⇒ 一次都不该多写 ✓
    }

    /// 计数本身要真的在动（否则上面两条会退化成"永远 0 == 永远 0"的空断言 ✗）
    void theCounterActuallyMoves()
    {
        MusicBar bar;
        const int before = bar.styleWrites();
        bar.setProgress(0.0, 0.0);             // 从"有/无未知"切到"没有进度" ✓
        bar.setProgress(9.0, 180.0);           // 再切回来 ✓
        QVERIFY2(bar.styleWrites() > before,
                 "计数没动 —— 要么优化把状态切换也吞了 ✗，要么计数根本没接上 ✗");
    }
};

QTEST_MAIN(TestMusicBar)
#include "test_music_bar.moc"
