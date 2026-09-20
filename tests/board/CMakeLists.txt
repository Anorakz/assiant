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

# ---------------------------------------------------------------------------
#  mpp_decode_smoke — MPP 解码离线冒烟 (独立可执行文件, 不依赖 Python)
#
#  宿主机测不了"真解出一帧" (没有 MPP, 也没有硬解设备), 所以这段只能在板端跑:
#  拿 tests/data/ 里的纯色码流喂给 Decoder, 逐帧核对尺寸/平面均匀性/YUV 值。
#
#  刻意**只**开 AGENT_HAVE_MPP, 不连 FFmpeg: 这样"能解出来"就一定是 MPP 的功劳,
#  不会因为偷偷退到软解而让测试变得没有意义。
# ---------------------------------------------------------------------------
add_executable(mpp_decode_smoke
    mpp_decode_smoke.cpp
    ${CMAKE_SOURCE_DIR}/native/decoder.cpp
    ${CMAKE_SOURCE_DIR}/native/decoder_mpp.cpp
)

target_include_directories(mpp_decode_smoke PRIVATE
    ${CMAKE_SOURCE_DIR}/native
    "${CMAKE_SYSROOT}/usr/include"
)
target_compile_definitions(mpp_decode_smoke PRIVATE AGENT_HAVE_MPP)
target_link_libraries(mpp_decode_smoke PRIVATE rockchip_mpp)

# librockchip_mpp.so 自己还 NEEDED libpthread.so.0, 而 ld 解析"被别人依赖的库"时
# 只看 -rpath-link, 不看 -L (-L 只服务 -l 的搜索)。少了这一句就是:
#   warning: libpthread.so.0, needed by .../librockchip_mpp.so, not found
target_link_options(mpp_decode_smoke PRIVATE
    "-Wl,-rpath-link,${CMAKE_SYSROOT}/usr/lib/aarch64-linux-gnu")

# ⚠ 必须显式链接**目标端**的 libstdc++。
#
# librockchip_mpp 是 C++ 写的, 内部要用 operator new / __cxa_guard_acquire 这些
# 带版本号的符号 (GLIBCXX_3.4 / CXXABI_1.3.9)。ARM 工具链自带的那个 libstdc++
# 满足不了这些版本需求, 于是链接**可执行文件**时报一堆:
#     librockchip_mpp.so: undefined reference to `operator new(unsigned long)@GLIBCXX_3.4'
# (agent_native 是 .so, 允许有未定义符号, 所以它没报 —— 靠运行时板端的
#  libstdc++.so.6 补上。可执行文件不行, 必须当场解掉。)
#
# 直接按路径链目标的 libstdc++.so.6: 它有 SONAME, 所以 DT_NEEDED 记的还是
# "libstdc++.so.6" (板端正常解析), 同时版本符号能和 mpp 对上。
target_link_libraries(mpp_decode_smoke PRIVATE
    "${CMAKE_SYSROOT}/usr/lib/aarch64-linux-gnu/libstdc++.so.6")
