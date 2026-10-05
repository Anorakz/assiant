// ============================================================================
//  gui/src/ui/base_style.h — 主样式表（T15-16 / G-A-1b）
//
//  为什么单独一个头（而不是留在 main_window.cpp）
//  ---------------------------------------------------------------------------
//  它原来是 `main_window.cpp` **匿名 namespace** 里的一份 `kBaseStyle` ✗ ——
//  于是"改前改后样式逐字节一致"这件事**测试看不见它** ✗（只能靠截图，而截图
//  受时钟/渲染非确定性干扰，实测连跑两次四页 sha256 全变 ✗）。
//  挪到这里之后 `test_theme.cpp` 可以直接 include 它并断言黄金长度/sha256 ✓✓。
//
//  内容约定
//  ---------------------------------------------------------------------------
//  色值与字号写成 `@token@`，由 `theme::styleSheet()`（见 ui/theme.h）展开 ✓；
//  **令牌一个都不许漏** —— 漏掉的会被 Qt 静默忽略（肉眼难查的回归 ✗），
//  所以 test_theme 有一条"展开后不许残留 @"的守卫 ✓。
// ============================================================================
#pragma once

namespace theme {

/// 主样式表（`@token@` 由 `theme::styleSheet()` 展开 ✓）。
inline constexpr const char* kBaseQss = R"(
QWidget { color: @text@; font-size: @font_xl@px; }
/* ⚠ 底色只给顶层窗口：子控件一律默认透明，卡片自己的 @panel@ 才能透出来。
   以前写成 `QWidget { background: @bg@ }` 会把卡片内部刷成页面底色
   （T7 量色带时发现音乐卡内部不是面板色）。 */
QMainWindow { background: @bg@; }
QStackedWidget#PageStack { background: transparent; }
QFrame#TopBar { background: rgba(43, 45, 49, 0.90); border: none; border-bottom: 1px solid @divider@; }
QFrame#NavBar { background: rgba(35, 36, 40, 0.90); border: none; border-radius: 12px; }
/* ⚠ 半透明：方案 §3 的全局壁纸要能透过面板看到（T8） */
QFrame#AreaFrame { background: rgba(43, 45, 49, 0.90); border: 1px solid @divider@; border-radius: 8px; }
/* 主区：非游戏模式不画背景与边框，完全留给全局壁纸（T8 接壁纸后即可见效果） */
QFrame#AreaFrameBare { background: transparent; border: none; }
QLabel#AreaTitle { color: @text_dim@; font-size: @font_xl@px; font-weight: bold; background: transparent; }
QLabel#AreaHint { color: @text_faint@; font-size: @font_sm2@px; background: transparent; }
/* S5：日程区。时间列等宽一点（用同一档字号 + 固定最小宽度，见 SchedulePanel），
   标题比提示亮一档 —— 和对话区气泡的层次保持一致。 */
QLabel#ScheduleTime { color: @text_dim@; font-size: @font_md@px; background: transparent; }
QLabel#ScheduleTitle { color: @text@; font-size: @font_md2@px; background: transparent; }
/* 主区占位文字（T8/T9 落地后删除），做得很淡以免干扰壁纸 */
QLabel#AreaTitleBare { color: @text_bare@; font-size: @font_lg@px; background: transparent; }
QLabel#AreaHintBare { color: @divider@; font-size: @font_sm@px; background: transparent; }
QLabel#TopBarText { color: @text_dim@; font-size: @font_lg@px; background: transparent; }
QLabel#TopBarClock { color: @text@; font-size: @font_xl@px; background: transparent; }
QPushButton#NavButton {
    background: transparent; border: none; color: @text_dim@;
    /* T14：加了图标后 96px 宽里要放"图标+4 个汉字"，字号与内边距都得收 */
    padding: 14px 2px; font-size: @font_sm2@px; text-align: center;
}
QPushButton#NavButton:hover { color: @text@; }
QPushButton#NavButton:checked {
    color: @text@; background: @panel@; border-left: 4px solid @accent@;
}
/* T5：模式切换按钮区 */
QPushButton#ModeButton {
    background: @panel@; border: 1px solid @divider@; border-radius: 8px;
    padding: 4px 10px; font-size: @font_lg@px; color: @text@; text-align: center;
}
QPushButton#ModeButton:hover { background: @panel_hover@; }
QPushButton#ModeButton:pressed { background: @accent@; color: @bg@; }
/* T5：对话区 */
/* ⚠ 这条必须写在气泡规则之前并保持"只清列表背景"的语义：
   它的特异度是 (0,1,2)，写成 QWidget#ChatList QWidget 会盖掉 (0,1,1) 的气泡背景，
   导致用户气泡变透明 + 深色文字看不见（T5 出图时踩到）。所以气泡规则要带祖先前缀。 */
QWidget#ChatList, QWidget#ChatList QWidget { background: transparent; }
QWidget#ChatList QLabel#ChatBubbleUser { background: @accent@; color: @bg@; border-radius: 10px;
                                         padding: 10px 12px; font-size: @font_md2@px; }
QWidget#ChatList QLabel#ChatBubbleAssistant { background: @panel_hover@; color: @text@; border-radius: 10px;
                                              padding: 10px 12px; font-size: @font_md2@px; }
QLabel#ChatSystem { color: @text_dim@; font-size: @font_sm2@px; background: transparent; }
QLabel#LinkBanner { background: @warn@; color: @bg@; padding: 6px 10px;
                    border-radius: 6px; font-size: @font_sm2@px; }
QLineEdit#ChatInput { background: @input_bg@; border: 1px solid @divider@; border-radius: 8px;
                      padding: 8px 12px; color: @text@; font-size: @font_lg@px; }
QPushButton#ChatSend { background: @accent@; color: @bg@; border: none; border-radius: 8px;
                       padding: 8px 16px; font-size: @font_lg@px; font-weight: bold; }
QPushButton#ChatSend:disabled { background: @divider@; color: @text_faint@; }
/* T6：输入源小按钮 */
QToolButton#InputTypeButton {
    background: @input_bg@; border: 1px solid @divider@; border-radius: 8px;
    padding: 6px 10px; color: @text@; font-size: @font_md@px;
}
QToolButton#InputTypeButton:hover { background: @panel@; }
QToolButton#InputTypeButton::menu-indicator { image: none; }
QScrollArea#ChatScroll { background: transparent; border: none; }
/* T14-7b：模型测试页整页在滚动区里 → 视口与内容都要透明，壁纸才透得出来 */
QScrollArea#ModelScroll, QScrollArea#ModelScroll > QWidget { background: transparent; border: none; }
/* T14-9：设置页的网络卡片里的 SSID 列表 */
QListWidget#WifiList {
    background: @bg@; border: 1px solid @divider@; border-radius: 6px;
    color: @text@; font-size: @font_md@px;
}
QListWidget#WifiList::item { padding: 6px 8px; }
QListWidget#WifiList::item:selected { background: @accent@; color: @bg@; }
/* T7：下区域音乐条 */
QLabel#MusicTitle { color: @text@; font-size: @font_xl@px; font-weight: bold; background: transparent; }
QLabel#MusicPlaceholder { color: @text_faint@; font-size: @font_md@px; background: transparent; }
QLabel#MusicState { background: transparent; }
QLabel#PlaceholderTag {
    background: @divider@; color: @text_dim@; border-radius: 4px;
    padding: 1px 6px; font-size: @font_xs2@px;
}
QProgressBar#MusicProgress {
    background: @input_bg@; border: 1px solid @divider@; border-radius: 5px;
}
QProgressBar#MusicProgress::chunk { background: @accent@; border-radius: 4px; }
/* T7 调整：控制按钮（协议无控制命令，点了只给"未接入"说明） */
QPushButton#MusicCtl {
    background: @panel@; border: 1px solid @divider@; border-radius: 10px;
    padding: 0; color: @text@; font-size: @font_lg@px;
}
QPushButton#MusicCtl:hover { background: @panel_hover@; }
QPushButton#MusicCtlMain {
    background: @panel@; border: 1px solid @accent@; border-radius: 28px;
    padding: 0; color: @text@; font-size: @font_huge@px;
}
QPushButton#MusicCtlMain:hover { background: @panel_hover@; }
QWidget#CoverPage { background: transparent; }
/* T11：模型测试页 */
QRadioButton#ModeRadio { color: @text@; font-size: @font_md2@px; spacing: 8px; }
QPlainTextEdit#ModelLog {
    background: rgba(20, 21, 24, 0.72); border: 1px solid @border_soft@; border-radius: 8px;
    color: @text_soft@; font-size: @font_sm@px; padding: 6px;
}
QLabel#BenchBanner {
    background: rgba(245, 158, 11, 0.16); border: 1px solid @warn@; border-radius: 6px;
    color: @warn@; padding: 6px 10px; font-size: @font_sm2@px;
}
QSpinBox, QDoubleSpinBox, QLineEdit {
    background: rgba(35, 36, 40, 0.9); border: 1px solid @divider@; border-radius: 6px;
    color: @text@; padding: 4px 8px; font-size: @font_sm2@px; selection-background-color: @sel_bg@;
}
QSpinBox:disabled, QDoubleSpinBox:disabled { color: @text_faint@; }
QComboBox {
    background: rgba(35, 36, 40, 0.92); border: 1px solid @divider@; border-radius: 6px;
    color: @text@; padding: 4px 8px; font-size: @font_sm2@px; min-height: 24px;
}
QComboBox::drop-down { border: none; width: 22px; }
QComboBox QAbstractItemView {
    background: @input_bg@; color: @text@; border: 1px solid @divider@;
    selection-background-color: @divider@; font-size: @font_sm2@px;
}
QCheckBox { color: @text@; font-size: @font_md@px; spacing: 8px; }
QComboBox#ModelCombo {
    background: rgba(35, 36, 40, 0.9); border: 1px solid @divider@; border-radius: 6px;
    color: @text@; padding: 4px 8px; font-size: @font_sm2@px;
}
/* 系统页的看门狗按钮（T7-3：原先是借「下一张」那条规则的，现在自己有一条） */
QPushButton#WatchdogButton {
    background: rgba(43, 45, 49, 0.85); border: 1px solid @divider@; border-radius: 8px;
    padding: 6px 14px; color: @text_dim@; font-size: @font_md@px;
}
QPushButton#WatchdogButton:hover { color: @text@; background: rgba(53, 55, 59, 0.92); }
/* T9：视频区 */
QStackedWidget#VideoStage { background: transparent; }
QLabel#VideoPlaceholder { color: @text_faint@; font-size: @font_xxl@px; background: transparent; }
QPushButton#VideoCtl {
    background: rgba(20, 21, 24, 0.72); border: 1px solid rgba(255, 255, 255, 0.10);
    border-radius: 8px; padding: 0; color: @text@; font-size: @font_lg@px;
}
QPushButton#VideoCtl:hover { background: rgba(40, 42, 47, 0.86); }
QPushButton#VideoCtlPlay {
    background: rgba(122, 162, 247, 0.82); border: 1px solid rgba(255, 255, 255, 0.14);
    border-radius: 20px; padding: 0; color: @on_accent@; font-size: @font_lg@px; font-weight: bold;
}
QPushButton#VideoCtlPlay:hover { background: rgba(140, 176, 250, 0.92); }
QToolButton#VideoSpeed {
    background: rgba(20, 21, 24, 0.72); border: 1px solid rgba(255, 255, 255, 0.10);
    border-radius: 8px; padding: 0; color: @btn_text2@; font-size: @font_md@px;
}
QToolButton#VideoSpeed:hover { background: rgba(40, 42, 47, 0.86); }
QToolButton#VideoSpeed::menu-indicator { image: none; }
/* T11-7：预览栏 + 地址栏 + 下区域封面 */
QWidget#BilibiliPreview { background: transparent; }
QLabel#BilibiliCaption { color: @text_dim@; font-size: @font_sm@px; background: transparent; }
QLineEdit#BilibiliAddress {
    background: rgba(20, 21, 24, 0.72); border: 1px solid @divider@; border-radius: 6px;
    color: @text_soft@; font-size: @font_sm@px; padding: 2px 8px;
}
QLineEdit#BilibiliAddress:read-only { color: @text_dim@; }
QLabel#BilibiliSource { color: @accent@; font-size: @font_sm@px; background: transparent; }
QLabel#BilibiliHint { color: @text_faint@; font-size: @font_sm2@px; background: transparent; padding-left: 4px; }
QListWidget#BilibiliList {
    background: transparent; border: none; outline: none;
}
QListWidget#BilibiliList::item {
    background: rgba(20, 21, 24, 0.62); border: 1px solid @border_soft@; border-radius: 8px;
    color: @text_soft@; font-size: @font_xs@px; padding: 2px;
}
QListWidget#BilibiliList::item:selected {
    border: 1px solid @accent@; background: rgba(122, 162, 247, 0.18); color: @text@;
}
QWidget#BilibiliCover { background: transparent; }
QLabel#BilibiliCoverImage {
    background: rgba(20, 21, 24, 0.62); border: 1px solid @border_soft@; border-radius: 8px;
    color: @text_faint@; font-size: @font_sm2@px;
}
QLabel#BilibiliCoverTitle { color: @text@; font-size: @font_lg2@px; font-weight: bold; background: transparent; }
QLabel#BilibiliCoverMeta { color: @text_dim@; font-size: @font_sm2@px; background: transparent; }
QLabel#BilibiliCoverPosition { color: @text_dim@; font-size: @font_sm@px; background: transparent; }
QLabel#BilibiliCoverSource { color: @accent@; font-size: @font_sm@px; background: transparent; }
/* 内嵌控制条：**没有整条背景**，只有一个半透明胶囊（验收：不要实体化） */
QWidget#VideoOverlay { background: transparent; }
QWidget#VideoPill {
    background: rgba(20, 21, 24, 0.42); border-radius: 22px;
}
QWidget#VideoPage { background: @stage_bg@; }
QStackedWidget#VideoStage { background: @stage_bg@; }
/* T10：系统页指标瓷砖 */
QFrame#SysTile {
    background: rgba(35, 36, 40, 0.72); border: 1px solid @border_soft@; border-radius: 8px;
}
QLabel#SysValue { color: @text@; font-size: @font_xxl@px; font-weight: bold; background: transparent; }
QLabel#SysValueSmall { color: @text@; font-size: @font_md2@px; background: transparent; }
QLabel#SysLabel { color: @label_dim@; font-size: @font_sm2@px; background: transparent; }
)";

} // namespace theme
