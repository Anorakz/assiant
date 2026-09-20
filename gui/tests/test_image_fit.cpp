// ============================================================================
//  gui/tests/test_image_fit.cpp — 壁纸"等比填满"裁剪几何单测（T8）
// ============================================================================
#include <QtTest/QtTest>

#include "core/image_fit.h"

using core::centeredCropRect;

class TestImageFit : public QObject {
    Q_OBJECT

private slots:
    void sameAspectIsIdentity();
    void wideTargetCropsTopAndBottom();
    void squareTargetCropsSides();
    void degenerateInputsAreEmpty();
    void cropAlwaysMatchesTargetAspect();
    void thinSourceIsClampedNotSubPixel();
};

void TestImageFit::sameAspectIsIdentity()
{
    // 完全一样 → 不裁
    QCOMPARE(centeredCropRect(QSize(1280, 800), QSize(1280, 800)),
             QRect(0, 0, 1280, 800));
    // 等比放大到一半 → 仍然不裁
    QCOMPARE(centeredCropRect(QSize(1280, 800), QSize(640, 400)),
             QRect(0, 0, 1280, 800));
}

void TestImageFit::wideTargetCropsTopAndBottom()
{
    // 256×256 的图铺 1280×800（更宽）→ 上下各裁 48，保留 256×160
    const QRect r = centeredCropRect(QSize(256, 256), QSize(1280, 800));
    QCOMPARE(r, QRect(0, 48, 256, 160));
    QVERIFY(r.height() < 256);
    QCOMPARE(r.x(), 0);
}

void TestImageFit::squareTargetCropsSides()
{
    // 1280×800 的图铺 400×400（正方）→ 左右各裁 240，保留 800×800
    const QRect r = centeredCropRect(QSize(1280, 800), QSize(400, 400));
    QCOMPARE(r, QRect(240, 0, 800, 800));
    QVERIFY(r.width() < 1280);
    QCOMPARE(r.y(), 0);
}

void TestImageFit::degenerateInputsAreEmpty()
{
    QVERIFY(centeredCropRect(QSize(0, 100), QSize(100, 100)).isEmpty());
    QVERIFY(centeredCropRect(QSize(100, 0), QSize(100, 100)).isEmpty());
    QVERIFY(centeredCropRect(QSize(100, 100), QSize(0, 0)).isEmpty());
    QVERIFY(centeredCropRect(QSize(-5, 10), QSize(100, 100)).isEmpty());
}

void TestImageFit::cropAlwaysMatchesTargetAspect()
{
    // 各种**常规尺寸**组合下：裁出来的宽高比都应≈目标宽高比，且不超出源图
    const QList<QSize> sources = {QSize(1920, 1080), QSize(800, 600), QSize(256, 256),
                                  QSize(300, 1000)};
    const QList<QSize> targets = {QSize(1280, 800), QSize(400, 400), QSize(100, 900),
                                 QSize(1, 1)};
    for (const QSize& src : sources) {
        for (const QSize& dst : targets) {
            const QRect r = centeredCropRect(src, dst);
            QVERIFY2(!r.isEmpty(), qPrintable(QStringLiteral("%1x%2 -> %3x%4")
                                                  .arg(src.width()).arg(src.height())
                                                  .arg(dst.width()).arg(dst.height())));
            QVERIFY(r.width() <= src.width());
            QVERIFY(r.height() <= src.height());
            QVERIFY(r.x() >= 0 && r.y() >= 0);
            const double srcAspect = double(r.width()) / double(r.height());
            const double dstAspect = double(dst.width()) / double(dst.height());
            // 取整会带来一点误差（短边 1 像素量级），宽容到 2%
            QVERIFY2(qAbs(srcAspect - dstAspect) / dstAspect < 0.02,
                     qPrintable(QStringLiteral("裁切后宽高比偏离: %1 vs %2 (裁切 %3x%4)")
                                    .arg(srcAspect, 0, 'f', 4)
                                    .arg(dstAspect, 0, 'f', 4)
                                    .arg(r.width())
                                    .arg(r.height())));
        }
    }
}

void TestImageFit::thinSourceIsClampedNotSubPixel()
{
    // 极端长宽比（1000×3 填 100×900）：理想裁切宽度只有 0.33px，**像素级不可分**。
    // 这里不假装"宽高比正确"，只固定行为：宽度被夹到 1px、结果非空、绘制时会被拉伸。
    const QRect r = centeredCropRect(QSize(1000, 3), QSize(100, 900));
    QCOMPARE(r.width(), 1);
    QCOMPARE(r.height(), 3);
    QVERIFY(!r.isEmpty());

    // 反过来（3×1000 填 900×100）同理，夹的是高度
    const QRect r2 = centeredCropRect(QSize(3, 1000), QSize(900, 100));
    QCOMPARE(r2.height(), 1);
    QCOMPARE(r2.width(), 3);
}

QTEST_APPLESS_MAIN(TestImageFit)
#include "test_image_fit.moc"
