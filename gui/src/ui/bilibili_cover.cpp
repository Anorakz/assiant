// ============================================================================
//  gui/src/ui/bilibili_cover.cpp — 下区域封面实现（T11-7）
// ============================================================================
#include "ui/bilibili_cover.h"

#include "core/bilibili_format.h"
#include "ui/cover_loader.h"

#include <QFontMetrics>
#include <QEvent>
#include <QHBoxLayout>
#include <QLabel>
#include <QResizeEvent>
#include <QStringList>
#include <QVBoxLayout>

namespace {

/// 封面图在下区域里的尺寸（16:9；下区域内容高 ~170px，放得下）
constexpr int kCoverW = 176;
constexpr int kCoverH = 99;

/// 给这几个标签"宽度听布局的"：不许它们按文字长度去要宽度（见头文件里那条实测）
void letTheLayoutDecideWidth(QLabel* label)
{
    label->setMinimumWidth(0);
    label->setSizePolicy(QSizePolicy::Ignored, QSizePolicy::Preferred);
}

} // namespace

QString BilibiliCover::elideFor(const QLabel* label, const QString& text)
{
    if (label == nullptr || text.isEmpty()) {
        return text;
    }
    const int width = label->width();
    if (width <= 0) {
        return text;                  // 还没排版：先原样显示，resizeEvent 里再截
    }
    return QFontMetrics(label->font()).elidedText(text, Qt::ElideRight, width);
}

BilibiliCover::BilibiliCover(QWidget* parent)
    : QWidget(parent)
{
    setObjectName(QStringLiteral("BilibiliCover"));

    auto* root = new QHBoxLayout(this);
    root->setContentsMargins(0, 0, 0, 0);
    root->setSpacing(14);

    cover_ = new QLabel(this);
    cover_->setObjectName(QStringLiteral("BilibiliCoverImage"));
    cover_->setFixedSize(kCoverW, kCoverH);
    cover_->setAlignment(Qt::AlignCenter);
    cover_->setText(QStringLiteral("封面"));
    root->addWidget(cover_, 0, Qt::AlignVCenter);

    auto* column = new QVBoxLayout();
    column->setContentsMargins(0, 0, 0, 0);
    column->setSpacing(4);

    title_ = new QLabel(this);
    title_->setObjectName(QStringLiteral("BilibiliCoverTitle"));
    meta_ = new QLabel(this);
    meta_->setObjectName(QStringLiteral("BilibiliCoverMeta"));
    position_ = new QLabel(this);
    position_->setObjectName(QStringLiteral("BilibiliCoverPosition"));
    source_ = new QLabel(this);
    source_->setObjectName(QStringLiteral("BilibiliCoverSource"));
    for (QLabel* label : {title_, meta_, position_, source_}) {
        letTheLayoutDecideWidth(label);
        label->installEventFilter(this);      // 布局给宽度时走标签自己的 resize
    }

    column->addWidget(title_);
    column->addWidget(meta_);
    column->addWidget(position_);
    column->addWidget(source_);
    column->addStretch(1);
    root->addLayout(column, 1);

    showPlaceholder(QStringLiteral("B 站视频未接入（游戏模式里跟我说一个视频名，或等画面认出游戏）"));
}

void BilibiliCover::setCoverLoader(CoverLoader* loader)
{
    if (loader_ == loader) {
        return;
    }
    if (loader_ != nullptr) {
        disconnect(loader_, nullptr, this, nullptr);
    }
    loader_ = loader;
    if (loader_ == nullptr) {
        return;
    }
    connect(loader_, &CoverLoader::coverReady, this,
            [this](const QString& key, const QPixmap& cover) {
                if (key != bvid_ || cover.isNull()) {
                    return;           // 换条了 / 不是这一张
                }
                cover_->setPixmap(cover.scaled(cover_->size(), Qt::KeepAspectRatio,
                                               Qt::SmoothTransformation));
            });
    connect(loader_, &CoverLoader::coverFailed, this, [this](const QString& key) {
        if (key != bvid_) {
            return;
        }
        cover_->setPixmap(QPixmap());           // 有 pixmap 时 QLabel 不显示文字，先清掉
        cover_->setText(QStringLiteral("封面没下来"));
    });
}

void BilibiliCover::setData(const QJsonObject& data)
{
    const QJsonObject current = data.value(QStringLiteral("current")).toObject();
    const QString bvid = current.value(QStringLiteral("bvid")).toString();
    if (bvid.isEmpty()) {
        hasVideo_ = false;
        bvid_.clear();
        fullTitle_.clear();
        fullMeta_.clear();
        fullPosition_.clear();
        fullSource_.clear();
        showPlaceholder(QStringLiteral("B站视频未接入（游戏模式里跟我说一个视频名，或等画面认出游戏）"));
        return;
    }

    hasVideo_ = true;
    const bool changed = (bvid != bvid_);
    bvid_ = bvid;

    const QString title = current.value(QStringLiteral("title")).toString().trimmed();
    fullTitle_ = title.isEmpty() ? bvid : title;

    const QString author = current.value(QStringLiteral("author")).toString().trimmed();
    const int durationS = current.value(QStringLiteral("duration_s")).toInt(0);
    const qint64 play = static_cast<qint64>(current.value(QStringLiteral("play")).toDouble());
    QStringList bits;
    if (!author.isEmpty()) {
        bits << author;
    }
    const QString meta = bilibili::previewMeta(durationS, play);
    if (!meta.isEmpty()) {
        bits << meta;
    }
    fullMeta_ = bits.join(QStringLiteral(" · "));

    const int index = data.value(QStringLiteral("index")).toInt(0);
    const int count = data.value(QStringLiteral("count")).toInt(0);
    const int target = data.value(QStringLiteral("target")).toInt(0);
    fullPosition_ = (count > 0)
                        ? QStringLiteral("第 %1 / %2 条（队列目标 %3 条）")
                              .arg(index + 1)
                              .arg(count)
                              .arg(target)
                        : QString();

    const QString keyword = data.value(QStringLiteral("keyword")).toString().trimmed();
    const QString source = data.value(QStringLiteral("source")).toString().trimmed();
    if (!keyword.isEmpty()) {
        fullSource_ = (source == QLatin1String("screen"))
                          ? QStringLiteral("画面认出：%1").arg(keyword)
                          : QStringLiteral("你说的：%1").arg(keyword);
    } else {
        fullSource_.clear();
    }

    title_->setToolTip(fullTitle_);
    meta_->setToolTip(fullMeta_);
    repositionText();

    if (changed) {
        // 换条了：先把旧图清掉（免得新条的图还没下来时挂着上一张，看着像"放错了"）
        cover_->setPixmap(QPixmap());
        cover_->setText(QStringLiteral("封面加载中…"));
        if (loader_ != nullptr) {
            loader_->request(bvid, current.value(QStringLiteral("cover")).toString());
        }
    }
    if (loader_ == nullptr) {
        cover_->setPixmap(QPixmap());
        cover_->setText(QStringLiteral("封面"));
    }
}

void BilibiliCover::repositionText()
{
    if (repositioning_) {
        return;                       // 防自己叫自己（改文本 -> resize -> 又进来）
    }
    repositioning_ = true;
    title_->setText(elideFor(title_, fullTitle_));
    meta_->setText(elideFor(meta_, fullMeta_));
    position_->setText(elideFor(position_, fullPosition_));
    source_->setText(elideFor(source_, fullSource_));
    repositioning_ = false;
}

void BilibiliCover::resizeEvent(QResizeEvent* event)
{
    QWidget::resizeEvent(event);
    repositionText();       // 宽度变了 -> 重新按宽度截断
}

bool BilibiliCover::eventFilter(QObject* watched, QEvent* event)
{
    if (event->type() == QEvent::Resize && watched != this) {
        repositionText();
    }
    return QWidget::eventFilter(watched, event);
}

void BilibiliCover::showPlaceholder(const QString& text)
{
    cover_->setPixmap(QPixmap());
    cover_->setText(QStringLiteral("封面"));
    title_->setText(elideFor(title_, text));
    title_->setToolTip(text);
    meta_->setText(QString());
    position_->setText(QString());
    source_->setText(QString());
}
