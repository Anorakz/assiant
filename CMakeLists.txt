cmake_minimum_required(VERSION 3.20)
project(agent LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

# 交叉编译时不编测试; 只有 host 构建才需要
if(CMAKE_CROSSCOMPILING)
  set(AGENT_BUILD_TESTS_DEFAULT OFF)
else()
  set(AGENT_BUILD_TESTS_DEFAULT ON)
endif()
option(AGENT_BUILD_TESTS "Build host-side C++ unit tests" ${AGENT_BUILD_TESTS_DEFAULT})

add_subdirectory(native)

if(AGENT_BUILD_TESTS)
  enable_testing()
  add_subdirectory(tests)
endif()

# ---------------------------------------------------------------- board 测试 --
# 板端要验证 image_rb 的 numpy 数据通路, 就必须有帧进缓冲 —— 而正式模块刻意
# 不暴露"写帧"入口 (生产者只有 moonlight 视频线程)。所以交叉编译时额外产出
# 一个**测试用**扩展 agent_native_test, 它通过 capsule 拿到同一个 image_rb。
# 它不进 ctest, 也不随 deploy.ps1 部署 (只手动推到板端跑测试时用)。
if(CMAKE_CROSSCOMPILING)
  add_subdirectory(tests/board)
endif()
