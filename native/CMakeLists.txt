add_subdirectory(third_party/pybind11)

# ---------------------------------------------------------------------------
#  交叉编译时 Python.h 必须来自 sysroot（目标 rootfs），而不是宿主机
#  ⚠ T15-2-10b-3 改：这里原来**写死 python3.8**。板端 Ubuntu 20.04 是 3.8，
#    而**镜像里的 buildroot python 是 3.11** —— 写死就会把 cp38 的扩展装进 3.11 的
#    镜像里（`import agent_native` 直接失败）。现在按 sysroot 里**实际存在的版本**探测，
#    多版本时取最高的那个；宿主机（非交叉）构建完全不走这条分支，CI 的 host ctest 不受影响。
# ---------------------------------------------------------------------------
if(CMAKE_CROSSCOMPILING)
    file(GLOB _py_incs "${CMAKE_SYSROOT}/usr/include/python3.*")
    set(_py_inc "")
    set(_py_ver "")
    foreach(_d ${_py_incs})
        if(EXISTS "${_d}/Python.h")
            get_filename_component(_v "${_d}" NAME)
            string(REPLACE "python" "" _v "${_v}")
            if(_py_ver STREQUAL "" OR _v VERSION_GREATER _py_ver)
                set(_py_ver "${_v}")
                set(_py_inc "${_d}")
            endif()
        endif()
    endforeach()
    if(_py_inc STREQUAL "")
        message(FATAL_ERROR
            "sysroot 里找不到 Python.h（找过 ${CMAKE_SYSROOT}/usr/include/python3.*）。\n"
            "板端 sysroot 需要 python3-dev 头文件；镜像构建里由 buildroot 的 python3 提供。")
    endif()
    message(STATUS "native: 用 sysroot 的 python ${_py_ver}（${_py_inc}）")
    # 让 pybind11 找到同一份解释器/库，而不是宿主机的
    set(PYTHON_INCLUDE_DIRS "${_py_inc}")
    set(PYTHON_INCLUDE_DIR "${_py_inc}")
    if(EXISTS "${CMAKE_SYSROOT}/usr/lib/libpython${_py_ver}.so")
        set(PYTHON_LIBRARY "${CMAKE_SYSROOT}/usr/lib/libpython${_py_ver}.so")
    endif()
    include_directories(SYSTEM "${_py_inc}")
endif()

pybind11_add_module(agent_native
    binding.cpp
    decoder.cpp
    decoder_mpp.cpp
    image_rb.cpp
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
    # 两种 sysroot 布局都要认（T15-2-10b-3）：
    #   · 板端 Ubuntu：usr/include/rockchip/… + usr/lib/aarch64-linux-gnu/librockchip_mpp.so
    #   · 镜像 buildroot：usr/include/rockchip/… + **usr/lib**/librockchip_mpp.so（扁平）
    # 第一版只认前者，于是在 buildroot sysroot 上直接 FATAL_ERROR，
    # 而其实头文件与链接名都在、只是目录不同。
    set(_mpp_hdr "")
    foreach(_d "${CMAKE_SYSROOT}/usr/include" "${CMAKE_SYSROOT}/usr/include/aarch64-linux-gnu")
        if(EXISTS "${_d}/rockchip/rk_mpi.h")
            set(_mpp_hdr "${_d}/rockchip/rk_mpi.h")
            set(_mpp_inc "${_d}")
            break()
        endif()
    endforeach()
    set(_mpp_lib "")
    foreach(_d "${CMAKE_SYSROOT}/usr/lib" "${CMAKE_SYSROOT}/usr/lib/aarch64-linux-gnu")
        if(EXISTS "${_d}/librockchip_mpp.so")
            set(_mpp_lib "${_d}/librockchip_mpp.so")
            break()
        endif()
    endforeach()
    if(_mpp_hdr STREQUAL "" OR _mpp_lib STREQUAL "")
        message(FATAL_ERROR
            "sysroot 里找不到 Rockchip MPP 开发文件（找过 usr/include[/aarch64-linux-gnu] 与 "
            "usr/lib[/aarch64-linux-gnu]）:\n"
            "    头文件: ${CMAKE_SYSROOT}/usr/include/rockchip/rk_mpi.h\n"
            "    库:     ${CMAKE_SYSROOT}/usr/lib/librockchip_mpp.so\n"
            "板端是 librockchip-mpp-dev（pkgconfig 名 rockchip_mpp），"
            "镜像里由 buildroot 的 rockchip-mpp 包提供（它会装到 staging）。")
    endif()
    message(STATUS "native: MPP -> ${_mpp_lib}")

    target_compile_definitions(agent_native PRIVATE AGENT_HAVE_MPP)
    # 头文件在 <sysroot>/usr/include/rockchip/*.h, 彼此之间用相对 include,
    # 所以源码里写 #include <rockchip/rk_mpi.h> 即可。
    target_include_directories(agent_native PRIVATE "${_mpp_inc}")
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
#
# ⚠ T15-2-10b-3：**镜像的 buildroot sysroot 里没有 FFmpeg**（镜像的硬解走 MPP +
#   GStreamer，不需要 FFmpeg 的 dev 文件），所以这里从"缺就 FATAL_ERROR"改成
#   "缺就跳过并说明" —— 否则镜像侧永远编不出 native 扩展。
#   板端（Ubuntu，装了 libavcodec-dev）行为不变：有头文件就照旧打开。
#   确实需要强制要求时传 -DAGENT_REQUIRE_FFMPEG=ON。
if(CMAKE_CROSSCOMPILING)
    set(_ffmpeg_hdr "")
    foreach(_d "${CMAKE_SYSROOT}/usr/include/aarch64-linux-gnu" "${CMAKE_SYSROOT}/usr/include")
        if(EXISTS "${_d}/libavcodec/avcodec.h")
            set(_ffmpeg_hdr "${_d}/libavcodec/avcodec.h")
            break()
        endif()
    endforeach()
    if(_ffmpeg_hdr STREQUAL "")
        if(AGENT_REQUIRE_FFMPEG)
            message(FATAL_ERROR
                "sysroot 里找不到 FFmpeg 头文件（要找 libavcodec/avcodec.h）。\n"
                "板端请装 libavcodec-dev；镜像里本来就没有 FFmpeg，此时不要开 "
                "AGENT_REQUIRE_FFMPEG。")
        endif()
        message(STATUS "native: sysroot 没有 FFmpeg，跳过软解后端（AGENT_HAVE_FFMPEG 不开）")
    else()
        message(STATUS "native: FFmpeg -> ${_ffmpeg_hdr}")
        target_compile_definitions(agent_native PRIVATE AGENT_HAVE_FFMPEG)
        target_link_libraries(agent_native PRIVATE avcodec avutil)
    endif()
endif()
