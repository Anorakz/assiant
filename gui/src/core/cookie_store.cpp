// ============================================================================
//  gui/src/core/cookie_store.cpp — B 站凭据文件**只读**实现（T14-3）
//
//  ⚠ 这里没有任何写路径：写凭据的是 Agent（`agent/core/settings_credentials.py`，
//    走 IPC 的 `set_config.credentials`）。理由见头文件与 docs/adr/0005。
// ============================================================================
#include "core/cookie_store.h"

#include <QDir>
#include <QFile>
#include <QFileInfo>
#include <QJsonDocument>
#include <QJsonObject>
#include <QJsonParseError>

namespace core {

namespace {

const char* const kAllowed[] = {"SESSDATA", "bili_jct", "DedeUserID"};

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

} // namespace core
