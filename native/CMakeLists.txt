add_subdirectory(third_party/pybind11)

# 交叉编译时 Python.h 必须来自 sysroot (板端 rootfs), 而不是宿主机
if(NOT EXISTS "${CMAKE_SYSROOT}/usr/include/python3.8/Python.h")
  message(FATAL_ERROR
    "Missing ${CMAKE_SYSROOT}/usr/include/python3.8/Python.h\n"
    "板端 sysroot 缺少 python3.8-dev 头文件, 请安装 libpython3.8-dev 到 sysroot."
    )
endif()

pybind11_add_module(agent_native binding.cpp)

target_include_directories(agent_native PRIVATE ${CMAKE_CURRENT_SOURCE_DIR})
