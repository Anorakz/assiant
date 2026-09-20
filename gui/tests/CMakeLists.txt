# ============================================================================
#  gui/tests/CMakeLists.txt — LocalClient 单测 (Qt5 Test / QTest)
#
#  对端是 Python 的 local_server.py: 测试自己用 QProcess 拉起它, 不用 mock。
#  从 gui/CMakeLists.txt 用 add_subdirectory(tests) 进来 (开关 GUI_BUILD_TESTS)。
#
#  跑法
#  ---------------------------------------------------------------------------
#      cmake -S gui -B gui/build && cmake --build gui/build -j4
#      cd gui/build && ctest --output-on-failure
#      # 或直接跑: ./gui/build/tests/test_local_client
# ============================================================================

find_package(Qt5 5.12 REQUIRED COMPONENTS Test)

add_executable(test_local_client
    test_local_client.cpp
)

target_link_libraries(test_local_client PRIVATE
    gui_services        # LocalClient
    Qt5::Test           # QTest / QSignalSpy / QTRY_*
)

# Python 对端脚本的绝对路径。测试进程的 cwd 不一定是源码目录, 所以编译期写死;
# 脚本不存在时测试会 QSKIP 而不是跑出一堆看不懂的失败。
target_compile_definitions(test_local_client PRIVATE
    LOCAL_SERVER_SCRIPT="${CMAKE_CURRENT_SOURCE_DIR}/local_server.py"
)

if(MSVC)
    target_compile_options(test_local_client PRIVATE /W4)
else()
    target_compile_options(test_local_client PRIVATE -Wall -Wextra)
endif()

add_test(NAME test_local_client COMMAND test_local_client)

# --------------------------------------------------- T2: core 层单测 ---
# 配置读写（文本级键路径替换）/ 配置同步（gui.yaml → llm.env + config.yaml）/
# 协议→显示状态。都不需要 Widgets，也不碰仓库里的真实配置文件。
foreach(core_test test_config_store test_config_sync test_view_state test_idle_watcher test_onboard_ctl test_image_fit test_system_stats)
    add_executable(${core_test} ${core_test}.cpp)
    target_link_libraries(${core_test} PRIVATE
        gui_services        # ConfigStore / ConfigSyncer / ViewState
        Qt5::Test
    )
    if(MSVC)
        target_compile_options(${core_test} PRIVATE /W4)
    else()
        target_compile_options(${core_test} PRIVATE -Wall -Wextra)
    endif()
    add_test(NAME ${core_test} COMMAND ${core_test})
    set_tests_properties(${core_test} PROPERTIES TIMEOUT 60)
endforeach()

# ------------------------------------------------- T5/T7: 控件级测试 ---
# 真的点控件（输入框 / 发送按钮 / 模式按钮 / 音乐条占位块），所以需要
# QApplication + Widgets，跑的时候用 offscreen 平台插件。
foreach(gui_test test_main_page test_bottom_bar test_video_panel test_sys_page test_model_page test_bench_runner test_settings_page)
    add_executable(${gui_test} ${gui_test}.cpp)
    # 控件测试里用到图标（VideoPanel/MusicBar）→ 把资源一起编进来
    target_sources(${gui_test} PRIVATE ../resources/icons.qrc)
    target_link_libraries(${gui_test} PRIVATE
        gui_widgets          # 控件本体都在这里
        gui_services
        Qt5::Widgets
        Qt5::Test
    )
    if(MSVC)
        target_compile_options(${gui_test} PRIVATE /W4)
    else()
        target_compile_options(${gui_test} PRIVATE -Wall -Wextra)
    endif()
    add_test(NAME ${gui_test} COMMAND ${gui_test})
    set_tests_properties(${gui_test} PROPERTIES
        TIMEOUT 60
        ENVIRONMENT "QT_QPA_PLATFORM=offscreen"
    )
endforeach()

# ---------------------------------------------------------------- 双向联调 ---
# 任务4: 起 Python IPC server + C++ GUI (headless), push status 再发 switch_mode。
# 用独立 socket 路径, 免得和真 Agent / 别的测试抢 /tmp/agent.sock。
find_package(Python3 COMPONENTS Interpreter QUIET)
if(Python3_Interpreter_FOUND)
    add_test(NAME e2e_ipc
             COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/e2e_ipc.py
                     --gui $<TARGET_FILE:agent_gui>
                     --socket /tmp/agent_e2e_ctest.sock)
    set_tests_properties(e2e_ipc PROPERTIES TIMEOUT 60)
endif()

# 每个用例都要起/杀 python 进程, 给足超时; 卡死时 ctest 不会一直挂着。
# (不设 TIMEOUT_SIGNAL_NAME: cmake 3.16 还不支持这个属性)
set_tests_properties(test_local_client PROPERTIES
    TIMEOUT 60
)
