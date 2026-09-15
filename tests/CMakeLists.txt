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

add_executable(test_ring_buffer
    test_ring_buffer.cpp
)
target_include_directories(test_ring_buffer PRIVATE ${CMAKE_SOURCE_DIR}/native)
target_link_libraries(test_ring_buffer PRIVATE GTest::gtest_main Threads::Threads)

if(MSVC)
    target_compile_options(test_ring_buffer PRIVATE /W4)
else()
    target_compile_options(test_ring_buffer PRIVATE -Wall -Wextra)
endif()

include(GoogleTest)
gtest_discover_tests(test_ring_buffer)
