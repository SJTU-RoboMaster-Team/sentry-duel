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

    add_library(opening_probe_ai MODULE tests/opening_probe_ai.cpp)
    set_target_properties(opening_probe_ai PROPERTIES PREFIX "")
    target_include_directories(opening_probe_ai PRIVATE include)
    add_test(
        NAME runner_opening_intel
        COMMAND ${CMAKE_COMMAND} -E env
                "LD_LIBRARY_PATH=$<TARGET_FILE_DIR:sentry_duel_engine>"
                $<TARGET_FILE:runner>
                --red $<TARGET_FILE:opening_probe_ai>
                --blue $<TARGET_FILE:opening_probe_ai>
                --max-turns 1)
    set_tests_properties(runner_opening_intel PROPERTIES
        PASS_REGULAR_EXPRESSION "\"side\":\"B\",\"action\":\"turn\",\"arg\":\"N\"")

    add_library(scan_probe_ai MODULE tests/scan_probe_ai.cpp)
    set_target_properties(scan_probe_ai PROPERTIES PREFIX "")
    target_include_directories(scan_probe_ai PRIVATE include)
    add_test(
        NAME runner_scan_visibility
        COMMAND ${CMAKE_COMMAND} -E env
                "LD_LIBRARY_PATH=$<TARGET_FILE_DIR:sentry_duel_engine>"
                $<TARGET_FILE:runner>
                --red $<TARGET_FILE:scan_probe_ai>
                --blue $<TARGET_FILE:scan_probe_ai>
                --max-turns 3)
    set_tests_properties(runner_scan_visibility PROPERTIES
        PASS_REGULAR_EXPRESSION "\"side\":\"R\",\"action\":\"turn\",\"arg\":\"N\"")

    add_library(timeout_probe_ai MODULE tests/timeout_probe_ai.cpp)
    set_target_properties(timeout_probe_ai PROPERTIES PREFIX "")
    target_include_directories(timeout_probe_ai PRIVATE include)
    add_test(
        NAME runner_repeated_timeout
        COMMAND ${CMAKE_COMMAND} -E env
                "LD_LIBRARY_PATH=$<TARGET_FILE_DIR:sentry_duel_engine>"
                $<TARGET_FILE:runner>
                --red $<TARGET_FILE:timeout_probe_ai>
                --blue $<TARGET_FILE:timeout_probe_ai>
                --max-turns 2)
    set_tests_properties(runner_repeated_timeout PROPERTIES
        TIMEOUT 6
        PASS_REGULAR_EXPRESSION "\"winner\":\"B\".*\"blue_score\":2")

    # 同一个 .so 同时当红蓝时,两侧不能共享 file-scope static。
    add_library(shared_state_probe_ai MODULE tests/shared_state_probe_ai.cpp)
    set_target_properties(shared_state_probe_ai PROPERTIES PREFIX "")
    target_include_directories(shared_state_probe_ai PRIVATE include)
    add_test(
        NAME runner_isolates_same_so
        COMMAND ${CMAKE_COMMAND} -E env
                "LD_LIBRARY_PATH=$<TARGET_FILE_DIR:sentry_duel_engine>"
                $<TARGET_FILE:runner>
                --red $<TARGET_FILE:shared_state_probe_ai>
                --blue $<TARGET_FILE:shared_state_probe_ai>
                --max-turns 3)
    set_tests_properties(runner_isolates_same_so PROPERTIES
        PASS_REGULAR_EXPRESSION "side=B calls=1"
        FAIL_REGULAR_EXPRESSION "SHARED_STATE_DETECTED")
endif()
