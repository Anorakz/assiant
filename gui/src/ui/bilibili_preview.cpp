// ============================================================================
//  gui/src/ui/bilibili_preview.cpp — 预览栏实现（T11-7）
// ============================================================================
#include "ui/bilibili_preview.h"

#include "core/bilibili_format.h"
#include "ui/cover_loader.h"

#include <QDebug>
#include <QHBoxLayout>
#include <QIcon>
#include <QJsonArray>
#include <QJsonValue>
#include <QLabel>
#include <QLineEdit>
#include <QListWidget>
#include <QResizeEvent>
#include <QStackedWidget>
#include <QVBoxLayout>

namespace {

/// 一格缩略图的图标尺寸（16:9 —— B 站封面就是这个比例）
constexpr int kIconW = 112;
constexpr int kIconH = 63;
/// 一格（图标 + 两行字）的格子尺寸
constexpr int kCellW = 120;
constexpr int kCellH = 96;
/// 两格之间的间隔
constexpr int kSpacing = 4;
/// 地址栏那一行的高度
constexpr int kAddressRowH = 26;
/// 整条预览栏的高度（地址行 + 间距 + 列表）
constexpr int kBarH = kAddressRowH + 6 + kCellH + 8;

/// 队列为空时的灰字说明（`why` 有内容时用它 —— 失败要如实说）
QString emptyHint(const QJsonObject& data)
{
    const QString why = data.value(QStringLiteral("why")).toString().trimmed();
    if (!why.isEmpty()) {
        return QStringLiteral("没搜到能用的：%1").arg(why);
    }
    return QStringLiteral("队列是空的 —— 在 GAME 模式里跟我说一个视频名（或等画面认出游戏）");
}

/// 「对话：xxx」/「画面：xxx」/ 空
QString sourceText(const QJsonObject& data)
{
    const QString keyword = data.value(QStringLiteral("keyword")).toString().trimmed();
    const QString source = data.value(QStringLiteral("source")).toString().trimmed();
    if (keyword.isEmpty()) {
        return QString();
    }
    if (source == QLatin1String("screen")) {
        return QStringLiteral("画面：%1").arg(keyword);
    }
    return QStringLiteral("对话：%1").arg(keyword);
}

} // namespace

BilibiliPreview::BilibiliPreview(QWidget* parent)
    : QWidget(parent)
{
    setObjectName(QStringLiteral("BilibiliPreview"));

    auto* root = new QVBoxLayout(this);
    root->setContentsMargins(0, 0, 0, 0);
    root->setSpacing(6);

    // ---- 行 1：地址栏（只读）+ 队列来源 ----
    auto* row = new QWidget(this);
    auto* rowBox = new QHBoxLayout(row);
    rowBox->setContentsMargins(0, 0, 0, 0);
    rowBox->setSpacing(8);

    addressCaption_ = new QLabel(QStringLiteral("地址"), row);
    addressCaption_->setObjectName(QStringLiteral("BilibiliCaption"));
    address_ = new QLineEdit(row);
    address_->setObjectName(QStringLiteral("BilibiliAddress"));
    address_->setReadOnly(true);                  // 你定的：GUI 自己不能搜
    address_->setPlaceholderText(QStringLiteral("（还没有队列）"));
    address_->setCursorPosition(0);
    source_ = new QLabel(row);
    source_->setObjectName(QStringLiteral("BilibiliSource"));

    rowBox->addWidget(addressCaption_);
    rowBox->addWidget(address_, 1);
    rowBox->addWidget(source_);
    row->setFixedHeight(kAddressRowH);
    root->addWidget(row);

    // ---- 行 2：空队列说明 / 预览列表 ----
    pages_ = new QStackedWidget(this);
    pages_->setObjectName(QStringLiteral("BilibiliPages"));
    // 同上：这一层也不能"按内容要宽度"，宽度一律听布局的
    pages_->setMinimumWidth(0);
    pages_->setSizePolicy(QSizePolicy::Ignored, QSizePolicy::Preferred);

    hint_ = new QLabel(emptyHint(QJsonObject()), pages_);
    hint_->setObjectName(QStringLiteral("BilibiliHint"));
    hint_->setAlignment(Qt::AlignLeft | Qt::AlignVCenter);
    hint_->setWordWrap(true);
    pages_->addWidget(hint_);

    list_ = new QListWidget(pages_);
    list_->setObjectName(QStringLiteral("BilibiliList"));
    // 一排、不换行：这就是"预览栏"（N 格 = 一屏放得下的格数）
    list_->setViewMode(QListView::IconMode);
    list_->setFlow(QListView::LeftToRight);
    list_->setWrapping(false);
    list_->setResizeMode(QListView::Adjust);
    list_->setMovement(QListView::Static);
    list_->setUniformItemSizes(true);
    list_->setIconSize(QSize(kIconW, kIconH));
    list_->setGridSize(QSize(kCellW, kCellH));
    list_->setSpacing(kSpacing);
    list_->setWordWrap(true);
    // 滚动条**不显示**但程序化滚动照样有效：用户走位靠「上一集/下一集」（Agent 那边滑窗口），
    // 手搓滚动条会把"看得见的这 N 格"和 Agent 的窗口算岔。
    list_->setHorizontalScrollBarPolicy(Qt::ScrollBarAlwaysOff);
    list_->setVerticalScrollBarPolicy(Qt::ScrollBarAlwaysOff);
    list_->setSelectionMode(QAbstractItemView::SingleSelection);
    list_->setEditTriggers(QAbstractItemView::NoEditTriggers);
    list_->setFocusPolicy(Qt::NoFocus);       // 别抢键盘焦点（软键盘盯着焦点那套）
    // ⚠ 板端实测踩到的坑：**不换行 + IconMode** 的 QListWidget 会按"所有格子排成一行"
    //   去要宽度 —— 封面一张张到齐之后，它的最小宽度越涨越大，最后把整个主区
    //   顶出窗口（右区域的地址栏/封面都被挤出屏幕）。所以：**宽度只由布局说了算**。
    list_->setMinimumWidth(0);
    list_->setSizeAdjustPolicy(QAbstractScrollArea::AdjustIgnored);
    list_->setHorizontalScrollMode(QAbstractItemView::ScrollPerPixel);
    pages_->addWidget(list_);
    pages_->setCurrentWidget(hint_);
    root->addWidget(pages_, 1);

    setFixedHeight(kBarH);
    // 列表可见区的每一次 resize 都要看一眼"现在能放几格"（见 eventFilter 的注释）
    list_->viewport()->installEventFilter(this);

    // 只有**真点击**才挑片：代码移动高亮（setCurrentRow）不发这个信号
    connect(list_, &QListWidget::itemClicked, this, [this](QListWidgetItem* item) {
        if (item == nullptr) {
            return;
        }
        activateItem(item->data(Qt::UserRole).toInt());
    });
}

int BilibiliPreview::activateItem(int index)
{
    if (index < 0 || index >= list_->count()) {
        qWarning().noquote() << QStringLiteral("[bilibili] 预览栏没有第 %1 格（共 %2 格）")
                                    .arg(index)
                                    .arg(list_->count());
        return -1;
    }
    QListWidgetItem* item = list_->item(index);
    if (item == nullptr) {
        return -1;
    }
    qInfo().noquote() << QStringLiteral("[bilibili] 点了预览图第 %1 格（bvid=%2）")
                             .arg(index)
                             .arg(item->data(Qt::UserRole + 1).toString());
    emit pickRequested(index);
    return index;
}

int BilibiliPreview::cellWidth()
{
    return kCellW + kSpacing;
}

int BilibiliPreview::itemCount() const
{
    return (list_ != nullptr) ? list_->count() : 0;
}

int BilibiliPreview::cellHeight()
{
    return kCellH;
}

int BilibiliPreview::barHeight()
{
    return kBarH;
}

void BilibiliPreview::setCoverLoader(CoverLoader* loader)
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
                QListWidgetItem* item = itemFor(key);
                if (item == nullptr) {
                    return;      // 这一格已经滑出窗口了 —— 正常，不报错
                }
                item->setIcon(QIcon(cover));
            });
    requestCoversAround(index_);           // 已经有队列的话，补一轮图
}

void BilibiliPreview::setData(const QJsonObject& data)
{
    data_ = data;

    const QJsonArray queue = data.value(QStringLiteral("queue")).toArray();
    QStringList bvids;
    bvids.reserve(queue.size());
    for (const QJsonValue& value : queue) {
        const QJsonObject entry = value.toObject();
        const QString bvid = entry.value(QStringLiteral("bvid")).toString();
        if (!bvid.isEmpty()) {
            bvids.append(bvid);
        }
    }
    int index = data.value(QStringLiteral("index")).toInt(0);
    if (index < 0) {
        index = 0;
    }
    if (index >= bvids.size()) {
        index = bvids.isEmpty() ? 0 : bvids.size() - 1;
    }

    if (bvids != bvids_) {
        rebuild(bvids, index);
    } else {
        updateHighlight(index);
    }
    if (bvids.isEmpty()) {
        pages_->setCurrentWidget(hint_);
        hint_->setText(emptyHint(data));
    } else {
        pages_->setCurrentWidget(list_);
    }

    const QJsonObject current = data.value(QStringLiteral("current")).toObject();
    const QString bvid = current.value(QStringLiteral("bvid")).toString();
    const QString url = current.value(QStringLiteral("url")).toString();
    updateAddress(bvid.isEmpty() && !bvids.isEmpty() ? bvids.at(index) : bvid, url);
    updateSourceText(data);
    maybeReportViewport();
    requestCoversAround(index_);
}

void BilibiliPreview::rebuild(const QStringList& bvids, int index)
{
    bvids_ = bvids;
    items_.clear();
    list_->clear();

    const QJsonArray queue = data_.value(QStringLiteral("queue")).toArray();
    // 逐条建格子：文字 = 标题 + (时长 · 播放量)，tooltip 里给作者与 bvid
    for (int i = 0; i < queue.size(); ++i) {
        const QJsonObject entry = queue.at(i).toObject();
        const QString bvid = entry.value(QStringLiteral("bvid")).toString();
        if (bvid.isEmpty()) {
            continue;
        }
        auto* item = new QListWidgetItem(list_);
        const QString title = entry.value(QStringLiteral("title")).toString().trimmed();
        const int durationS = entry.value(QStringLiteral("duration_s")).toInt(0);
        const qint64 play = static_cast<qint64>(entry.value(QStringLiteral("play")).toDouble());
        const QString meta = bilibili::previewMeta(durationS, play);
        item->setText(meta.isEmpty() ? title : QStringLiteral("%1\n%2").arg(title, meta));
        const QString author = entry.value(QStringLiteral("author")).toString().trimmed();
        item->setToolTip(QStringLiteral("%1\n%2\n%3")
                             .arg(title.isEmpty() ? bvid : title,
                                  author.isEmpty() ? QStringLiteral("（作者未知）") : author,
                                  bilibili::videoUrl(bvid)));
        item->setData(Qt::UserRole, i);
        item->setData(Qt::UserRole + 1, bvid);
        items_.insert(bvid, item);
    }
    updateHighlight(index);
}

void BilibiliPreview::updateHighlight(int index)
{
    index_ = index;
    currentBvid_ = (index >= 0 && index < bvids_.size()) ? bvids_.at(index) : QString();
    if (list_->count() == 0) {
        return;
    }
    list_->setCurrentRow(index);
    if (QListWidgetItem* item = list_->item(index)) {
        list_->scrollToItem(item, QAbstractItemView::PositionAtCenter);
    }
}

void BilibiliPreview::updateAddress(const QString& bvid, const QString& url)
{
    QString text = url.trimmed();
    if (text.isEmpty()) {
        text = bilibili::videoUrl(bvid);
    }
    if (address_->text() != text) {
        address_->setText(text);
        address_->setCursorPosition(0);      // 让人先看见域名与 BV 号，不是尾巴
    }
}

void BilibiliPreview::updateSourceText(const QJsonObject& data)
{
    QString text = sourceText(data);
    if (text.isEmpty() && bvids_.isEmpty()) {
        text = QString();                       // 队列都没有的时候不写"来源"这一栏
    } else if (text.isEmpty()) {
        text = QStringLiteral("来源未知");        // 有队列却没说来源：如实写"未知"
    }
    if (source_->text() != text) {
        source_->setText(text);
    }
}

int BilibiliPreview::visibleCells() const
{
    const int width = (list_ != nullptr) ? list_->viewport()->width() : 0;
    return bilibili::visibleCells(width, kCellW, kSpacing);
}

void BilibiliPreview::maybeReportViewport()
{
    // ⚠ 只有**手里真有队列**时才上报：队列是空的时候上报等于给 Agent 找事
    //   （B 站没开时还会白弹一句"B 站视频还没开"的气泡）。
    if (bvids_.isEmpty()) {
        return;
    }
    // ⚠ 布局还没算出来时可见区只有几十像素 —— 那时上报 1 格会把队列目标压成 3 条。
    //   宁可先不上报：等布局给了真实宽度（`list_->viewport()` 的 resize 会再叫我们）。
    const int width = list_->viewport()->width();
    if (width < kCellW) {
        qInfo().noquote()
            << QStringLiteral("[bilibili] 预览栏还没量出宽度（%1 px < %2）—— 先不上报格数")
                   .arg(width)
                   .arg(kCellW);
        return;
    }
    const int cells = visibleCells();
    if (cells == reported_) {
        return;
    }
    reported_ = cells;
    qInfo().noquote() << QStringLiteral("[bilibili] 预览栏能放 %1 格 -> 上报 bilibili_viewport")
                             .arg(cells);
    emit viewportChanged(cells);
}

void BilibiliPreview::requestCoversAround(int index)
{
    if (loader_ == nullptr || bvids_.isEmpty()) {
        return;
    }
    const QJsonArray queue = data_.value(QStringLiteral("queue")).toArray();
    // 只取**当前附近**的那些格（一屏放不下的先不取）：十几张图已经够填满看得见的区域，
    // 又不会因为窗口有 60 条就一口气发 60 个请求。
    const int span = qMax(1, visibleCells());
    const int from = qMax(0, index - span);
    const int to = qMin(queue.size() - 1, index + span);
    for (int i = from; i <= to; ++i) {
        const QJsonObject entry = queue.at(i).toObject();
        const QString bvid = entry.value(QStringLiteral("bvid")).toString();
        if (bvid.isEmpty()) {
            continue;
        }
        loader_->request(bvid, entry.value(QStringLiteral("cover")).toString());
    }
}

QListWidgetItem* BilibiliPreview::itemFor(const QString& bvid) const
{
    return items_.value(bvid, nullptr);
}

void BilibiliPreview::resizeEvent(QResizeEvent* event)
{
    QWidget::resizeEvent(event);
    maybeReportViewport();
}

bool BilibiliPreview::eventFilter(QObject* watched, QEvent* event)
{
    if (watched == list_->viewport() && event->type() == QEvent::Resize) {
        maybeReportViewport();
        requestCoversAround(index_);        // 变宽了可能露出新格子，顺手补图
    }
    return QWidget::eventFilter(watched, event);
}

void BilibiliPreview::showEvent(QShowEvent* event)
{
    QWidget::showEvent(event);
    maybeReportViewport();
}
