// det_ai_crash.cpp - 确定性测试 AI:act() 故意写空指针触发 SIGSEGV,测崩溃判负路径
#include "sentry_duel.h"
#include <csignal>

extern "C" void act(const Board& board, char my_color) {
    (void)board;
    (void)my_color;
    volatile int* p = nullptr;
    *p = 42;                 // SIGSEGV
    std::raise(SIGSEGV);     // 兜底(正常执行到不了这里)
}
