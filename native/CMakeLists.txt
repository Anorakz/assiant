add_subdirectory(third_party/pybind11)

# 交叉编译时 Python.h 必须来自 sysroot (板端 rootfs), 而不是宿主机
if(CMAKE_CROSSCOMPILING AND NOT EXISTS "${CMAKE_SYSROOT}/usr/include/python3.8/Python.h")
  message(FATAL_ERROR
    "Missing ${CMAKE_SYSROOT}/usr/include/python3.8/Python.h\n"
    "板端 sysroot 缺少 python3.8-dev 头文件, 请安装 libpython3.8-dev 到 sysroot."
    )
endif()

pybind11_add_module(agent_native
    binding.cpp
    decoder.cpp
    image_rb.cpp
    host_input_rb.cpp
    preprocess.cpp
    input_sender.cpp
    moonlight_connection.cpp
    moonlight_adapter.cpp
)

target_include_directories(agent_native PRIVATE ${CMAKE_CURRENT_SOURCE_DIR})

# moonlight_connection.cpp 用的是原始 socket: Windows 上要显式链接 ws2_32
# (MinGW 不认 #pragma comment(lib, ...))。sysroot 侧不需要额外库。
if(WIN32)
    target_link_libraries(agent_native PRIVATE ws2_32)
endif()

# ------------------------------------------------------- moonlight-common-c ---
# 只在交叉编译时集成。host 上不集成的原因:
#   · 板端才是真正要收流的目标
#   · host 构建里 moonlight_adapter.cpp 走"不引用 moonlight 符号"的分支,
#     仍然可以验证状态机 / HTTP 握手 / Annex-B 通路
if(CMAKE_CROSSCOMPILING)
    set(_ml_dir "${CMAKE_CURRENT_SOURCE_DIR}/third_party/moonlight-common-c")
    if(NOT EXISTS "${_ml_dir}/src/Limelight.h")
        message(FATAL_ERROR
            "moonlight-common-c 未初始化: ${_ml_dir}\n"
            "请跑: git submodule update --init --recursive")
    endif()
    if(NOT EXISTS "${_ml_dir}/enet/CMakeLists.txt")
        message(FATAL_ERROR
            "moonlight-common-c 的 enet 子模块是空的, 必须 **递归** 初始化:\n"
            "    git submodule update --init --recursive")
    endif()

    # moonlight-common-c 自己是个顶层 CMake 工程 (带 enet + nanors),
    # 直接 add_subdirectory 即可。它需要 OpenSSL >= 1.0.2, 由 sysroot 提供。
    add_subdirectory(${_ml_dir} ${CMAKE_BINARY_DIR}/moonlight-common-c)

    target_compile_definitions(agent_native PRIVATE AGENT_HAVE_MOONLIGHT)
    target_include_directories(agent_native PRIVATE ${_ml_dir}/src)
    target_link_libraries(agent_native PRIVATE moonlight-common-c)
endif()

# ---------------------------------------------------------------- FFmpeg -----
# 板端 FFmpeg 4.2.7 的 dev 文件装在多架构目录里:
#     <sysroot>/usr/include/aarch64-linux-gnu/libavcodec/avcodec.h
# 而 toolchain.cmake 已经把 .../usr/include/aarch64-linux-gnu 加进 -isystem,
# 所以源码里直接 #include <libavcodec/avcodec.h> 就能找到。
#
# host 构建 **不** 打开 AGENT_HAVE_FFMPEG: 宿主机没有这套头文件, 而且骨架
# 阶段的 decoder stub 本来就不需要它 (decoder.cpp 里的 #include 被条件编译挡住)。
if(CMAKE_CROSSCOMPILING)
    set(_ffmpeg_hdr "${CMAKE_SYSROOT}/usr/include/aarch64-linux-gnu/libavcodec/avcodec.h")
    if(NOT EXISTS "${_ffmpeg_hdr}")
        message(FATAL_ERROR
            "sysroot 里找不到 FFmpeg 头文件: ${_ffmpeg_hdr}\n"
            "请把板端对应的 libavcodec-dev / libavutil-dev 装进 sysroot "
            "(见 docs/cross-build-rk3568.md 的 FFmpeg 一节)。")
    endif()

    target_compile_definitions(agent_native PRIVATE AGENT_HAVE_FFMPEG)

    # 真正链接 FFmpeg。骨架阶段 decoder.cpp 不引用任何 libav* 符号, 所以现在
    # 加上只是提前把链接链路打通 (并验证 sysroot 里的 .so 链接脚本可用)。
    # 等 decoder.cpp 填了真实实现, 这里不用改。
    target_link_libraries(agent_native PRIVATE avcodec avutil)
endif()
