# ============================================================================
#  RK3568 / aarch64 cross-compilation settings
#  目标板: Ubuntu 20.04 / aarch64 / glibc 2.31 / Python 3.8
#
#  工具链: Arm GNU Toolchain 9.2-2019.12 (aarch64-none-linux-gnu, glibc 2.30)
#          安装于 E:/rk3568/arm
#  sysroot: E:/rk3568/sysroot  (从板端同步, 含 Python 3.8.10 开发头文件)
# ============================================================================

set(CMAKE_SYSTEM_NAME Linux)
set(CMAKE_SYSTEM_PROCESSOR aarch64)

# 注意: 驱动器号统一用小写 e:。这套 mingw 版 gcc 在拼接 -B/-isystem 搜索路径时
# 对大小写敏感, 写成 E:/ 会导致 crt1.o / libc.so 全部找不到。
set(CMAKE_SYSROOT "e:/rk3568/sysroot")

set(CMAKE_C_COMPILER   "e:/rk3568/arm/bin/aarch64-none-linux-gnu-gcc.exe")
set(CMAKE_CXX_COMPILER "e:/rk3568/arm/bin/aarch64-none-linux-gnu-g++.exe")

# 关键: Ubuntu 的多架构目录, 工具链默认不会去这里找
set(_MULTIARCH "${CMAKE_SYSROOT}/usr/lib/aarch64-linux-gnu")
# 头文件同理: sys/cdefs.h、bits/*.h 都在 usr/include/aarch64-linux-gnu 下
set(_MULTIARCH_INC "${CMAKE_SYSROOT}/usr/include/aarch64-linux-gnu")

# -B 让 GCC 在这里找启动文件 (crt1.o, crti.o, crtbeginS.o)
# -L 让 ld 在这里找库 (libm, libc)
# -isystem 补上多架构头文件目录, 否则 features.h 找不到 sys/cdefs.h
set(_SYSROOT_FLAGS "-B${_MULTIARCH} -L${_MULTIARCH} -isystem ${_MULTIARCH_INC}")

set(CMAKE_C_FLAGS_INIT   "${_SYSROOT_FLAGS}")
set(CMAKE_CXX_FLAGS_INIT "${_SYSROOT_FLAGS}")

set(CMAKE_EXE_LINKER_FLAGS_INIT    "-B${_MULTIARCH} -L${_MULTIARCH}")
set(CMAKE_SHARED_LINKER_FLAGS_INIT "-B${_MULTIARCH} -L${_MULTIARCH}")
set(CMAKE_MODULE_LINKER_FLAGS_INIT "-B${_MULTIARCH} -L${_MULTIARCH}")
set(CMAKE_FIND_ROOT_PATH ${CMAKE_SYSROOT})
set(CMAKE_FIND_ROOT_PATH_MODE_PROGRAM NEVER)
set(CMAKE_FIND_ROOT_PATH_MODE_LIBRARY ONLY)
set(CMAKE_FIND_ROOT_PATH_MODE_INCLUDE ONLY)
set(CMAKE_FIND_ROOT_PATH_MODE_PACKAGE ONLY)

# ----------------------------------------------------------------------------
# pybind11: 交叉编译时必须手工提供目标端 Python 信息, 不能让 CMake 去跑
# 宿主机的 python.exe (那样会得到 .pyd / cpython-312 之类的错误后缀).
# ----------------------------------------------------------------------------
set(PYBIND11_CROSSCOMPILING ON)
set(PYBIND11_USE_CROSSCOMPILING ON)
# OFF = 不做 "用 Python 解释器输出覆盖下列变量" 的动作
set(PYBIND11_PYTHONLIBS_OVERWRITE OFF CACHE BOOL "" FORCE)
set(PYBIND11_FINDPYTHON OFF)

# 找一个宿主机解释器仅仅是为了让 FindPythonInterp 不报错, 其输出不再被采用
if(NOT PYTHON_EXECUTABLE)
  find_program(PYTHON_EXECUTABLE NAMES python python3 python.exe)
endif()

# --- 目标端 Python 3.8.10 (来自板端 rootfs) ---
# 注意: 不要在这里设 PYTHON_VERSION / PYTHON_VERSION_STRING。
# pybind11 把它们当"输出"变量, 设了会触发
#   "Set PYBIND11_PYTHON_VERSION to search for a specific version, not PYTHON_VERSION"
# 警告; 4 元组报版本号时缺失的那几项会自动显示成空串。
set(PYTHON_VERSION_MAJOR   3        CACHE INTERNAL "" FORCE)
set(PYTHON_VERSION_MINOR   8        CACHE INTERNAL "" FORCE)

set(PYTHON_INCLUDE_DIR "${CMAKE_SYSROOT}/usr/include/python3.8" CACHE PATH "" FORCE)
set(PYTHON_INCLUDE_DIRS
    "${CMAKE_SYSROOT}/usr/include/python3.8"
    "${CMAKE_SYSROOT}/usr/include/aarch64-linux-gnu/python3.8"
    CACHE INTERNAL "" FORCE)

set(PYTHON_LIBRARY   "${CMAKE_SYSROOT}/usr/lib/aarch64-linux-gnu/libpython3.8.so" CACHE FILEPATH "" FORCE)
set(PYTHON_LIBRARIES "${PYTHON_LIBRARY}" CACHE INTERNAL "" FORCE)

set(PYTHON_IS_DEBUG          OFF CACHE INTERNAL "" FORCE)
set(PYTHON_SIZEOF_VOID_P     8   CACHE INTERNAL "" FORCE)
set(PYTHON_MODULE_PREFIX     ""  CACHE INTERNAL "" FORCE)
set(PYTHON_MODULE_DEBUG_POSTFIX "" CACHE INTERNAL "" FORCE)
# 从 sysroot 的 lib/python3.8/config-3.8-aarch64-linux-gnu/Makefile 读到的 EXT_SUFFIX
set(PYTHON_MODULE_EXTENSION ".cpython-38-aarch64-linux-gnu.so" CACHE INTERNAL "" FORCE)
