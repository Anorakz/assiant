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
    QString key;         ///< 键路径
    QString oldLine;     ///< 原行（新增行为空）
    QString newLine;     ///< 新行（多行插入时 = `newLines` 的第一行）
    QStringList newLines;///< 多行插入（T13-9 的"缺段按模板新建"）；非空时以它为准
    int lineNo = -1;     ///< 1-based 行号；-1 = 新增
    int insertAt = -1;   ///< 仅新增行有效：插到第几行之前（0-based，**按计划顺序回放**）
    int indent = 0;      ///< 仅新增行有效：缩进空格数
};

class ConfigStore {
public:
    explicit ConfigStore(Flavor flavor = Flavor::Yaml);

    /// 读文件并建索引。**文件不存在不算错误**（视为空文档，save 时会新建）。
    bool load(const QString& path, QString* error = nullptr);

    /// 载入"键清单 + 说明注释"的模板（板端 = `config/config.example.yaml`）。
    ///
    /// 载入之后多两条能力（T13-9；与 `agent/core/settings_config.py` **同一套约定**：
    /// 那个 Python 写入器跑在 Agent 侧、这个跑在 GUI 侧，两边没法共用代码，只能同口径）：
    ///   · **缺段/缺块可以按模板新建**：把模板里那一整块（含它上面那段说明注释、含
    ///     块内其它键的默认值）搬过来，缩进按目标位置对齐；段在、只是键不在时，
    ///     连模板里这个键上面的注释一起带上。
    ///   · **值的类型跟着模板走**：模板里是 `false` / `0.05` / 路径，就只收对应类型
    ///     —— 免得把 `relative_band: "0.05"`（字符串）这种错悄悄写进真源
    ///     （YAML 不报错，Agent 读出来却是另一个东西）。
    /// 没载入模板时行为与 T13-9 之前**完全一致**：键在位就改、父块不存在就拒绝写入。
    bool loadTemplate(const QString& path, QString* error = nullptr);
    bool hasTemplate() const { return tplLoaded_; }
    const QString& templatePath() const { return tplPath_; }
    /// 与配置同目录的模板路径（`<dir>/config.example.yaml`；不判断它存不存在）。
    static QString siblingTemplatePath(const QString& configPath);

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

    /// 一份行集合的索引。成员索引与"算计划时的工作副本"共用这一个结构
    /// （T13-9：一次保存里的多个键可能落在**刚插进去的块**里，所以必须边计划边重新索引）。
    struct Index {
        QHash<QString, QString> values;
        QHash<QString, int> lineOf;
        QHash<QString, QString> commentOf;   ///< 行尾注释（含前导空格，原样）
        QHash<QString, Block> blocks;
    };

    /// 模板里的一个**叶子键**。
    struct TplLeaf {
        int line = -1;           ///< 键那一行（0-based）
        int indent = 0;
        QString value;           ///< 裸值（去引号）
        int commentStart = -1;   ///< 紧贴上面的注释段起始行（没有 = -1）
    };

    /// 模板里的一个**块**（顶层段或嵌套映射）。
    struct TplBlock {
        int line = -1;           ///< 容器键那一行
        int indent = 0;
        int pastLine = 0;        ///< 块末之后（不含）
        int commentStart = -1;   ///< 紧贴上面的注释段起始行（没有 = -1）
    };

    void index();
    void indexLines(const QStringList& lines, Index* out) const;
    void indexTemplate();
    /// 模板里 `path` 这一块的原文（含说明注释），缩进对齐到 `wantIndent`。
    QStringList templateBlockLines(const QString& path, int wantIndent) const;
    /// 计划"新增一个键"：父块在就插进父块末尾；父块不在则（有模板时）搬来缺的那一块。
    bool planInsert(const QString& key, const QString& value, const QStringList& working,
                    const Index& index, LineChange* out, QString* error) const;
    /// 把一条改动落进"工作副本"（set = 换行；insert = 插一行或多行）。
    static void applyChange(QStringList* lines, const LineChange& change);
    /// 值的类型跟着模板走（模板不认识的键不在这里管）。
    bool kindMatches(const QString& key, const QString& value, QString* error) const;
    static QString templateKind(const QString& raw);
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

    // ---- 模板（缺段新建 + 类型校验；没载入时全是空的）----
    bool tplLoaded_ = false;
    QString tplPath_;
    QStringList tplLines_;
    QHash<QString, TplLeaf> tplLeaves_;
    QHash<QString, TplBlock> tplBlocks_;
};

} // namespace core
