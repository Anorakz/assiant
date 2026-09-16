# ---------------------------------------------------------------- host 单测 ---
# 只在 host 构建时进来 (见根 CMakeLists.txt 的 AGENT_BUILD_TESTS)
#
# 注意: 这里 **不要** 定义 BUILD_TESTING。pybind11 在自己是 master project
# 且 BUILD_TESTING 有定义时会 add_subdirectory(tests), 于是开始拉 Catch2 / Eigen。
# 本项目用 AGENT_BUILD_TESTS 这个独立开关绕开它。

find_package(Threads REQUIRED)

# googletest: 只需要 gtest, 不要 gmock / install 规则, 减少编译量
set(BUILD_GMOCK OFF CACHE BOOL "" FORCE)
set(INSTALL_GTEST OFF CACHE BOOL "" FORCE)
add_subdirectory(../native/third_party/googletest
                 "${CMAKE_BINARY_DIR}/googletest-build" EXCLUDE_FROM_ALL)

include(GoogleTest)

# 每个单测一个可执行文件; 公共设置走这个 helper, 避免每加一个测试就抄一遍
function(agent_add_host_test name)
    add_executable(${name} ${ARGN})
    target_include_directories(${name} PRIVATE ${CMAKE_SOURCE_DIR}/native)
    target_link_libraries(${name} PRIVATE GTest::gtest_main Threads::Threads)
    if(MSVC)
        target_compile_options(${name} PRIVATE /W4)
    else()
        target_compile_options(${name} PRIVATE -Wall -Wextra)
    endif()
    gtest_discover_tests(${name})
endfunction()

# ring_buffer: 纯模板, 头文件即可
agent_add_host_test(test_ring_buffer
    test_ring_buffer.cpp
)

# image_rb: 有 .cpp, 一并编进来
agent_add_host_test(test_image_rb
    test_image_rb.cpp
    ../native/image_rb.cpp
)

# host_input_rb
agent_add_host_test(test_host_input_rb
    test_host_input_rb.cpp
    ../native/host_input_rb.cpp
)

# preprocess: YUV420P → 256×256 RGB888
agent_add_host_test(test_preprocess
    test_preprocess.cpp
    ../native/preprocess.cpp
)

# decoder: 骨架阶段只测 stub 契约 (不链接 FFmpeg)
agent_add_host_test(test_decoder
    test_decoder.cpp
    ../native/decoder.cpp
)

# input_sender: stub 模式, 不链接 moonlight-common-c
agent_add_host_test(test_input_sender
    test_input_sender.cpp
    ../native/input_sender.cpp
)

# moonlight_connection: HTTP + XML 握手层 (纯 host, 不依赖 moonlight 库)
agent_add_host_test(test_moonlight_connection
    test_moonlight_connection.cpp
    ../native/moonlight_connection.cpp
)
if(WIN32)
    target_link_libraries(test_moonlight_connection PRIVATE ws2_32)
endif()

# moonlight_adapter: 状态机 + 参数校验 (host 不链接 moonlight)
agent_add_host_test(test_moonlight_adapter
    test_moonlight_adapter.cpp
    ../native/moonlight_adapter.cpp
    ../native/moonlight_connection.cpp
    ../native/decoder.cpp
    ../native/image_rb.cpp
    ../native/host_input_rb.cpp
    ../native/preprocess.cpp
)
if(WIN32)
    target_link_libraries(test_moonlight_adapter PRIVATE ws2_32)
endif()
