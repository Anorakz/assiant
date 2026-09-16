// ============================================================================
//  input_sender.cpp — 输入接口实现 (stub / moonlight 双模式)
//
//  两种模式
//  ---------------------------------------------------------------------------
//  没定义 AGENT_HAVE_MOONLIGHT (host 构建):
//      只打印日志 + 计数, 不引用任何 moonlight 符号, 因此不需要链接它。
//
//  定义了 AGENT_HAVE_MOONLIGHT (交叉编译, 任务 6 打开):
//      真正调用 LiSendKeyboardEvent / LiSendMousePositionEvent /
//      LiSendMouseButtonEvent。链接 moonlight-common-c 由任务 6 处理。
//
//  两种模式下 **参数校验、夹取、组合键展开顺序完全共用** —— 也就是说
//  单测验证的行为在真机上是一样的, 不存在"stub 和真实现不一致"的问题。
// ============================================================================

#include "input_sender.h"

#include <algorithm>
#include <cstdio>

#ifdef AGENT_HAVE_MOONLIGHT
// moonlight-common-c 是 C 库
extern "C" {
#include "Limelight.h"
}
#endif

namespace agent {
namespace input_sender {
namespace {

bool g_log = true;
std::size_t g_events = 0;

/// 我们自己的常量取值与 moonlight 一致, 但仍显式对映一次 ——
/// 万一上游改了常量值, 这里会编译期/运行期立刻暴露, 而不是悄悄发错事件。
#ifdef AGENT_HAVE_MOONLIGHT
static_assert(kModifierShift == MODIFIER_SHIFT, "modifier 常量与 moonlight 不一致");
static_assert(kModifierCtrl == MODIFIER_CTRL, "modifier 常量与 moonlight 不一致");
static_assert(kModifierAlt == MODIFIER_ALT, "modifier 常量与 moonlight 不一致");
static_assert(kModifierMeta == MODIFIER_META, "modifier 常量与 moonlight 不一致");
static_assert(kButtonLeft == BUTTON_LEFT, "button 常量与 moonlight 不一致");
static_assert(kButtonMiddle == BUTTON_MIDDLE, "button 常量与 moonlight 不一致");
static_assert(kButtonRight == BUTTON_RIGHT, "button 常量与 moonlight 不一致");
#endif

/// 把坐标夹到参考平面内
int clamp_coord(int v, int hi) {
    if (v < 0) {
        return 0;
    }
    if (v > hi) {
        return hi;
    }
    return v;
}

}  // namespace

int clamp_mouse_x(int x) { return clamp_coord(x, kMouseRefWidth - 1); }
int clamp_mouse_y(int y) { return clamp_coord(y, kMouseRefHeight - 1); }

void set_log_enabled(bool enabled) { g_log = enabled; }
bool log_enabled() { return g_log; }
std::size_t event_count() { return g_events; }
void reset_event_count() { g_events = 0; }

void send_key(std::uint32_t modifier, std::uint32_t key, bool press) {
    ++g_events;

    // 键码 0 视为无效 (没有 VK 0); 不发出去, 只记账
    if (key == 0) {
        if (g_log) {
            std::fprintf(stderr, "[input] send_key: 忽略无效键码 0 (press=%d)\n",
                         press ? 1 : 0);
        }
        return;
    }

#ifdef AGENT_HAVE_MOONLIGHT
    LiSendKeyboardEvent(static_cast<short>(key),
                        press ? KEY_ACTION_DOWN : KEY_ACTION_UP,
                        static_cast<char>(modifier));
#else
    if (g_log) {
        std::fprintf(stderr, "[input] send_key: modifier=0x%02X key=0x%02X %s\n",
                     static_cast<unsigned>(modifier) & 0xFFu,
                     static_cast<unsigned>(key) & 0xFFu,
                     press ? "DOWN" : "UP");
    }
#endif
}

void send_mouse(int x, int y) {
    ++g_events;

    const int cx = clamp_mouse_x(x);
    const int cy = clamp_mouse_y(y);

#ifdef AGENT_HAVE_MOONLIGHT
    // 参考平面就是 vision 输出的 ROI, 所以调用方不用自己换算
    LiSendMousePositionEvent(static_cast<short>(cx),
                             static_cast<short>(cy),
                             static_cast<short>(kMouseRefWidth),
                             static_cast<short>(kMouseRefHeight));
#else
    if (g_log) {
        std::fprintf(stderr, "[input] send_mouse: (%d,%d) ref=%dx%d%s\n", cx, cy,
                     kMouseRefWidth, kMouseRefHeight,
                     (cx != x || cy != y) ? " [已夹取]" : "");
    }
#endif
}

void send_mouse_button(bool press, std::uint32_t button) {
    ++g_events;

    // 只允许已知按钮, 避免把野值发给主机
    const bool known = (button == kButtonLeft || button == kButtonMiddle ||
                        button == kButtonRight || button == kButtonX1 ||
                        button == kButtonX2);
    if (!known) {
        if (g_log) {
            std::fprintf(stderr, "[input] send_mouse_button: 忽略未知按钮 %u\n",
                         static_cast<unsigned>(button));
        }
        return;
    }

#ifdef AGENT_HAVE_MOONLIGHT
    LiSendMouseButtonEvent(press ? BUTTON_ACTION_PRESS : BUTTON_ACTION_RELEASE,
                           static_cast<int>(button));
#else
    if (g_log) {
        std::fprintf(stderr, "[input] send_mouse_button: button=%u %s\n",
                     static_cast<unsigned>(button), press ? "PRESS" : "RELEASE");
    }
#endif
}

void send_hotkey(const std::vector<std::uint32_t>& keys) {
    if (keys.empty()) {
        return;
    }

    // 按下: 正序; 抬起: 逆序。
    // 逆序是必须的 —— 否则 Ctrl+Shift+A 会先抬 Ctrl, 让 A 在 Shift 仍按下时
    // 变成另一个字符, 甚至被主机当成普通按键。
    for (std::size_t i = 0; i < keys.size(); ++i) {
        send_key(kModifierNone, keys[i], /*press=*/true);
    }
    for (std::size_t i = keys.size(); i-- > 0;) {
        send_key(kModifierNone, keys[i], /*press=*/false);
    }
}

}  // namespace input_sender
}  // namespace agent
