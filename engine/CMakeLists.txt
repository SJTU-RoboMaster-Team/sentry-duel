cmake_minimum_required(VERSION 3.10)
project(sentry_duel_engine CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

if(NOT CMAKE_BUILD_TYPE)
    set(CMAKE_BUILD_TYPE Release)
endif()

add_compile_options(-Wall -Wextra -O2 -fPIC)

# 引擎核心库 - 包含 board/utils,供 runner 链接
add_library(sentry_duel_engine SHARED
    src/board.cpp
    src/utils.cpp
    src/match.cpp)
target_include_directories(sentry_duel_engine PUBLIC include)
target_link_libraries(sentry_duel_engine PUBLIC dl)

# 主引擎二进制 - 含 move/turn/fire/scan,选手 .so 在 dlopen 时从进程全局符号表解析
# 用 -rdynamic 让 runner 导出所有符号
add_executable(runner src/runner.cpp)
target_link_libraries(runner sentry_duel_engine dl)
target_include_directories(runner PRIVATE include)
target_link_options(runner PRIVATE -rdynamic)

# 交互式人机对战入口。人类动作从 stdin 逐行读取，AI 仍通过同一 Match 规则结算。
add_executable(human_runner src/human_runner.cpp)
target_link_libraries(human_runner sentry_duel_engine dl)
target_include_directories(human_runner PRIVATE include)
target_link_options(human_runner PRIVATE -rdynamic)

include(CTest)
if(BUILD_TESTING)
    add_executable(test_free_turn tests/test_free_turn.cpp)
    target_link_libraries(test_free_turn sentry_duel_engine dl)
    target_include_directories(test_free_turn PRIVATE include)
    target_link_options(test_free_turn PRIVATE -rdynamic)
    add_test(NAME free_turn_rules COMMAND test_free_turn)
endif()
