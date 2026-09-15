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
