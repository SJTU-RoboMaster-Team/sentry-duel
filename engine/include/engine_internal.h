// engine_internal.h - 引擎内部接口(不暴露给选手)
// runner.cpp 和 board.cpp 共用

#pragma once

#include "sentry_duel.h"

// 创建初始 Board(用于开局)
Board make_initial_board(int size);

struct ActionOutcome {
    bool success;
    bool hit;
};

// 应用一次行动。FIRE 的可见性由 runner 按行动方视图预先校验。
// action: 0=move, 1=turn, 2=fire, 3=scan
// arg: 仅 turn 时使用('N'/'E'/'S'/'W'),其他行动忽略
ActionOutcome apply_action(Board& b, char side, int action, char arg);

// 回合结束:CD -1、占点计分、turn++
void end_turn(Board& b);
