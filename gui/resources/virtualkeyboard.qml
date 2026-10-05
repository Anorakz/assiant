// ============================================================================
//  gui/resources/virtualkeyboard.qml
//
//  Qt 虚拟键盘的 **Application 集成**（官方 Deployment Guide, Creating InputPanel）：
//  应用自己实例化 `InputPanel`，让键盘**渲染进应用自己的窗口**。
//
//  ⚠ 为什么必须这样（2026-10-05 板上实证 ✓）：
//    板上是 eglfs（无 X）⇒ **不支持多个顶层窗口** ✗
//    而 VK 的 **Desktop 集成**要求把键盘显示在**独立顶层窗口**里 ✗
//      ⇒ 结果：`setFocusObject(QLineEdit)` ✓ 与 `showInputPanel()` ✓ 都正常发生，
//        但 `IM_VISIBLE=0` ✗、`WINDOW_COUNT=1` ✗ —— **面板窗口根本建不出来** ✓
//    官方原文：在没有多顶层窗口支持的环境（嵌入式设备）上，Application 集成是 **mandatory** ✓
//
//  ★ 这份文件的内容与我在板上用 `qmlscene` 跑通的**最小验证**同构 ✓
//    （见 docs/gui.md §12.14 与截图 vk-min.png ✓）。
//
//  与 C++ 的接口 ✓：
//    · `kbHeight`  —— 键盘面板的实际高度（0 = 没显示）✓
//      C++ 侧用 `rootObject()->property("kbHeight")` 读它 ✓ 据此摆放承载用的 QQuickWidget ✓
//    · `kbVisible` —— `Qt.inputMethod.visible` ✓（同上，供 C++ 判断 ✓）
// ============================================================================

import QtQuick 2.0
import QtQuick.VirtualKeyboard 2.1

Item {
    id: root

    // 给 C++ 读的两个属性（见上方注释）
    readonly property int kbHeight: kb.height
    readonly property bool kbVisible: Qt.inputMethod.visible

    // 面板：按官方示例锚在**底部**，只在需要时露出来
    InputPanel {
        id: kb
        width: parent.width
        anchors.left: parent.left
        anchors.right: parent.right
        y: Qt.inputMethod.visible ? parent.height - kb.height : parent.height
    }
}
