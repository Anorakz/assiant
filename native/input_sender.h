// ============================================================================
//  input_sender.h — 把"AI 想做的输入动作"翻译成 moonlight-common-c 调用
//
//  架构位置 (docs/architecure.md §3.2)
//  ---------------------------------------------------------------------------
//      Python Agent → pybind11: send_key / send_mouse / send_hotkey
//        → **input_sender** → LiSendKeyboardEvent / LiSendMouseEvent
//        → Sunshine → Windows 主机执行
//
//  为什么走"直调"而不是环形缓冲
//  ---------------------------------------------------------------------------
//  输入事件稀疏 (每秒几次到几十次), 不需要缓冲; 直调延迟最低。
//  另外这些函数必须 **释放 GIL** (在 pybind11 绑定层加
//  py::call_guard<py::gil_scoped_release>), 否则会阻塞板端 asyncio。
//
//  ⚠ 当前状态: **stub**
//  ---------------------------------------------------------------------------
//  默认 (host) 构建只把调用内容打到 stderr, 不触碰 moonlight-common-c。
//  交叉编译时如果定义了 AGENT_HAVE_MOONLIGHT, 就会真正调用 Li* 函数。
//  真正的集成 (链接 moonlight-common-c) 在任务 6。
//
//  这样切的好处: 接口、常量、参数范围现在就能定死并单测, 任务 6 只需要
//  打开一个宏并处理链接, 不用回头改签名。
//
//  键码约定 (重要)
//  ---------------------------------------------------------------------------
//  moonlight 的 LiSendKeyboardEvent(keyCode, keyAction, modifiers) 直接把
//  **Windows 虚拟键码 (VK)** 原样发给主机, 中间不做转换。所以本模块的
//  key 参数也是 VK 码:
//
//      'A'..'Z' '0'..'9'  → 直接用 ASCII (VK 值与 ASCII 一致)
//      VK_ESCAPE 0x1B, VK_RETURN 0x0D, VK_SPACE 0x20, VK_TAB 0x09
//      VK_LEFT 0x25 / VK_UP 0x26 / VK_RIGHT 0x27 / VK_DOWN 0x28
//      VK_F1..F12 0x70..0x7B
//
//  ⚠ **不要** 直接传 UTF-16 字符给非 ASCII 按键 (例如 'L' 没问题, 但
//    中文/韩文这类字符高位字节会被截断成 0xE0, 映射到错误的 VK)。
//    要输入文本请用 LiSendUtf8TextEvent (后续按需补一个 send_text)。
//
//  修饰键: modifier 是位掩码, 可以组合 (MODIFIER_CTRL | MODIFIER_SHIFT)
// ============================================================================

#pragma once

#include <cstddef>
#include <cstdint>
#include <vector>

namespace agent {
namespace input_sender {

/// 修饰键位掩码 —— 取值与 moonlight 的 MODIFIER_* 完全一致, 便于直接透传
enum Modifier : std::uint32_t {
    kModifierNone = 0x00,
    kModifierShift = 0x01,
    kModifierCtrl = 0x02,
    kModifierAlt = 0x04,
    kModifierMeta = 0x08,  ///< Windows 键 / Command 键
};

/// 鼠标按钮 —— 取值与 moonlight 的 BUTTON_* 一致
enum MouseButton : std::uint32_t {
    kButtonLeft = 0x01,
    kButtonMiddle = 0x02,
    kButtonRight = 0x03,
    kButtonX1 = 0x04,
    kButtonX2 = 0x05,
};

/// 鼠标坐标是相对于这个参考平面的 —— 也就是 vision 模块输出的 ROI 尺寸
inline constexpr int kMouseRefWidth = 256;
inline constexpr int kMouseRefHeight = 256;

// ---------------------------------------------------------------------------
//  发送接口
// ---------------------------------------------------------------------------

/// 发送一次按键
/// @param modifier 修饰键位掩码 (kModifier* 的按位或)
/// @param key      Windows 虚拟键码 (见文件头"键码约定")
/// @param press    true = 按下, false = 抬起
void send_key(std::uint32_t modifier, std::uint32_t key, bool press);

/// 把鼠标移动到 ROI 坐标系里的绝对位置
/// @param x 0..255, 会被夹到这个范围
/// @param y 0..255, 会被夹到这个范围
///
/// @note 底层是 LiSendMousePositionEvent(x, y, 256, 256), 参考平面就是
///       vision 输出的 ROI, 所以调用方不用自己换算。
/// @note moonlight 文档明确提示: 绝对定位在部分游戏里不可靠 (游戏常读相对
///       位移)。若发现移动无效, 需要在任务 6 之后换成相对位移方案。
void send_mouse(int x, int y);

/// 发送一次鼠标按钮事件
/// @param press  true = 按下, false = 抬起
/// @param button kButton* 之一
void send_mouse_button(bool press, std::uint32_t button);

/// 发送组合键 (例如 Ctrl+Alt+S)
///
/// 会按顺序把 keys 逐个按下, 再按 **相反顺序** 逐个抬起 —— 这样嵌套组合
/// 也能正确展开, 且不会出现"修饰键先抬起导致按键变成普通字符"的问题。
///
/// ⚠ keys 里必须 **自己带上修饰键的 VK 码**, 例如 Ctrl+Alt+S 是
///   {VK_CONTROL(0x11), VK_MENU(0x12), 'S'}。send_key() 的 modifier 位掩码
///   只是随事件捎带的一个提示位 (告诉主机"此刻修饰键是被按住的"), 它本身
///   不会产生按键事件; 主机要的是真正的 VK_CONTROL 按下/抬起。
///
/// @param keys 键码序列 (VK 码); 空序列不做任何事
void send_hotkey(const std::vector<std::uint32_t>& keys);

/// 把鼠标坐标夹到 ROI 参考平面内 (0..kMouseRefWidth-1)
/// send_mouse() 内部就用的这个; 暴露出来是为了能直接单测, 调用方也可以
/// 用它先做校验
int clamp_mouse_x(int x);
int clamp_mouse_y(int y);

// ---------------------------------------------------------------------------
//  stub 观测 (仅用于单测/调试)
// ---------------------------------------------------------------------------

/// 是否打印每次调用 (默认开; 单测里可关掉免得刷屏)
void set_log_enabled(bool enabled);
bool log_enabled();

/// 自进程启动以来累计发送的事件数 (stub 模式下由本地计数维护)
std::size_t event_count();

/// 清空计数 (单测用)
void reset_event_count();

}  // namespace input_sender
}  // namespace agent
