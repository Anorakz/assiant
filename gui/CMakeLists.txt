# ============================================================================
#  gui/CMakeLists.txt — 板端 C++ / Qt5 GUI 工程
#
#  可以单独构建 (验收用):
#      cmake -S gui -B gui/build && cmake --build gui/build -j4
#      ./gui/build/agent_gui --windowed --page home --screenshot /tmp/x.png
#      ./gui/build/agent_gui --stdio                 # 纯终端模式
#
#  目录
#      src/services/   IPC 等服务层 (只依赖 QtCore/QtNetwork)
#      src/ui/         页面与区域 (pages.*)
#      src/main_window.* Widgets 主窗口框架
#      src/main.cpp    入口 (GUI 默认 / --stdio 保留终端行为)
#      tests/          QTest 单测 (Python local_server.py 当真对端)
#      tools/          假 Agent (fake_agent.py) —— 本地联调用
# ============================================================================
cmake_minimum_required(VERSION 3.16)

project(agent_gui
    VERSION 0.1.0
    DESCRIPTION "RK3568 桌面助手 GUI (Qt5)"
    LANGUAGES CXX
)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

# Q_OBJECT / .ui / .qrc 一律交给 CMake 自动处理 (local_client.h 里有 Q_OBJECT)
set(CMAKE_AUTOMOC ON)
set(CMAKE_AUTOUIC ON)
set(CMAKE_AUTORCC ON)

# 板端 Qt 5.12; 用 REQUIRED 让版本不足时在配置阶段就报清楚,
# 而不是编译到一半找不到符号。
find_package(Qt5 5.12 REQUIRED COMPONENTS Core Network Widgets Multimedia MultimediaWidgets)

# yaml-cpp: 只给 core/schedule_model.cpp **读** config.yaml 的 scheduler 段用
#   (日程区)。写路径仍然只有 ConfigStore 的文本级替换 —— yaml-cpp 会重排+丢注释,
#   绝不参与写回。板端安装: apt-get install -y libyaml-cpp-dev (0.6.2)
find_package(yaml-cpp REQUIRED)
# 0.6.x 的 imported target 是 `yaml-cpp`; 0.7+ 才是 `yaml-cpp::yaml-cpp`
if(TARGET yaml-cpp::yaml-cpp)
    set(YAMLCPP_TARGET yaml-cpp::yaml-cpp)
elseif(TARGET yaml-cpp)
    set(YAMLCPP_TARGET yaml-cpp)
else()
    message(FATAL_ERROR "找到了 yaml-cpp 但拿不到可用的 imported target")
endif()
message(STATUS "yaml-cpp: ${yaml-cpp_VERSION} -> ${YAMLCPP_TARGET}")

# ------------------------------------------------------------------ 服务层 ---
# 非 Widgets 的逻辑层：IPC 客户端 + 配置读写/同步 + 协议→显示状态。
# 只依赖 QtCore / QtNetwork，因此能被单测直接链接（不需要起界面）。
# 新增服务文件往下面这个列表里加。
add_library(gui_services STATIC
    src/services/local_client.h
    src/services/local_client.cpp
    src/services/onboard_ctl.h
    src/services/onboard_ctl.cpp
    src/core/config_store.h
    src/core/config_store.cpp
    # T13-9: B 站凭据文件（config/bilibili_cookie.json）的读写。与 ConfigStore 分开:
    # 它写的不是配置真源, 是凭据（整份 JSON + .bak + 原子写 + 只认三个键）。
    # ⚠ T14-3 起 GUI 也不写它了（凭据走 IPC 让 Agent 写）—— 这里只剩"读 + 掩码"。
    src/core/cookie_store.h
    src/core/cookie_store.cpp
    src/core/view_state.h
    src/core/view_state.cpp
    src/core/bilibili_format.h
    src/core/bilibili_format.cpp
    src/core/idle_watcher.h
    src/core/idle_watcher.cpp
    src/core/lyrics.h
    src/core/image_fit.h
    src/core/image_fit.cpp
    src/core/system_stats.h
    src/core/system_stats.cpp
    src/core/crash_log.h
    src/core/crash_log.cpp
    src/core/schedule_model.h
    src/core/schedule_model.cpp
)
target_include_directories(gui_services PUBLIC ${CMAKE_CURRENT_SOURCE_DIR}/src)
target_link_libraries(gui_services PUBLIC Qt5::Core Qt5::Network)
# PRIVATE: schedule_model.h 只暴露 Qt 类型, yaml-cpp 不外泄到使用方
target_link_libraries(gui_services PRIVATE ${YAMLCPP_TARGET})

# ------------------------------------------------------------------ 界面层 ---
# 可复用控件做成静态库：主程序与「控件级单测」都能链接（单测要真的点按钮）。
add_library(gui_widgets STATIC
    src/ui/pages.h
    src/ui/pages.cpp
    src/ui/region_host.h
    src/ui/region_host.cpp
    src/ui/top_bar.h
    src/ui/top_bar.cpp
    src/ui/mode_panel.h
    src/ui/mode_panel.cpp
    src/ui/chat_panel.h
    src/ui/chat_panel.cpp
    src/ui/schedule_panel.h
    src/ui/schedule_panel.cpp
    src/ui/music_bar.h
    src/ui/music_bar.cpp
    src/ui/bottom_bar.h
    src/ui/bottom_bar.cpp
    src/ui/video_panel.h
    src/ui/video_panel.cpp
    src/ui/bilibili_preview.h
    src/ui/bilibili_preview.cpp
    src/ui/bilibili_cover.h
    src/ui/bilibili_cover.cpp
    src/ui/cover_loader.h
    src/ui/cover_loader.cpp
    src/ui/sys_page.h
    src/ui/sys_page.cpp
    src/ui/model_page.h
    src/ui/model_page.cpp
    src/ui/settings_page.h
    src/ui/settings_page.cpp
    src/ui/icons.h
    src/ui/icons.cpp
)
target_include_directories(gui_widgets PUBLIC ${CMAKE_CURRENT_SOURCE_DIR}/src)
target_link_libraries(gui_widgets PUBLIC gui_services Qt5::Widgets Qt5::Multimedia Qt5::MultimediaWidgets)

# ------------------------------------------------------------------ 可执行 ---
# 阶段 3: Qt5 Widgets 主窗口 (上区域通栏 + 左区域导航 + 页面栈)。
#   main_window.* 装配各控件并接 IPC；控件本体在 gui_widgets 里。
#   --stdio 仍保留 T1 之前的纯终端验收行为，e2e_ipc 测试依赖它。
add_executable(agent_gui
    src/main.cpp
    src/main_window.h
    src/main_window.cpp
    resources/icons.qrc          # T14：单色 SVG 图标集（22 个）
)
target_link_libraries(agent_gui PRIVATE gui_services gui_widgets)

# ⚠ T14-3 删掉了 `gui_config_sync`（配置同步 CLI）与 `core/config_sync.*`：
#   llm.env 的派生实现搬到了 Python（agent/core/llm_env.py），
#   而 config.yaml 的唯一写入者是 Agent（docs/adr/0005）。
#   要预览/真写派生文件：`assistant doctor`（只看）或让 Agent 走 set_config。

# ------------------------------------------------------------------ 单测 ---
# QTest 单测 (gui/tests/): 用 Python 的 local_server.py 当真对端, 不需要 mock。
# 关掉的办法: cmake -S gui -B gui/build -DGUI_BUILD_TESTS=OFF
option(GUI_BUILD_TESTS "构建 gui 的 QTest 单测 (需要 Qt5Test)" ON)

if(GUI_BUILD_TESTS)
    enable_testing()
    add_subdirectory(tests)
endif()

# ------------------------------------------------------------------ 告警 ---
# 两个 target 用同一套告警级别, 避免以后加 target 时漏掉。
foreach(gui_target gui_services gui_widgets agent_gui)
    if(MSVC)
        target_compile_options(${gui_target} PRIVATE /W4)
    else()
        target_compile_options(${gui_target} PRIVATE -Wall -Wextra)
    endif()
endforeach()
