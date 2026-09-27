// ============================================================================
//  gui/src/core/config_store.h — **只读**配置视图 + 改动预览（T14-3）
//
//  ⚠ T14-3 起 GUI **不写** config.yaml：唯一的写入者是 Agent（详见
//    `docs/adr/0005-config-single-writer.md`）。所以这个类现在只剩两件事：
//
//      · **读**：`load()` + `value()/boolValue()/intValue()/doubleValue()` —— 界面照着
//        配置显示（注释与顺序无所谓，只要值对）。
//      · **预览**：`set()` 只记"想改成什么"，`planChanges()`/`renderDiff()` 算出
//        "会改哪几行" —— 给人看，**不落盘**。
//
//    真正落盘走 IPC：`set_config` → Agent（`agent/core/settings_config.py` 那套文本级手术）
//    → 回执 `config_result`。**GUI 侧一行都不写**，所以"缺段新建 / 类型跟着模板走 /
//    `.bak` / 原子写"这些承诺全都搬到了 Agent 那一份实现里（T13-8/T14-2）。
//
//  历史（别当它是可以删的注释）
//  ---------------------------------------------------------------------------
//    T2–T13 这个类**是**一个文本级写入器（保注释保顺序、`.bak`、原子 rename，
//    T13-9 还给 GUI 加了"缺段按模板新建 + 类型校验"）。T14-3 把写路径整段删掉，
//    只留下读与预览 —— 因为两个进程各写一份的代价（互相覆盖、`.bak` 互盖）比
//    "GUI 自己写省一次 IPC 往返"大得多。
// ============================================================================
#pragma once

#include <QHash>
#include <QString>
#include <QStringList>
#include <QVector>

namespace core {

/// 一次预览里的单行改动（**只给 diff 看**；不落盘）。
struct LineChange {
    QString key;        ///< 键路径
    QString oldLine;    ///< 原行（新增行时为空）
    QString newLine;    ///< 新行
    int lineNo = -1;    ///< 1-based 行号；-1 = 新增（不在文件里）
    int insertAt = -1;  ///< 仅新增行有效：会插到第几行之前（0-based）
    int indent = 0;     ///< 仅新增行有效：缩进空格数
};

class ConfigStore {
public:
    ConfigStore() = default;

    /// 读文件并建索引。**文件不存在算错误**（调用方要如实说"读不到配置"）。
    bool load(const QString& path, QString* error = nullptr);

    const QString& path() const { return path_; }
    bool loaded() const { return exists_; }

    // ---- 读 ----
    QString value(const QString& key, const QString& fallback = QString()) const;
    bool boolValue(const QString& key, bool fallback) const;
    int intValue(const QString& key, int fallback) const;
    double doubleValue(const QString& key, double fallback) const;
    bool contains(const QString& key) const
    {
        return values_.contains(key) || pending_.contains(key);
    }

    // ---- 预览（**不落盘**；真正写入走 IPC 的 set_config）----
    /// 记下"想把这个键改成什么"。**只影响本对象**（以及 `planChanges()` 的结果）。
    void set(const QString& key, const QString& value);
    void setBool(const QString& key, bool value);
    void clearPending();
    bool hasPending() const { return !pendingOrder_.isEmpty(); }
    QStringList pendingKeys() const { return pendingOrder_; }
    /// 待改的 `{点号路径: 新值}` —— **就是发给 Agent 的那份 payload**
    /// （`set_config` 的 `keys` 字段），顺序与 `pendingKeys()` 一致。
    QHash<QString, QString> pendingValues() const { return pending_; }

    /// 算出将要改动哪几行（不落盘、不校验类型）。
    /// 键**不在文件里**时：父块在 -> 给一条"会新增"的预览；父块也不在 -> 返回 false +
    /// 说明（GUI 不预览"整段新建"，那是 Agent 按模板干的事）。
    bool planChanges(QVector<LineChange>* out, QString* error = nullptr) const;

    /// 渲染 diff（`--- path` / `- 旧` / `+ 新`）。
    static QString renderDiff(const QString& path, const QVector<LineChange>& changes);

    /// 当前文本。
    QString text() const { return lines_.join(QLatin1Char('\n')); }

private:
    struct Block {
        int firstLine = 0;   ///< 块首行（0-based，含）
        int pastLine = 0;    ///< 块末行之后（0-based，不含）
        int indent = -1;     ///< 容器键所在行缩进；根块为 -1
    };

    void index();
    static QString unquote(const QString& raw);
    static QString encodeScalar(const QString& value);
    static int indentOf(const QString& line);

    QString path_;
    bool exists_ = false;
    /// load() 成功读进文件后才为 true —— 否则一个路径写错就会"读出满屏默认值"当成功。
    bool loaded_ = false;
    QStringList lines_;

    QHash<QString, QString> values_;
    QHash<QString, int> lineOf_;
    QHash<QString, QString> commentOf_;   ///< 行尾注释（含前导空格，原样）—— 预览里要保留
    QHash<QString, Block> blocks_;        ///< 容器键 → 行范围；根块键为 ""

    QStringList pendingOrder_;
    QHash<QString, QString> pending_;
};

} // namespace core
