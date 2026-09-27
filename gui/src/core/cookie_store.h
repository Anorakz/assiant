// ============================================================================
//  gui/src/core/cookie_store.h — B 站凭据文件（config/bilibili_cookie.json）**只读**
//
//  干什么（T13-9 起；T14-3 起只读）
//  ---------------------------------------------------------------------------
//    设置页"游戏检测"那张卡要显示"现在有没有凭据"（**只给掩码**）—— 就这一件事。
//
//  ⚠ T14-3 起 GUI **不写**这个文件：唯一的写入者是 Agent
//    （`agent/core/settings_credentials.py`，走 IPC 的 `set_config.credentials`），
//    理由与 config.yaml 完全一样（`docs/adr/0005`）：两个进程各写一份会互相覆盖，
//    `.bak` 也会互相盖掉。所以这里**只剩读 + 掩码**，`set()/save()` 都删了。
//
//  读的口径（与 Agent 侧同一套）
//  ---------------------------------------------------------------------------
//    · 只认 `SESSDATA` / `bili_jct` / `DedeUserID`（大小写敏感）；
//    · 文件不在 / 空的 / 坏的都是"没读到"，**不是错误** —— 匿名是合法状态；
//    · 相对路径按**仓库根**解析（与 Agent 读的是同一个文件）。
// ============================================================================
#pragma once

#include <QHash>
#include <QString>
#include <QStringList>

namespace core {

class CookieStore {
public:
    /// 只认这三个（B 站的键名，大小写敏感）。
    static QStringList allowedKeys();
    /// 配置里没写 `bilibili.cookie_file` 时的默认位置。
    static QString defaultRelativePath();
    /// 相对路径按**仓库根**解析（绝对路径原样 normpath）。
    static QString resolvePath(const QString& raw, const QString& repoRoot);
    /// 回显用：只给长度与头尾各两位（与 CLI 的 `_mask` 同一口径）。
    static QString mask(const QString& value);

    /// 读凭据文件。**不在 / 空的 / 坏的都是"没读到"**，不是错误。
    bool load(const QString& path);
    QString value(const QString& key) const { return values_.value(key); }
    bool isEmpty() const { return values_.isEmpty(); }
    QStringList keys() const { return values_.keys(); }

private:
    QHash<QString, QString> values_;
};

} // namespace core
