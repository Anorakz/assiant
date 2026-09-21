// ============================================================================
//  gui/src/core/config_store.h — 极简配置读写（文本级键路径替换）
//
//  需求（方案 §5）
//  ---------------------------------------------------------------------------
//    · config.yaml 是唯一真源（GUI 读写它的 gui: 段），保存时要**同步**写 llm.env
//    · **必须保住注释与原有顺序**：config.yaml 里那段"字段的事实约定"注释、
//      llm.env 里的分节注释，都是给人看的文档，不能被洗掉
//
//  所以不引 YAML 库（yaml-cpp 会整体重排 + 丢注释），而是：
//    读：按缩进栈走一遍，把 `a.b.c` 标量收进 hash
//    写：只替换目标行 **冒号/等号之后的那一段**，其余字节原样保留
//
//  两种风格
//  ---------------------------------------------------------------------------
//    Flavor::Yaml  config.yaml —— 支持 `a.b.c` 键路径与缩进块
//    Flavor::Env   llm.env              —— `KEY=value` 单层，键就是全名
// ============================================================================
#pragma once

#include <QHash>
#include <QString>
#include <QStringList>
#include <QVector>

namespace core {

enum class Flavor {
    Yaml,
    Env,
};

/// 一次保存里的单行改动（用于 diff 预览）。
struct LineChange {
    QString key;        ///< 键路径
    QString oldLine;    ///< 原行（新增行为空）
    QString newLine;    ///< 新行
    int lineNo = -1;    ///< 1-based 行号；-1 = 新增
    int insertAt = -1;  ///< 仅新增行有效：插到第几行之前（0-based）
    int indent = 0;     ///< 仅新增行有效：缩进空格数
};

class ConfigStore {
public:
    explicit ConfigStore(Flavor flavor = Flavor::Yaml);

    /// 读文件并建索引。**文件不存在不算错误**（视为空文档，save 时会新建）。
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

    // ---- 写（先记录，再 save）----
    void set(const QString& key, const QString& value);
    void setBool(const QString& key, bool value);
    void clearPending();
    bool hasPending() const { return !pendingOrder_.isEmpty(); }
    QStringList pendingKeys() const { return pendingOrder_; }

    /// 算出将要写入的行改动（不落盘）。键找不到父块时返回 false + error。
    bool planChanges(QVector<LineChange>* out, QString* error = nullptr) const;

    /// 落盘：备份 <path>.bak → 写 <path>.tmp → 原子 rename。
    bool save(QString* error = nullptr);

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
    /// 定位键所在行；不存在时给出可插入位置（父块末尾）与缩进。
    bool locate(const QString& key, int* lineIndex, int* insertAt, int* indent,
                QString* error) const;
    static QString unquote(const QString& raw);
    static QString encodeScalar(const QString& value);
    static int indentOf(const QString& line);

    Flavor flavor_;
    QString path_;
    bool exists_ = false;
    /// load() 成功读进文件后才为 true。save() 拒绝"从未加载过"的 store ——
    /// 否则一个路径写错就会凭空造出残桩配置（T9 踩过）。
    bool loaded_ = false;
    bool trailingNewline_ = true;   ///< 原文是否以换行结尾（重写时原样保持）
    QStringList lines_;

    QHash<QString, QString> values_;
    QHash<QString, int> lineOf_;
    QHash<QString, QString> commentOf_;   ///< 行尾注释（含前导空格，原样）
    QHash<QString, Block> blocks_;        ///< 容器键 → 行范围；根块键为 ""

    QStringList pendingOrder_;
    QHash<QString, QString> pending_;
};

} // namespace core
