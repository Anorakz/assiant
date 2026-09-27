// ============================================================================
//  gui/src/core/cookie_store.h — B 站凭据文件（config/bilibili_cookie.json）
//
//  干什么（T13-9）: 设置页"游戏检测"那张卡里的 B 站凭据由它落盘 / 读出来。
//
//  为什么单独一个类（不塞进 ConfigStore）
//  ---------------------------------------------------------------------------
//    · 它写的**不是配置真源**，是**凭据**：格式是 JSON、内容只有几个键、
//      路径来自 `bilibili.cookie_file`，而且回显要打码。
//    · ConfigStore 的承诺是"只动 config.yaml 里那一行"；凭据是另一件事
//      （整份文件、原子写、`.bak`）。混在一起会让两边的承诺都变模糊。
//
//  与 `agent/core/settings_credentials.py` **同一套约定**（那个跑在 Agent 侧、
//  这个跑在板端 GUI 里，两边没法共用代码，只能同口径）：
//    · 只认 `SESSDATA` / `bili_jct` / `DedeUserID`（⚠ 大小写敏感：`SEESSDATA`
//      这种笔误实测 B 站会当没登录，所以这里直接拒绝）；
//    · **合并**写：只给一个键时不许把别的键抹掉；空值 = 不改动这个键；
//    · 先 `.bak` 再原子写；相对路径按**仓库根**解析（与 Agent 读的是同一个文件）。
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

    /// 读凭据文件。**不在 / 空的 / 坏的都是"没读到"**，不是错误 —— 匿名是合法状态。
    bool load(const QString& path);
    QString value(const QString& key) const { return values_.value(key); }
    bool isEmpty() const { return values_.isEmpty(); }
    QStringList keys() const { return values_.keys(); }

    /// 记一个待写入的凭据。空值 = **不改动这个键**（与 CLI 侧同一口径）。
    /// @return false + error：键不认识（笔误会被 B 站当成没登录）
    bool set(const QString& key, const QString& value, QString* error = nullptr);

    /// 合并 + 落盘（先 `.bak` 再原子写）。
    /// @param wrote 真的写了文件才置 true（没有待写值时不写、也不留 `.bak`）
    bool save(const QString& path, QString* error = nullptr, bool* wrote = nullptr) const;

private:
    QHash<QString, QString> values_;
};

} // namespace core
