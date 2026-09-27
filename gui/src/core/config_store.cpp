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
//
//  T13-9 增补：**模板驱动的"缺段新建"**
//  ---------------------------------------------------------------------------
//    `loadTemplate()` 载入 `config/config.example.yaml`（与 Python 侧
//    `agent/core/settings_config.py` 同一套约定），于是：
//      · 键不在、**段/块也不在** -> 把模板里那一整块（含说明注释与块内其它键的
//        默认值）搬过来，缩进按目标位置对齐，再把目标键的值写进去；
//      · 键不在、父块在 -> 插到父块末尾，连模板里这个键上面的注释一起带；
//      · 值的类型跟着模板走（bool / int / float / str）。
//    没载入模板时**行为与 T13-9 之前完全一致**：父块不存在就拒绝写入。
// ============================================================================
#include "core/config_store.h"

#include <QFile>
#include <QFileInfo>
#include <QStringList>
#include <QVector>

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
            // ⚠ 把**对齐用的那串空白**一起留给注释：`  mode: x      # 注释` 改值之后
            //   注释还要对在原地（早先只留一个空格，写一次值对齐就散了）。
            //   `agent/core/settings_config.py::_split_value` 就是这个口径。
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

/// 把 `键: 值  # 注释` 那一行的值换成 `rendered`（**保住缩进与行尾注释**）。
///
/// 与 `agent/core/settings_config.py::_new_line` 同一个意思 —— 模板里的目标键那一行
/// 要先换成"用户要的值"再插进去，否则新建出来的段里还是模板的默认值
/// （T13-8 的 Python 单测抓过一次同款问题）。
QString withValue(const QString& line, const QString& rendered)
{
    const int colon = line.indexOf(QLatin1Char(':'));
    if (colon <= 0) {
        return line;
    }
    QString valuePart;
    QString commentPart;
    splitTrailingComment(line.mid(colon + 1), &valuePart, &commentPart);
    const QString trimmed = valuePart.trimmed();
    const QString lead = valuePart.left(valuePart.size() - trimmed.size());
    return line.left(colon + 1) + (lead.isEmpty() ? QStringLiteral(" ") : lead)
           + rendered + commentPart;
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

QString ConfigStore::siblingTemplatePath(const QString& configPath)
{
    if (configPath.isEmpty()) {
        return QString();
    }
    return QFileInfo(configPath).absolutePath() + QStringLiteral("/config.example.yaml");
}

bool ConfigStore::loadTemplate(const QString& path, QString* error)
{
    tplLoaded_ = false;
    tplPath_ = path;
    tplLines_.clear();
    tplLeaves_.clear();
    tplBlocks_.clear();
    if (path.isEmpty()) {
        if (error) {
            *error = QStringLiteral("模板路径为空");
        }
        return false;
    }
    QFile file(path);
    if (!file.exists()) {
        if (error) {
            *error = QStringLiteral("模板不存在: %1（缺段新建与类型校验都以它为准）").arg(path);
        }
        return false;
    }
    if (!file.open(QIODevice::ReadOnly | QIODevice::Text)) {
        if (error) {
            *error = QStringLiteral("打不开模板 %1: %2").arg(path, file.errorString());
        }
        return false;
    }
    const QString content = QString::fromUtf8(file.readAll());
    file.close();
    tplLines_ = content.split(QLatin1Char('\n'));
    if (!tplLines_.isEmpty() && tplLines_.last().isEmpty()) {
        tplLines_.removeLast();
    }
    indexTemplate();
    tplLoaded_ = true;
    return true;
}

void ConfigStore::index()
{
    Index idx;
    indexLines(lines_, &idx);
    values_ = idx.values;
    lineOf_ = idx.lineOf;
    commentOf_ = idx.commentOf;
    blocks_ = idx.blocks;
}

void ConfigStore::indexLines(const QStringList& lines, Index* out) const
{
    out->values.clear();
    out->lineOf.clear();
    out->commentOf.clear();
    out->blocks.clear();

    out->blocks.insert(QString(), Block{0, lines.size(), -1});

    if (flavor_ == Flavor::Env) {
        for (int i = 0; i < lines.size(); ++i) {
            const QString line = lines.at(i);
            const QString trimmed = line.trimmed();
            if (trimmed.isEmpty() || trimmed.startsWith(QLatin1Char('#'))) {
                continue;
            }
            const int eq = trimmed.indexOf(QLatin1Char('='));
            if (eq <= 0) {
                continue;
            }
            const QString key = trimmed.left(eq).trimmed();
            out->values.insert(key, unquote(trimmed.mid(eq + 1)));
            out->lineOf.insert(key, i);
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

    for (int i = 0; i < lines.size(); ++i) {
        const QString line = lines.at(i);
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
            out->blocks.insert(full, Block{f.line + 1, i, f.indent});
        }
        const QString parent = prefixOf();
        const QString full = parent.isEmpty() ? key : parent + QLatin1Char('.') + key;

        QString valuePart;
        QString commentPart;
        splitTrailingComment(trimmed.mid(colon + 1), &valuePart, &commentPart);

        if (valuePart.trimmed().isEmpty()) {
            stack.append(Frame{indent, key, i});   // 容器
        } else {
            out->values.insert(full, unquote(valuePart));
            out->lineOf.insert(full, i);
            out->commentOf.insert(full, commentPart);
        }
    }
    while (!stack.isEmpty()) {
        const Frame f = stack.takeLast();
        const QString parent = prefixOf();
        const QString full = parent.isEmpty() ? f.key : parent + QLatin1Char('.') + f.key;
        out->blocks.insert(full, Block{f.line + 1, lines.size(), f.indent});
    }
}

void ConfigStore::indexTemplate()
{
    tplLeaves_.clear();
    tplBlocks_.clear();

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
    // 紧贴在这一行上面的注释段（**遇到空行/非注释就停** —— 段与段之间就靠那个空行分界）
    const auto commentStart = [this](int line) {
        int start = -1;
        for (int i = line - 1; i >= 0; --i) {
            const QString trimmed = tplLines_.at(i).trimmed();
            if (trimmed.isEmpty() || !trimmed.startsWith(QLatin1Char('#'))) {
                break;
            }
            start = i;
        }
        return start;
    };

    for (int i = 0; i < tplLines_.size(); ++i) {
        const QString line = tplLines_.at(i);
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

        while (!stack.isEmpty() && stack.last().indent >= indent) {
            const Frame f = stack.takeLast();
            const QString parent = prefixOf();
            const QString full = parent.isEmpty() ? f.key : parent + QLatin1Char('.') + f.key;
            tplBlocks_.insert(full, TplBlock{f.line, f.indent, i, commentStart(f.line)});
        }
        const QString parent = prefixOf();
        const QString full = parent.isEmpty() ? key : parent + QLatin1Char('.') + key;

        QString valuePart;
        QString commentPart;
        splitTrailingComment(trimmed.mid(colon + 1), &valuePart, &commentPart);

        if (valuePart.trimmed().isEmpty()) {
            stack.append(Frame{indent, key, i});
        } else if (!key.contains(QLatin1Char('.'))) {
            tplLeaves_.insert(full, TplLeaf{i, indent, unquote(valuePart), commentStart(i)});
        }
        // ⚠ 键名里带点的**映射条目**（`study.process_names` 下的 `code.exe`）没法用点号
        //   路径寻址，不进键清单 —— 与 `settings_config._Template` 同一口径（结构级的东西手改）。
    }
    while (!stack.isEmpty()) {
        const Frame f = stack.takeLast();
        const QString parent = prefixOf();
        const QString full = parent.isEmpty() ? f.key : parent + QLatin1Char('.') + f.key;
        tplBlocks_.insert(full, TplBlock{f.line, f.indent, tplLines_.size(), commentStart(f.line)});
    }
}

QStringList ConfigStore::templateBlockLines(const QString& path, int wantIndent) const
{
    const auto it = tplBlocks_.constFind(path);
    if (it == tplBlocks_.constEnd()) {
        return QStringList();
    }
    const TplBlock& block = it.value();
    const int start = block.commentStart >= 0 ? block.commentStart : block.line;
    int end = block.pastLine;
    // 块尾那些**属于父级**的空行/注释不搬（目标文件里已经有了）——
    // 与 Python 侧"段的末尾遇到下一个段的横幅就停"是同一个意思。
    while (end > block.line + 1) {
        const QString body = tplLines_.at(end - 1);
        const QString trimmed = body.trimmed();
        if (trimmed.isEmpty()
            || (trimmed.startsWith(QLatin1Char('#')) && indentOf(body) <= block.indent)) {
            --end;
            continue;
        }
        break;
    }
    const int delta = wantIndent - block.indent;
    QStringList out;
    for (int i = start; i < end; ++i) {
        QString line = tplLines_.at(i);
        if (delta > 0) {
            line = QString(delta, QLatin1Char(' ')) + line;
        } else if (delta < 0) {
            line = line.mid(qMin(-delta, indentOf(line)));
        }
        out << line;
    }
    return out;
}

QString ConfigStore::templateKind(const QString& raw)
{
    const QString text = raw.trimmed();
    const QString low = text.toLower();
    if (low == QLatin1String("true") || low == QLatin1String("false")) {
        return QStringLiteral("bool");
    }
    bool ok = false;
    text.toLongLong(&ok);
    if (ok) {
        return QStringLiteral("int");
    }
    text.toDouble(&ok);
    return ok ? QStringLiteral("float") : QStringLiteral("str");
}

bool ConfigStore::kindMatches(const QString& key, const QString& value, QString* error) const
{
    if (!tplLoaded_) {
        return true;
    }
    const auto it = tplLeaves_.constFind(key);
    if (it == tplLeaves_.constEnd()) {
        return true;         // 模板不认识的键：类型不在这里管（照旧写法写出去）
    }
    const QString kind = templateKind(it.value().value);
    const QString text = value.trimmed();
    const QString low = text.toLower();
    if (kind == QLatin1String("bool")) {
        if (low == QLatin1String("true") || low == QLatin1String("false")
            || low == QLatin1String("1") || low == QLatin1String("0")
            || low == QLatin1String("yes") || low == QLatin1String("no")
            || low == QLatin1String("on") || low == QLatin1String("off")) {
            return true;
        }
        if (error) {
            *error = QStringLiteral("这个键是布尔值（模板里是 %1），给不了 \"%2\"")
                         .arg(it.value().value, value);
        }
        return false;
    }
    if (kind == QLatin1String("int") || kind == QLatin1String("float")) {
        bool ok = false;
        text.toDouble(&ok);
        if (!ok) {
            if (error) {
                *error = QStringLiteral("这个键是数字（模板里是 %1），给不了 \"%2\"")
                             .arg(it.value().value, value);
            }
            return false;
        }
        if (kind == QLatin1String("int")) {
            bool isInteger = false;
            text.toLongLong(&isInteger);
            if (!isInteger) {
                if (error) {
                    *error = QStringLiteral("这个键是整数（模板里是 %1），给不了小数 \"%2\"")
                                 .arg(it.value().value, value);
                }
                return false;
            }
        }
        return true;
    }
    return true;   // 字符串：什么都能写（encodeScalar 会按需加引号）
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

void ConfigStore::applyChange(QStringList* lines, const LineChange& change)
{
    if (change.lineNo > 0) {
        if (change.lineNo <= lines->size()) {
            (*lines)[change.lineNo - 1] = change.newLine;
        }
        return;
    }
    const QStringList block =
        change.newLines.isEmpty() ? QStringList{change.newLine} : change.newLines;
    int at = qBound(0, change.insertAt, lines->size());
    for (const QString& line : block) {
        lines->insert(at++, line);
    }
}

bool ConfigStore::planInsert(const QString& key, const QString& value, const QStringList& working,
                             const Index& index, LineChange* out, QString* error) const
{
    out->key = key;

    if (flavor_ == Flavor::Env) {
        out->lineNo = -1;
        out->insertAt = working.size();
        out->indent = 0;
        out->newLine = key + QLatin1Char('=') + value;
        return true;
    }

    const int dot = key.lastIndexOf(QLatin1Char('.'));
    const QString parent = (dot < 0) ? QString() : key.left(dot);
    const auto parentIt = index.blocks.constFind(parent);
    if (parentIt != index.blocks.constEnd()) {
        // 父块在、键不在：插到父块末尾（带上模板里这个键上面那段说明注释）
        // ⚠ 根块的缩进是 -1（不是 0），顶层新键要插成 **0 空格** —— 早先一律 `+2`，
        //   顶层新键会带着 1 个空格落进文件，读回来就成了上一个块的子键
        //   （T13-9 加"新建块里必须有这个键"的校验时才把它逼出来）。
        const int indent = parentIt.value().indent < 0 ? 0 : parentIt.value().indent + 2;
        QStringList lines;
        if (tplLoaded_) {
            const auto leaf = tplLeaves_.constFind(key);
            if (leaf != tplLeaves_.constEnd() && leaf.value().commentStart >= 0) {
                for (int i = leaf.value().commentStart; i < leaf.value().line; ++i) {
                    lines << tplLines_.at(i);
                }
            }
        }
        lines << QString(indent, QLatin1Char(' ')) + key.section(QLatin1Char('.'), -1)
                     + QStringLiteral(": ") + encodeScalar(value);
        out->lineNo = -1;
        out->insertAt = parentIt.value().pastLine;
        out->indent = indent;
        out->newLine = lines.last();
        if (lines.size() > 1) {
            out->newLines = lines;
        }
        return true;
    }

    // 父块也不在：只有**载入了模板**才谈得上"新建"（否则与以前一样拒绝）
    if (!tplLoaded_) {
        if (error) {
            *error = QStringLiteral("找不到键 %1，也没有可插入的父块 %2")
                         .arg(key, parent.isEmpty() ? QStringLiteral("(根)") : parent);
        }
        return false;
    }

    // 找**最深的已存在祖先**，以及它下面缺的那一块（可能是中间容器，也可能是顶层段）
    const QStringList parts = key.split(QLatin1Char('.'));
    QString ancestor;            // 空串 = 根块
    int ancestorIndent = -1;
    QString missing;
    QString walked;
    for (int i = 0; i + 1 < parts.size(); ++i) {
        walked = walked.isEmpty() ? parts.at(i) : walked + QLatin1Char('.') + parts.at(i);
        const auto bit = index.blocks.constFind(walked);
        if (bit == index.blocks.constEnd()) {
            missing = walked;
            break;
        }
        ancestor = walked;
        ancestorIndent = bit.value().indent;
    }
    if (missing.isEmpty()) {
        if (error) {
            *error = QStringLiteral("找不到键 %1 的父块（%2）").arg(key, parent);
        }
        return false;
    }

    const auto tbit = tplBlocks_.constFind(missing);
    if (tbit == tplBlocks_.constEnd()) {
        if (error) {
            *error = QStringLiteral("找不到键 %1：父块 %2 不存在，模板 %3 里也没有 %2 这一块，没法新建")
                         .arg(key, missing, QFileInfo(tplPath_).fileName());
        }
        return false;
    }
    const int wantIndent = ancestor.isEmpty() ? 0 : ancestorIndent + 2;
    QStringList lines = templateBlockLines(missing, wantIndent);
    if (lines.isEmpty()) {
        if (error) {
            *error = QStringLiteral("模板里 %1 这一块是空的，没法新建").arg(missing);
        }
        return false;
    }

    // 目标键那一行：把模板的默认值换成**要写的值**再搬（与 Python 的 _block_with_value 同口径）
    QString targetLine;
    const auto leaf = tplLeaves_.constFind(key);
    if (leaf != tplLeaves_.constEnd()) {
        const int start = tbit.value().commentStart >= 0 ? tbit.value().commentStart
                                                         : tbit.value().line;
        const int offset = leaf.value().line - start;
        if (offset >= 0 && offset < lines.size()) {
            targetLine = withValue(lines.at(offset), encodeScalar(value));
            lines[offset] = targetLine;
        }
    }
    if (targetLine.isEmpty()) {
        targetLine = lines.last();
    }
    const int at = index.blocks.value(ancestor).pastLine;
    // 与已有内容隔一个空行（与 settings_config "追加整段"同一口径）
    if (at > 0 && !working.at(at - 1).trimmed().isEmpty()) {
        lines.prepend(QString());
    }

    out->lineNo = -1;
    out->insertAt = at;
    out->indent = wantIndent;
    out->newLine = targetLine;
    out->newLines = lines;
    return true;
}

bool ConfigStore::planChanges(QVector<LineChange>* out, QString* error) const
{
    out->clear();
    // ⚠ 一次保存里的多个键可能落进**刚插进去的块**里（缺段新建时就是这样），
    //   所以这里维护一份工作副本、每落一条改动就重新索引，而不是只看原文件。
    QStringList working = lines_;
    Index index;
    indexLines(working, &index);
    const bool isEnv = (flavor_ == Flavor::Env);

    for (const QString& key : pendingOrder_) {
        const QString newValue = pending_.value(key);
        if (!kindMatches(key, newValue, error)) {
            return false;
        }
        const auto existing = index.values.constFind(key);
        if (existing != index.values.constEnd() && existing.value() == newValue) {
            continue;   // 值与文件现状一致 → 不算改动（不进 diff、不重写、不因此留 .bak）
        }

        if (existing == index.values.constEnd()) {
            LineChange insert;
            if (!planInsert(key, newValue, working, index, &insert, error)) {
                return false;
            }
            applyChange(&working, insert);
            indexLines(working, &index);
            out->append(insert);
            if (!index.values.contains(key)) {
                if (error) {
                    *error = QStringLiteral("新建出来的块里没有 %1 这个键（模板里缺了它？）")
                                 .arg(key);
                }
                return false;
            }
        }

        const auto now = index.values.constFind(key);
        if (now == index.values.constEnd() || now.value() == newValue) {
            continue;   // 插入时就把值写进去了
        }
        const int lineIndex = index.lineOf.value(key);
        // 已存在的行：**原样保留"缩进 + 键名"那一段**，只换冒号/等号之后的值。
        // ⚠ 这里不能用 key（那是 `llm.mode` 这样的全路径）重建行首，
        //    否则会把 `  mode: x` 写成 `  llm.mode: x`，把文件写坏。
        const QString original = working.at(lineIndex);
        const int sepAt = original.indexOf(isEnv ? QLatin1Char('=') : QLatin1Char(':'));
        const QString head = (sepAt >= 0) ? original.left(sepAt) : original;
        LineChange change;
        change.key = key;
        change.lineNo = lineIndex + 1;
        change.oldLine = original;
        change.newLine = isEnv ? head + QLatin1Char('=') + newValue
                               : head + QStringLiteral(": ") + encodeScalar(newValue)
                                     + index.commentOf.value(key);
        applyChange(&working, change);
        indexLines(working, &index);
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
        if (c.newLines.isEmpty()) {
            out << QStringLiteral("+ %1").arg(c.newLine);
        } else {
            for (const QString& line : c.newLines) {
                out << QStringLiteral("+ %1").arg(line);
            }
        }
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

    // 按**计划顺序回放**（计划是边算边落的：插入点用的是那一刻的工作副本下标，
    // 所以回放的顺序必须与算的时候一致 —— 原来是"先替换、再倒序插入"，
    // 遇到"一次插整段"就排不出来了）。
    QStringList next = lines_;
    for (const LineChange& change : changes) {
        applyChange(&next, change);
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
