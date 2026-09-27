// ============================================================================
//  gui/src/core/config_store.cpp — **只读**配置视图 + 改动预览（T14-3）
//
//  解析规则（刻意做窄，够用即可，宽了就得上 YAML 库）
//  ---------------------------------------------------------------------------
//    只认 `key: value` 与 `key:`（容器）。忽略空行、以 '#' 开头的整行注释、
//    以及以 '-' 开头的列表项。行尾 `  # 注释` 会被切开并原样保留。
//    嵌套靠**缩进栈**：缩进 <= 栈顶则弹栈，容器键路径 = 栈内键用 '.' 连接。
//
//  ⚠ 这里**没有任何写入**（T14-3 删掉了）：唯一的写入者是 Agent，
//    GUI 只把"想改成什么"通过 IPC 交过去（`set_config` → `config_result`）。
// ============================================================================
#include "core/config_store.h"

#include <QFile>
#include <QFileInfo>

namespace core {

namespace {

/// 取"行尾注释"：返回 (值部分, 注释部分含前导空格)。
///
/// 必须**跳过引号内的 `#`**：`llm.commentish: "a # b"` 里的 `#` 是值的一部分，
/// 按第一次出现 ` #` 就切会把值切成 `"a`（T2 单测抓到的真 bug）。
void splitTrailingComment(const QString& raw, QString* valuePart, QString* commentPart)
{
    bool inSingle = false;
    bool inDouble = false;
    for (int i = 0; i < raw.size(); ++i) {
        const QChar c = raw.at(i);
        if (inDouble && c == QLatin1Char('\\')) {
            ++i;                       // 跳过转义字符
            continue;
        }
        if (c == QLatin1Char('"') && !inSingle) {
            inDouble = !inDouble;
            continue;
        }
        if (c == QLatin1Char('\'') && !inDouble) {
            inSingle = !inSingle;
            continue;
        }
        if (!inSingle && !inDouble && c == QLatin1Char('#') && i > 0
            && raw.at(i - 1) == QLatin1Char(' ')) {
            // ⚠ 把**对齐用的那串空白**一起留给注释：`  mode: x      # 注释` 预览时
            //   注释还要对在原地（早先只留一个空格，看起来像把行改丑了）。
            int start = i - 1;
            while (start > 0 && raw.at(start - 1) == QLatin1Char(' ')) {
                --start;
            }
            *valuePart = raw.left(start);
            *commentPart = raw.mid(start);
            return;
        }
    }
    *valuePart = raw;
    commentPart->clear();
}

} // namespace

int ConfigStore::indentOf(const QString& line)
{
    int n = 0;
    while (n < line.size() && line.at(n) == QLatin1Char(' ')) {
        ++n;
    }
    return n;
}

QString ConfigStore::unquote(const QString& raw)
{
    const QString v = raw.trimmed();
    if (v.size() >= 2) {
        const QChar first = v.at(0);
        const QChar last = v.at(v.size() - 1);
        if (first == QLatin1Char('"') && last == QLatin1Char('"')) {
            QString inner = v.mid(1, v.size() - 2);
            inner.replace(QLatin1String("\\\""), QLatin1String("\""));
            inner.replace(QLatin1String("\\\\"), QLatin1String("\\"));
            return inner;
        }
        if (first == QLatin1Char('\'') && last == QLatin1Char('\'')) {
            return v.mid(1, v.size() - 2);
        }
    }
    return v;
}

QString ConfigStore::encodeScalar(const QString& value)
{
    if (value.isEmpty()) {
        return QStringLiteral("\"\"");
    }
    const QChar c0 = value.at(0);
    const bool needQuote =
        value.contains(QLatin1String(" #")) || value.contains(QLatin1Char('\n'))
        || c0 == QLatin1Char('#') || c0 == QLatin1Char('-') || c0 == QLatin1Char('?')
        || c0 == QLatin1Char(':') || c0 == QLatin1Char('[') || c0 == QLatin1Char('{')
        || c0 == QLatin1Char('&') || c0 == QLatin1Char('*') || c0 == QLatin1Char('!')
        || c0 == QLatin1Char('|') || c0 == QLatin1Char('>') || c0 == QLatin1Char('\'')
        || c0 == QLatin1Char('"') || c0 == QLatin1Char('%') || c0 == QLatin1Char('@')
        || c0 == QLatin1Char('`');
    if (!needQuote) {
        return value;
    }
    QString out = value;
    out.replace(QLatin1String("\\"), QLatin1String("\\\\"));
    out.replace(QLatin1String("\""), QLatin1String("\\\""));
    return QLatin1Char('"') + out + QLatin1Char('"');
}

bool ConfigStore::load(const QString& path, QString* error)
{
    path_ = path;
    loaded_ = false;
    lines_.clear();
    values_.clear();
    lineOf_.clear();
    commentOf_.clear();
    blocks_.clear();
    pendingOrder_.clear();
    pending_.clear();

    QFile file(path);
    exists_ = file.exists();
    if (!exists_) {
        // 不能对不存在的路径"假装加载成功"：调用方会拿满屏默认值当"读到了"。
        if (error) {
            *error = QStringLiteral("文件不存在: %1").arg(path);
        }
        return false;
    }
    if (!file.open(QIODevice::ReadOnly | QIODevice::Text)) {
        if (error) {
            *error = QStringLiteral("打不开 %1: %2").arg(path, file.errorString());
        }
        return false;
    }
    const QString content = QString::fromUtf8(file.readAll());
    file.close();
    lines_ = content.split(QLatin1Char('\n'));
    // 末尾换行的 split 会留一个空元素：去掉它，行号才与编辑器里看到的一致
    if (!lines_.isEmpty() && lines_.last().isEmpty()) {
        lines_.removeLast();
    }
    index();
    loaded_ = true;
    return true;
}

void ConfigStore::index()
{
    values_.clear();
    lineOf_.clear();
    commentOf_.clear();
    blocks_.clear();

    blocks_.insert(QString(), Block{0, lines_.size(), -1});

    struct Frame {
        int indent;
        QString key;
        int line;
    };
    QVector<Frame> stack;

    const auto prefixOf = [&stack]() {
        QStringList parts;
        for (const Frame& f : stack) {
            parts << f.key;
        }
        return parts.join(QLatin1Char('.'));
    };

    for (int i = 0; i < lines_.size(); ++i) {
        const QString line = lines_.at(i);
        const QString trimmed = line.trimmed();
        if (trimmed.isEmpty() || trimmed.startsWith(QLatin1Char('#'))
            || trimmed.startsWith(QLatin1Char('-'))) {
            continue;
        }
        const int colon = trimmed.indexOf(QLatin1Char(':'));
        if (colon <= 0) {
            continue;
        }
        const int indent = indentOf(line);
        const QString key = trimmed.left(colon).trimmed();

        // 弹栈：缩进不比当前行深的容器都结束了
        while (!stack.isEmpty() && stack.last().indent >= indent) {
            const Frame f = stack.takeLast();
            const QString parent = prefixOf();
            const QString full = parent.isEmpty() ? f.key : parent + QLatin1Char('.') + f.key;
            blocks_.insert(full, Block{f.line + 1, i, f.indent});
        }
        const QString parent = prefixOf();
        const QString full = parent.isEmpty() ? key : parent + QLatin1Char('.') + key;

        QString valuePart;
        QString commentPart;
        splitTrailingComment(trimmed.mid(colon + 1), &valuePart, &commentPart);

        if (valuePart.trimmed().isEmpty()) {
            stack.append(Frame{indent, key, i});   // 容器
        } else {
            values_.insert(full, unquote(valuePart));
            lineOf_.insert(full, i);
            commentOf_.insert(full, commentPart);
        }
    }
    while (!stack.isEmpty()) {
        const Frame f = stack.takeLast();
        const QString parent = prefixOf();
        const QString full = parent.isEmpty() ? f.key : parent + QLatin1Char('.') + f.key;
        blocks_.insert(full, Block{f.line + 1, lines_.size(), f.indent});
    }
}

QString ConfigStore::value(const QString& key, const QString& fallback) const
{
    // 未"落盘"的改动**对读取可见**（像编辑器的缓冲区）：
    // 否则界面上刚改的值立刻被别处的重读覆盖回去。
    const auto pendingIt = pending_.constFind(key);
    if (pendingIt != pending_.constEnd()) {
        return pendingIt.value();
    }
    const auto it = values_.constFind(key);
    return it == values_.constEnd() ? fallback : it.value();
}

bool ConfigStore::boolValue(const QString& key, bool fallback) const
{
    if (!contains(key)) {
        return fallback;
    }
    const QString v = value(key).trimmed().toLower();
    if (v == QLatin1String("true") || v == QLatin1String("yes") || v == QLatin1String("on")
        || v == QLatin1String("1")) {
        return true;
    }
    if (v == QLatin1String("false") || v == QLatin1String("no") || v == QLatin1String("off")
        || v == QLatin1String("0")) {
        return false;
    }
    return fallback;
}

int ConfigStore::intValue(const QString& key, int fallback) const
{
    if (!contains(key)) {
        return fallback;
    }
    bool ok = false;
    const int v = value(key).trimmed().toInt(&ok);
    return ok ? v : fallback;
}

double ConfigStore::doubleValue(const QString& key, double fallback) const
{
    if (!contains(key)) {
        return fallback;
    }
    bool ok = false;
    const double v = value(key).trimmed().toDouble(&ok);
    return ok ? v : fallback;
}

void ConfigStore::set(const QString& key, const QString& value)
{
    if (key.isEmpty()) {
        return;
    }
    if (!pending_.contains(key)) {
        pendingOrder_.append(key);
    }
    pending_.insert(key, value);
}

void ConfigStore::setBool(const QString& key, bool value)
{
    set(key, value ? QStringLiteral("true") : QStringLiteral("false"));
}

void ConfigStore::clearPending()
{
    pendingOrder_.clear();
    pending_.clear();
}

bool ConfigStore::planChanges(QVector<LineChange>* out, QString* error) const
{
    out->clear();
    for (const QString& key : pendingOrder_) {
        const QString newValue = pending_.value(key);
        const auto existing = values_.constFind(key);
        if (existing != values_.constEnd() && existing.value() == newValue) {
            continue;   // 值没变：不进 diff
        }
        LineChange change;
        change.key = key;
        if (existing != values_.constEnd()) {
            const int lineIndex = lineOf_.value(key);
            const QString original = lines_.at(lineIndex);
            // 只换冒号之后那一段，**原样保留"缩进 + 键名"与行尾注释**。
            // ⚠ 不能用 key（那是 `llm.mode` 这样的全路径）重建行首，
            //    否则预览会显示成 `  llm.mode: x`，与实际要写的行不符。
            const int sepAt = original.indexOf(QLatin1Char(':'));
            const QString head = (sepAt >= 0) ? original.left(sepAt) : original;
            change.lineNo = lineIndex + 1;
            change.oldLine = original;
            change.newLine = head + QStringLiteral(": ") + encodeScalar(newValue)
                             + commentOf_.value(key);
        } else {
            const int dot = key.lastIndexOf(QLatin1Char('.'));
            const QString parent = (dot < 0) ? QString() : key.left(dot);
            const auto bit = blocks_.constFind(parent);
            if (bit == blocks_.constEnd()) {
                if (error) {
                    *error = QStringLiteral("键 %1 现在不在 %2 里（整段/整块的新建由 Agent 按模板"
                                            "做；GUI 只预览已经存在的那几行）")
                                 .arg(key, QFileInfo(path_).fileName());
                }
                return false;
            }
            // 父块在、键不在：会插到父块末尾（缩进 = 父缩进 + 2；根块缩进是 -1 -> 0）
            const int indent = bit.value().indent < 0 ? 0 : bit.value().indent + 2;
            change.lineNo = -1;
            change.insertAt = bit.value().pastLine;
            change.indent = indent;
            change.newLine = QString(indent, QLatin1Char(' ')) + key.section(QLatin1Char('.'), -1)
                             + QStringLiteral(": ") + encodeScalar(newValue);
        }
        out->append(change);
    }
    return true;
}

QString ConfigStore::renderDiff(const QString& path, const QVector<LineChange>& changes)
{
    QStringList out;
    out << QStringLiteral("--- %1").arg(path);
    for (const LineChange& c : changes) {
        if (c.lineNo > 0) {
            out << QStringLiteral("- %1").arg(c.oldLine);
        }
        out << QStringLiteral("+ %1").arg(c.newLine);
    }
    return out.join(QLatin1Char('\n'));
}

} // namespace core
