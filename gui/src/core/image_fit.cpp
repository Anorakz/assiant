// ============================================================================
//  gui/src/core/image_fit.cpp — 等比填满的裁剪几何
// ============================================================================
#include "core/image_fit.h"

#include <QtGlobal>

namespace core {

QRect centeredCropRect(const QSize& source, const QSize& target)
{
    if (source.width() <= 0 || source.height() <= 0 || target.width() <= 0
        || target.height() <= 0) {
        return QRect();
    }

    const double srcAspect = double(source.width()) / double(source.height());
    const double dstAspect = double(target.width()) / double(target.height());

    int w = source.width();
    int h = source.height();
    if (srcAspect > dstAspect) {
        w = qRound(source.height() * dstAspect);   // 源更宽 → 裁左右
    } else if (srcAspect < dstAspect) {
        h = qRound(source.width() / dstAspect);    // 源更高 → 裁上下
    }
    w = qBound(1, w, source.width());
    h = qBound(1, h, source.height());
    return QRect((source.width() - w) / 2, (source.height() - h) / 2, w, h);
}

} // namespace core
