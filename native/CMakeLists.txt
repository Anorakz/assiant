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
    decoder_mpp.cpp
    image_rb.cpp
    host_input_rb.cpp
    preprocess.cpp
    input_sender.cpp
    moonlight_adapter.cpp
)

target_include_directories(agent_native PRIVATE ${CMAKE_CURRENT_SOURCE_DIR})

# 注: 原来这里为 moonlight_connection.cpp (手写裸 socket HTTP 客户端) 在 Windows 上
# 链接 ws2_32。那一层已在 Phase 6 删除 —— 握手移到 Python 侧
# (agent/net/sunshine_client.py 走 HTTPS 47984 + 客户端证书), native 只通过
# start_with_session() 接 app_version / sessionUrl, 不再有任何 socket 调用,
# 所以 ws2_32 也不必链接了。

# ------------------------------------------------------------------ RPATH -----
# 板端 import agent_native 时报 "libmoonlight-common-c.so: cannot open shared
# object file" 就是因为构建期的 rpath 指向宿主机目录, 而部署时只拷了 .so。
# 把 $ORIGIN (自己所在目录) 写进 rpath, 两个库放在一起就能直接 import,
# 不必再要求 LD_LIBRARY_PATH=.
# 只在交叉编译时加: 宿主机构建产出的是 PE, rpath 没意义。
if(CMAKE_CROSSCOMPILING AND UNIX)
    set_target_properties(agent_native PROPERTIES
        BUILD_RPATH "$ORIGIN"
        INSTALL_RPATH "$ORIGIN")
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

# ------------------------------------------------------------- Rockchip MPP ---
# 视频硬解走 MPP (Rockchip Media Process Platform): 板端有 /dev/mpp_service +
# librockchip_mpp 1.3.8, 而 **没有** V4L2 M2M 解码设备 —— 所以原来那条
# FFmpeg h264_v4l2m2m / hevc_v4l2m2m 的路在这块板子上根本走不通 (已删)。
# 详见 docs/decoder-mpp.md。
#
# host 构建 **不** 打开 AGENT_HAVE_MPP: 宿主机没有 MPP, 而 decoder_mpp.cpp 在
# 没有这个宏时只会提供一个返回 nullptr 的工厂, 不需要任何 MPP 头文件。
if(CMAKE_CROSSCOMPILING)
    set(_mpp_hdr "${CMAKE_SYSROOT}/usr/include/rockchip/rk_mpi.h")
    set(_mpp_lib "${CMAKE_SYSROOT}/usr/lib/aarch64-linux-gnu/librockchip_mpp.so")
    if(NOT EXISTS "${_mpp_hdr}" OR NOT EXISTS "${_mpp_lib}")
        message(FATAL_ERROR
            "sysroot 里找不到 Rockchip MPP 开发文件:\n"
            "    头文件: ${_mpp_hdr}\n"
            "    库:     ${_mpp_lib}\n"
            "板端对应的包是 librockchip-mpp-dev (pkgconfig 名 rockchip_mpp), "
            "把它装进 sysroot (见 docs/cross-build-rk3568.md 的 MPP 一节)。")
    endif()

    target_compile_definitions(agent_native PRIVATE AGENT_HAVE_MPP)
    # 头文件在 <sysroot>/usr/include/rockchip/*.h, 彼此之间用相对 include,
    # 所以源码里写 #include <rockchip/rk_mpi.h> 即可。
    target_include_directories(agent_native PRIVATE "${CMAKE_SYSROOT}/usr/include")
    target_link_libraries(agent_native PRIVATE rockchip_mpp)
endif()

# ---------------------------------------------------------------- FFmpeg -----
# 板端 FFmpeg 4.2.7 的 dev 文件装在多架构目录里:
#     <sysroot>/usr/include/aarch64-linux-gnu/libavcodec/avcodec.h
# 而 toolchain.cmake 已经把 .../usr/include/aarch64-linux-gnu 加进 -isystem,
# 所以源码里直接 #include <libavcodec/avcodec.h> 就能找到。
#
# host 构建 **不** 打开 AGENT_HAVE_FFMPEG: 宿主机没有这套头文件, 而没有这个宏时
# decoder.cpp 里的 FFmpeg 软解后端整个被条件编译挡掉, 只留纯函数和分发逻辑
# (宿主机正好用它们测 Decoder 的契约)。
if(CMAKE_CROSSCOMPILING)
    set(_ffmpeg_hdr "${CMAKE_SYSROOT}/usr/include/aarch64-linux-gnu/libavcodec/avcodec.h")
    if(NOT EXISTS "${_ffmpeg_hdr}")
        message(FATAL_ERROR
            "sysroot 里找不到 FFmpeg 头文件: ${_ffmpeg_hdr}\n"
            "请把板端对应的 libavcodec-dev / libavutil-dev 装进 sysroot "
            "(见 docs/cross-build-rk3568.md 的 FFmpeg 一节)。")
    endif()

    target_compile_definitions(agent_native PRIVATE AGENT_HAVE_FFMPEG)
    target_link_libraries(agent_native PRIVATE avcodec avutil)
endif()
