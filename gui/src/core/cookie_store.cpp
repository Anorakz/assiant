// ============================================================================
//  gui/src/core/cookie_store.cpp — B 站凭据文件读写实现（T13-9）
//
//  JSON 的形状刻意与 Python 侧逐字节对齐（`json.dumps(..., ensure_ascii=False,
//  indent=2)` + 结尾换行）：两边写的是**同一个文件**，格式漂移没有意义，
//  而"两空格缩进"是那边已经落在板上的样子。
// ============================================================================
#include "core/cookie_store.h"

#include <QDir>
#include <QFile>
#include <QFileInfo>
#include <QJsonDocument>
#include <QJsonObject>
#include <QJsonParseError>

#include <cstdio>   // std::rename

namespace core {

namespace {

const char* const kAllowed[] = {"SESSDATA", "bili_jct", "DedeUserID"};

/// JSON 字符串字面量（`json.dumps` 的等价物：转义引号/反斜杠/控制字符）。
QString jsonString(const QString& text)
{
    QString out;
    out.reserve(text.size() + 2);
    out += QLatin1Char('"');
    for (const QChar c : text) {
        switch (c.unicode()) {
        case u'"':
            out += QLatin1String("\\\"");
            break;
        case u'\\':
            out += QLatin1String("\\\\");
            break;
        case u'\n':
            out += QLatin1String("\\n");
            break;
        case u'\r':
            out += QLatin1String("\\r");
            break;
        case u'\t':
            out += QLatin1String("\\t");
            break;
        case u'\b':
            out += QLatin1String("\\b");
            break;
        case u'\f':
            out += QLatin1String("\\f");
            break;
        default:
            if (c.unicode() < 0x20) {
                out += QStringLiteral("\\u%1").arg(c.unicode(), 4, 16, QLatin1Char('0'));
            } else {
                out += c;
            }
            break;
        }
    }
    out += QLatin1Char('"');
    return out;
}

} // namespace

QStringList CookieStore::allowedKeys()
{
    QStringList out;
    for (const char* key : kAllowed) {
        out << QString::fromLatin1(key);
    }
    return out;
}

QString CookieStore::defaultRelativePath()
{
    return QStringLiteral("config/bilibili_cookie.json");
}

QString CookieStore::resolvePath(const QString& raw, const QString& repoRoot)
{
    const QString text = raw.trimmed();
    const QString path = text.isEmpty() ? defaultRelativePath() : text;
    const QFileInfo info(path);
    if (info.isAbsolute()) {
        return QDir::cleanPath(path);
    }
    if (repoRoot.trimmed().isEmpty()) {
        return QDir::cleanPath(path);
    }
    return QDir::cleanPath(repoRoot + QLatin1Char('/') + path);
}

QString CookieStore::mask(const QString& value)
{
    const QString text = value;
    if (text.size() <= 8) {
        return QString(text.size(), QLatin1Char('*'));
    }
    return QStringLiteral("%1…%2（%3 位）")
        .arg(text.left(2), text.right(2))
        .arg(text.size());
}

bool CookieStore::load(const QString& path)
{
    values_.clear();
    QFile file(path);
    if (!file.exists() || !file.open(QIODevice::ReadOnly)) {
        return false;
    }
    const QByteArray bytes = file.readAll();
    file.close();
    QJsonParseError parseError;
    const QJsonDocument doc = QJsonDocument::fromJson(bytes, &parseError);
    if (parseError.error != QJsonParseError::NoError || !doc.isObject()) {
        return false;          // 坏文件 = 没读到（匿名是合法状态，不当错误）
    }
    const QJsonObject object = doc.object();
    for (const QString& key : allowedKeys()) {
        const QString value = object.value(key).toString().trimmed();
        if (!value.isEmpty()) {
            values_.insert(key, value);
        }
    }
    return !values_.isEmpty();
}

bool CookieStore::set(const QString& key, const QString& value, QString* error)
{
    if (!allowedKeys().contains(key)) {
        if (error) {
            *error = QStringLiteral("不认识的键 \"%1\"（只认 %2 —— 笔误会被 B 站当成没登录）")
                         .arg(key, allowedKeys().join(QStringLiteral(" / ")));
        }
        return false;
    }
    const QString text = value.trimmed();
    if (text.isEmpty()) {
        return true;           // 空值 = 不改动这个键（不是"删掉它"）
    }
    if (text.contains(QLatin1Char('\n')) || text.contains(QLatin1Char('\r'))) {
        if (error) {
            *error = QStringLiteral("凭据不能换行（一行一个键）");
        }
        return false;
    }
    values_.insert(key, text);
    return true;
}

bool CookieStore::save(const QString& path, QString* error, bool* wrote) const
{
    if (wrote != nullptr) {
        *wrote = false;
    }
    if (values_.isEmpty()) {
        return true;           // 没给任何值：不写文件、也不留 .bak
    }
    // 合并：先把文件里已有的（只认那三个键）读进来，再用本次给的值覆盖 ——
    // 只填一个键时不许把别的键抹掉。
    QHash<QString, QString> merged;
    {
        CookieStore existing;
        existing.load(path);
        for (const QString& key : allowedKeys()) {
            if (!existing.value(key).isEmpty()) {
                merged.insert(key, existing.value(key));
            }
        }
    }
    for (const QString& key : allowedKeys()) {
        if (!values_.value(key).isEmpty()) {
            merged.insert(key, values_.value(key));
        }
    }

    QStringList lines;
    lines << QStringLiteral("{");
    QStringList body;
    for (const QString& key : allowedKeys()) {
        if (!merged.value(key).isEmpty()) {
            body << QStringLiteral("  %1: %2").arg(jsonString(key), jsonString(merged.value(key)));
        }
    }
    lines << body.join(QStringLiteral(",\n"));
    lines << QStringLiteral("}");
    const QString content = lines.join(QLatin1Char('\n')) + QLatin1Char('\n');

    const QString dir = QFileInfo(path).absolutePath();
    if (!dir.isEmpty()) {
        QDir().mkpath(dir);
    }
    // 备份（只备份已存在的文件）
    if (QFile::exists(path)) {
        const QString bak = path + QStringLiteral(".bak");
        QFile::remove(bak);
        if (!QFile::copy(path, bak)) {
            if (error) {
                *error = QStringLiteral("备份失败: %1").arg(bak);
            }
            return false;
        }
    }
    const QString tmp = path + QStringLiteral(".tmp");
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
    if (std::rename(tmp.toUtf8().constData(), path.toUtf8().constData()) != 0) {
        QFile::remove(tmp);
        if (error) {
            *error = QStringLiteral("原子替换失败: %1").arg(path);
        }
        return false;
    }
    if (wrote != nullptr) {
        *wrote = true;
    }
    return true;
}

} // namespace core
