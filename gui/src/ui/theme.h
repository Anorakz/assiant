// ============================================================================
//  gui/src/ui/theme.h — 唯一的**视觉常量**来源（T15-16 / G-A-1a）
//
//  为什么要有它
//  ---------------------------------------------------------------------------
//  在这之前，配色只活在两处**注释**里（`main_window.cpp:62-63`、`top_bar.cpp:9-11`），
//  而真正的色值**散落在 116 处字面量**里（G-A-1 实测：`gui/src` 全目录 grep `#RRGGBB`
//  命中 116 行，其中 `kBaseStyle` 那段 QSS 占绝大多数），字号也有 **10 档、约 25 处**。
//  后果是具体的：同一族颜色已经漂出**近似重复值** ✗ ——
//    · 次文字：`#9AA0A6`（QSS 主用）vs **`#9AA1A9`**（`music_bar.cpp:168,238,313`）
//    · 分隔/边框：`#3A3D42`（主用）vs `#33363B`（浮层边框）
//    · 面板 hover：`#35373B`（QSS）vs `rgba(53,55,59,…)`（内联）
//  所以本文件是**后续所有视觉改动的唯一落点** ✓；G-A-2 的守卫测试按它判"有没有裸色值" ✓。
//
//  ⚠ 两条纪律
//  ---------------------------------------------------------------------------
//    · **不要再往这里加"用完就算"的颜色** ✗ —— 加之前先问"能不能用已有的" ✓；
//    · `rgba(...)` **暂时保持字面量** ✗（QSS 里 alpha 必须写进字符串，见 §rgba 清单），
//      但**必须**用下面某个基色的同名 α 变体，不许引入新色相 ✓。
//
//  与文档的关系
//  ---------------------------------------------------------------------------
//  `docs/gui.md` §8 只写了 6 个主色（底/面板/分隔/主文字/次文字/强调）✓；
//  实际用到的是 **21 个不透明色 + 6 组 rgba** ✓ —— G-A-3 要把文档补齐到这个事实 ✓。
// ============================================================================
#pragma once

#include <QString>
#include <QtGlobal>

namespace theme {

// ---- §8 六个主色（文档里写过的那六个 ✓）------------------------------------
inline constexpr const char* kBg        = "#1E1F22";   ///< 页面底色（只给顶层窗口）
inline constexpr const char* kPanel     = "#2B2D31";   ///< 卡片/面板底色
inline constexpr const char* kDivider   = "#3A3D42";   ///< 分隔线、卡片描边
inline constexpr const char* kText      = "#E6E6E6";   ///< 主文字
inline constexpr const char* kTextDim   = "#9AA0A6";   ///< 次文字（标签、时间、元信息）
inline constexpr const char* kAccent    = "#7AA2F7";   ///< 强调（选中、主按钮、进度）

// ---- 状态色（`top_bar.cpp` 的链路/模式色 ✓）--------------------------------
inline constexpr const char* kOk        = "#4ADE80";   ///< 正常 / STUDY
inline constexpr const char* kWarn      = "#F59E0B";   ///< 警告 / GAME / 需要人管
inline constexpr const char* kOffline   = "#6B7280";   ///< 未连接 / SLEEP

// ---- 衍生色（实际在用的 ✓，逐条给出处，便于 G-C 收敛时判断去留）------------
inline constexpr const char* kTextFaint = "#6F757C";   ///< 更暗的提示文字（占位、禁用）
inline constexpr const char* kTextBare  = "#4A4E54";   ///< 空区域标题（几乎不可见）
inline constexpr const char* kLabelDim  = "#8A9099";   ///< 系统页标签列
inline constexpr const char* kTextSoft  = "#C9CED6";   ///< 浮层正文（B 站提示/输入）
inline constexpr const char* kPanelHover= "#35373B";   ///< 卡片/按钮 hover
inline constexpr const char* kInputBg   = "#232428";   ///< 输入框/列表底
inline constexpr const char* kBorderSoft= "#33363B";   ///< 浮层描边（比 kDivider 暗）
inline constexpr const char* kStageBg   = "#0B0C0E";   ///< 视频区黑底
inline constexpr const char* kOnAccent  = "#12141A";   ///< 压在强调色上的深色文字
inline constexpr const char* kSelBg     = "#4A5568";   ///< 文本框选区
inline constexpr const char* kBtnText2  = "#D6DAE0";   ///< 视频全屏按钮文字
/// ⚠ **待收敛**：这是 `kTextDim` 的漂移值（只差 3 个低位）✗ ——
///   G-C-0 会把它统一到 `kTextDim` ✓，**那会改像素**，所以要单独一项 + 单独对照 ✓。
inline constexpr const char* kTextDimDrift = "#9AA1A9";

// ---- 字号（10 档；G-C-2 计划收敛到 5 档 ✓）--------------------------------
inline constexpr int kFontXs   = 12;   ///< 唯一一处破档（`music_bar.cpp:168`）✗
inline constexpr int kFontSm   = 14;
inline constexpr int kFontSm2  = 15;
inline constexpr int kFontMd   = 16;
inline constexpr int kFontMd2  = 17;
inline constexpr int kFontLg   = 18;
inline constexpr int kFontLg2  = 19;
inline constexpr int kFontXl   = 20;
inline constexpr int kFontXxl  = 22;
inline constexpr int kFontHuge = 24;

// ---- 圆角与间距 -----------------------------------------------------------
// 圆角实际用到：4 / 5 / 6 / 8 / 10 / 20 / 28（QSS 与各控件 ✓）
inline constexpr int kRadiusSm  = 4;
inline constexpr int kRadiusMd  = 8;
inline constexpr int kRadiusLg  = 10;
inline constexpr int kRadiusPill= 20;
/// QSS 里所有 `padding/margin` 的**基准网格**：一律用它的整数倍 ✓（G-C 逐项核对 ✓）。
inline constexpr int kGrid = 4;

// ---- rgba 清单（**暂留字面量** ✗，但只许是上面基色的 α 变体 ✓）------------
//   rgba(43, 45, 49, α)   = kPanel  的 α 变体（顶栏/卡片半透明底）
//   rgba(20, 21, 24, α)   = kBg     的 α 变体（浮层底）
//   rgba(35, 36, 40, α)   = kInputBg 的 α 变体（输入类控件底）
//   rgba(53, 55, 59, α)   = kPanelHover 的 α 变体（hover 半透明）
//   rgba(122,162, 247, α) = kAccent 的 α 变体（选中态微光）
//   rgba(245,158, 11, α)  = kWarn   的 α 变体（警告底）
//   ⇒ 数值与上面常量一致，改基色时**必须**同步改这里（G-A-2 的守卫会核对 ✓）。

// ---- 令牌 → 常量（QSS 的单值来源）-----------------------------------------
//  QSS 是**字符串**，常量插不进去（`constexpr const char*` 不能做字面量拼接 ✗），
//  所以我们把 QSS 里的色值写成 `@token@`，再由 `styleSheet()` **按令牌替换** ✓。
//  好处：① 每个色值全仓只在这里出现一次 ✓；② 生成结果与原字面量**逐字节相同** ——
//  这是"零视觉变化"的**构造性证明** ✓✓（比截图可靠：截图受时钟/渲染非确定性干扰 ✗，
//  G-A-1b 实测连跑两次四页 sha256 全变 ✗）。
//  ⚠ 令牌**漏替换**会被 Qt 静默忽略（= 肉眼难查的回归 ✗✗）⇒ `styleSheet()` 会
//    对所有已知令牌做替换，`test_theme` 再断言产物里**不残留 `@`** ✓。
struct Token {
    const char* name;    ///< QSS 里的写法（不含 @）
    const char* value;   ///< 替换成的值
};

inline constexpr Token kTokens[] = {
    {"bg",         kBg},
    {"panel",      kPanel},
    {"divider",    kDivider},
    {"text",       kText},
    {"text_dim",   kTextDim},
    {"accent",     kAccent},
    {"ok",         kOk},
    {"warn",       kWarn},
    {"offline",    kOffline},
    {"text_faint", kTextFaint},
    {"text_bare",  kTextBare},
    {"label_dim",  kLabelDim},
    {"text_soft",  kTextSoft},
    {"panel_hover",kPanelHover},
    {"input_bg",   kInputBg},
    {"border_soft",kBorderSoft},
    {"stage_bg",   kStageBg},
    {"on_accent",  kOnAccent},
    {"sel_bg",     kSelBg},
    {"btn_text2",  kBtnText2},
    // ⚠ 漂移值单列一个令牌 ✗ —— G-C-0 才会把它并到 `text_dim`（那一步**会改像素**）
    {"text_dim_drift", kTextDimDrift},
    // 字号（QSS 里写成 `font-size: @font_md@px`）
    {"font_xs",   "12"},
    // ⚠ 13 是 G-A-2 的守卫**抓出来的漏网之鱼** ✗ —— 第一版令牌表只有 12/14/15/…/24 ✓，
    //   于是 `ui/base_style.h` 里那句 `font-size: 13px` 没被令牌化 ✓（不是视觉回归 ✗，
    //   因为它本来就是 13px ✓；但一致性漏了一档 ✓）。
    {"font_xs2",  "13"},
    {"font_sm",   "14"},
    {"font_sm2",  "15"},
    {"font_md",   "16"},
    {"font_md2",  "17"},
    {"font_lg",   "18"},
    {"font_lg2",  "19"},
    {"font_xl",   "20"},
    {"font_xxl",  "22"},
    {"font_huge", "24"},
};

/// 把 QSS 里的 `@token@` 全部换成常量值 ✓（逐字节等价 ⇒ 视觉零变化 ✓）。
inline QString styleSheet(const char* raw)
{
    QString out = QString::fromUtf8(raw);
    for (const Token& token : kTokens) {
        out.replace(QLatin1Char('@') + QLatin1String(token.name) + QLatin1Char('@'),
                    QLatin1String(token.value));
    }
    return out;
}

} // namespace theme
