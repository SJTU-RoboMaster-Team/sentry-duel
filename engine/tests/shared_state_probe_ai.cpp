// 回归测试用探针:红蓝加载同一个 .so 时,file-scope static 不能被两侧共用。
//
// runner 会为同源的一侧创建私有副本(不同 inode)来隔离状态。若隔离失效,
// 单个模块的 static 会被两侧交替写入,于是这里会打印 SHARED_STATE_DETECTED;
// 隔离正确时,每一侧各自拥有独立模块,首次 act() 都是各自的第 1 次调用。
#include "sentry_duel.h"
#include <cstdio>

static int calls = 0;
static char last_side = 0;

extern "C" void act(const Board& board, char my_color) {
    calls++;
    if (last_side != 0 && last_side != my_color) {
        std::fprintf(stderr, "SHARED_STATE_DETECTED call=%d last=%c now=%c\n",
                     calls, last_side, my_color);
    } else if (last_side == 0) {
        std::fprintf(stderr, "probe first act side=%c calls=%d\n",
                     my_color, calls);
    }
    last_side = my_color;
    (void)board;
}
