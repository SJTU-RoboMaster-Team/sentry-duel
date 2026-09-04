// det_ai_sleep.cpp - 确定性测试 AI:act() 睡眠超过 1 秒,触发引擎超时罚分路径
#include "sentry_duel.h"
#include <unistd.h>

extern "C" void act(const Board& board, char my_color) {
    (void)board;
    (void)my_color;
    ::usleep(1200 * 1000);  // 1.2s > 引擎 1s 限制
}
