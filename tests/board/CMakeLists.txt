# ============================================================================
#  tests/board/CMakeLists.txt — 板端测试用扩展 (只在交叉编译时构建)
#
#  产出 agent_native_test：往 agent_native 的 image_rb 里推已知图案的帧，
#  好让 tests/board/test_image_rb_numpy.py 能在真机上核对 numpy 数据通路。
#
#  单独走 CMake 而不是手写 g++ 命令，是因为 sysroot 的 include/lib 搜索路径
#  (尤其是 python3.8 的 pyconfig.h 与多架构目录) 由 cmake/toolchain.cmake 统一
#  提供，手写极易漏掉 -B/-L/-isystem 而误用工具链自带的头文件。
# ============================================================================

pybind11_add_module(agent_native_test
    push_test_frame.cpp
    ${CMAKE_SOURCE_DIR}/native/image_rb.cpp
)

target_include_directories(agent_native_test PRIVATE ${CMAKE_SOURCE_DIR}/native)
