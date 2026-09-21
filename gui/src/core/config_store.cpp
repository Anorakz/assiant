// ============================================================================
//  gui/src/core/config_store.cpp — 极简配置读写实现
//
//  解析规则（刻意做窄，够用即可，宽了就得上 YAML 库）
//  ---------------------------------------------------------------------------
//    Yaml: 只认 `key: value` 与 `key:`（容器）。忽略空行、以 '#' 开头的整行注释、
//          以及以 '-' 开头的列表项。行尾 `  # 注释` 会被切开并原样保留。
//          嵌套靠**缩进栈**：缩进 <= 栈顶则弹栈，容器键路径 = 栈内键用 '.' 连接。
//    Env:  只认 `KEY=value`（'#' 开头的整行是注释）。
//
//  "保注释保顺序"的做法：写回时只重写目标行冒号/等号之后的部分，
//  未涉及的行一个字节都不动。
// ============================================================================
#include "core/config_store.h"

#include <QFile>
#include <QFileInfo>
#include <QStringList>

#include <algorithm>
#include <cstdio>   // std::rename

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
            *valuePart = raw.left(i - 1);
            *commentPart = raw.mid(i - 1);
            return;
        }
    }
    *valuePart = raw;
    commentPart->clear();
}

} // namespace

ConfigStore::ConfigStore(Flavor flavor)
    : flavor_(flavor)
{
}

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
        // ⚠ 不能对不存在的路径"假装加载成功"：调用方（例如输入源切换）会 set + save，
        //   于是凭空造出一个只有个别键的残桩配置文件（T9 出图时踩到：
        //   --config 指了个不存在的路径 → 生成了只有 gui.input_source 一行的 config.yaml）。
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
    trailingNewline_ = content.isEmpty() ? true : content.endsWith(QLatin1Char('\n'));
    lines_ = content.split(QLatin1Char('\n'));
    // 末尾换行的 split 会留一个空元素：去掉它，保证 save 时行数不被放大
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

    if (flavor_ == Flavor::Env) {
        for (int i = 0; i < lines_.size(); ++i) {
            const QString line = lines_.at(i);
            const QString trimmed = line.trimmed();
            if (trimmed.isEmpty() || trimmed.startsWith(QLatin1Char('#'))) {
                continue;
            }
            const int eq = trimmed.indexOf(QLatin1Char('='));
            if (eq <= 0) {
                continue;
            }
            const QString key = trimmed.left(eq).trimmed();
            values_.insert(key, unquote(trimmed.mid(eq + 1)));
            lineOf_.insert(key, i);
        }
        return;
    }

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
    // 未落盘的改动**对读取可见**（像编辑器的缓冲区）。
    // 否则 `--debug` 这种"只改内存不落盘"的覆盖会静默失效——T4 出图时真踩到了。
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

bool ConfigStore::locate(const QString& key, int* lineIndex, int* insertAt, int* indent,
                         QString* error) const
{
    const auto it = lineOf_.constFind(key);
    if (it != lineOf_.constEnd()) {
        *lineIndex = it.value();
        *insertAt = -1;
        *indent = indentOf(lines_.at(it.value()));
        return true;
    }

    // 键不在文件里：找父块，插到父块末尾（Yaml 缩进 = 父缩进 + 2；Env 直接追加）
    if (flavor_ == Flavor::Env) {
        *lineIndex = -1;
        *insertAt = lines_.size();
        *indent = 0;
        return true;
    }

    const int dot = key.lastIndexOf(QLatin1Char('.'));
    const QString parent = (dot < 0) ? QString() : key.left(dot);
    const auto bit = blocks_.constFind(parent);
    if (bit == blocks_.constEnd()) {
        if (error) {
            *error = QStringLiteral("找不到键 %1，也没有可插入的父块 %2")
                         .arg(key, parent.isEmpty() ? QStringLiteral("(根)") : parent);
        }
        return false;
    }
    *lineIndex = -1;
    *insertAt = bit.value().pastLine;
    *indent = bit.value().indent + 2;
    return true;
}

bool ConfigStore::planChanges(QVector<LineChange>* out, QString* error) const
{
    out->clear();
    for (const QString& key : pendingOrder_) {
        const QString newValue = pending_.value(key);
        // 值与文件现状一致 → 不算改动（不进 diff、不重写、不因此留 .bak）
        const auto existing = values_.constFind(key);
        if (existing != values_.constEnd() && existing.value() == newValue) {
            continue;
        }
        int lineIndex = -1;
        int insertAt = -1;
        int indent = 0;
        if (!locate(key, &lineIndex, &insertAt, &indent, error)) {
            return false;
        }
        LineChange change;
        change.key = key;
        const bool isEnv = (flavor_ == Flavor::Env);

        if (lineIndex >= 0) {
            // 已存在的行：**原样保留"缩进 + 键名"那一段**，只换冒号/等号之后的值。
            // ⚠ 这里不能用 key（那是 `llm.mode` 这样的全路径）重建行首，
            //    否则会把 `  mode: x` 写成 `  llm.mode: x`，把文件写坏。
            const QString original = lines_.at(lineIndex);
            const int sepAt = original.indexOf(isEnv ? QLatin1Char('=') : QLatin1Char(':'));
            const QString head = (sepAt >= 0) ? original.left(sepAt) : original;
            change.lineNo = lineIndex + 1;
            change.oldLine = original;
            change.newLine = isEnv
                                 ? head + QLatin1Char('=') + newValue
                                 : head + QStringLiteral(": ") + encodeScalar(newValue)
                                       + commentOf_.value(key);
        } else {
            // 新增行：只写键路径的**最后一段**，缩进由父块决定
            const QString shortKey = isEnv ? key : key.section(QLatin1Char('.'), -1);
            change.lineNo = -1;
            change.insertAt = insertAt;
            change.indent = indent;
            change.newLine = isEnv
                                 ? shortKey + QLatin1Char('=') + newValue
                                 : QString(indent, QLatin1Char(' ')) + shortKey
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

bool ConfigStore::save(QString* error)
{
    if (!hasPending()) {
        return true;   // 没有改动就不动文件（也不留 .bak）
    }
    if (!loaded_) {
        if (error) {
            *error = QStringLiteral("没有成功加载过 %1，拒绝写入").arg(path_);
        }
        return false;
    }
    QVector<LineChange> changes;
    if (!planChanges(&changes, error)) {
        if (error) {
            *error = QStringLiteral("拒绝写入 %1: %2").arg(path_, *error);
        }
        return false;
    }
    if (changes.isEmpty()) {
        clearPending();      // 全是"与现状相同"的待写值：不动文件、不留 .bak
        return true;
    }

    QStringList next = lines_;
    // 1) 先做行内替换（不改变行数）
    for (const LineChange& c : changes) {
        if (c.lineNo > 0 && c.lineNo <= next.size()) {
            next[c.lineNo - 1] = c.newLine;
        }
    }
    // 2) 再做插入：按插入点从后往前，避免下标漂移
    QVector<LineChange> inserts;
    for (const LineChange& c : changes) {
        if (c.lineNo <= 0) {
            inserts.append(c);
        }
    }
    std::sort(inserts.begin(), inserts.end(), [](const LineChange& a, const LineChange& b) {
        return a.insertAt > b.insertAt;
    });
    for (const LineChange& ins : inserts) {
        next.insert(qBound(0, ins.insertAt, next.size()), ins.newLine);
    }

    // 结尾换行也原样保持：原文没有就以没有结尾（"只改目标行"要做到字节级）
    const QString content = next.join(QLatin1Char('\n'))
                            + (trailingNewline_ ? QString(QLatin1Char('\n')) : QString());

    // 备份（只备份已存在的文件）
    if (exists_) {
        const QString bak = path_ + QStringLiteral(".bak");
        QFile::remove(bak);
        if (!QFile::copy(path_, bak)) {
            if (error) {
                *error = QStringLiteral("备份失败: %1").arg(bak);
            }
            return false;
        }
    }

    const QString tmp = path_ + QStringLiteral(".tmp");
    QFile out(tmp);
    if (!out.open(QIODevice::WriteOnly | QIODevice::Truncate)) {
        if (error) {
            *error = QStringLiteral("写临时文件失败: %1 (%2)").arg(tmp, out.errorString());
        }
        return false;
    }
    const QByteArray bytes = content.toUtf8();
    if (out.write(bytes) != bytes.size()) {
        out.close();
        QFile::remove(tmp);
        if (error) {
            *error = QStringLiteral("写临时文件不完整: %1").arg(tmp);
        }
        return false;
    }
    out.close();

    // 原子替换
    if (std::rename(tmp.toUtf8().constData(), path_.toUtf8().constData()) != 0) {
        QFile::remove(tmp);
        if (error) {
            *error = QStringLiteral("原子替换失败: %1").arg(path_);
        }
        return false;
    }

    // 重新索引（保持路径与行号新鲜）
    const QString savedPath = path_;
    load(savedPath, nullptr);
    clearPending();
    return true;
}

} // namespace core
