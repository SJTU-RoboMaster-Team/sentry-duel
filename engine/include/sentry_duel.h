// sentry_duel.h - 选手 ABI 头文件
// 哨兵大战 V4 Final (2026-08-18)
//
// 选手 AI 必须:
//   1. #include "sentry_duel.h"
//   2. 实现 extern "C" void act(const Board& board, char my_color)
//   3. 通过 g++ -std=c++17 -O2 -shared -fPIC 编译为 .so
//
// 引擎在调用 act() 前会绑定行动函数表(move/turn/fire/scan)。
// 每个行动会立即结算并返回是否成功；SCAN 额外返回实时敌情。

#pragma once

#include <vector>

#ifdef __cplusplus
extern "C" {
#endif

typedef struct {
    int x;  // 列 [0, size)
    int y;  // 行 [0, size)
} Pos;

typedef struct {
    // 在本方 Board 视图中，对方的这些字段为部分可见信息。
    Pos last_known_pos;       // 上次已知位置
    char last_known_facing;   // 'N' / 'E' / 'S' / 'W' / '?'
    bool visible;             // 当前是否在我视野内(SCAN 临时可见也算)

    // 我方视角的字段(总是可见)
    int fire_cd;              // 距下次 FIRE 的回合数
    int scan_cd;              // 距下次 SCAN 的回合数
    int score;
} Sentry;

typedef struct {
    Sentry red;
    Sentry blue;
    int turn;                          // 当前回合编号,从 0 开始
    int size;                          // 棋盘边长,恒为 7
    std::vector<Pos> obstacles;        // 障碍位置
    std::vector<Pos> score_zones;      // 得分区格子
} Board;

typedef struct {
    Pos my_pos;
    char my_facing;
    Pos opp_last_known_pos;
    char opp_last_known_facing;
    bool opp_visible;
    bool opp_directly_visible; // 非 SCAN 临时情报，当前处于 T 形视野内
    int fire_cd;
    int scan_cd;
} ActionObservation;

typedef struct {
    bool success;
    bool consumed;
    ActionObservation observation;
} ActionResult;

typedef struct {
    bool success;
    bool consumed;
    ActionObservation observation;
} ScanResult;

// 行动函数 - 只能在 act() 内调用。失败不消耗行动次数。
ActionResult move();
ActionResult turn(char c);   // 'N' / 'E' / 'S' / 'W'
ActionResult fire();
ScanResult scan();

// 选手必须实现的全局函数
void act(const Board& board, char my_color);  // my_color = 'R' 或 'B'

#ifdef __cplusplus
}
#endif
