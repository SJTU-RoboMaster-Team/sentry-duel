// utils.h - 工具函数声明
// 选手 AI 可直接 include 使用,引擎内部也用同一份保证一致。

#pragma once

#include "sentry_duel.h"
#include <vector>

#ifdef __cplusplus
extern "C" {
#endif

// 视野判定 - 是否能从 me 看到 target(考虑障碍遮挡)
bool can_see(const Sentry& me, const Pos& target, const std::vector<Pos>& obstacles);

// 曼哈顿距离
int manhattan_distance(const Pos& a, const Pos& b);

// 得分区判断
bool in_score_zone(const Pos& pos, const std::vector<Pos>& score_zones);

// 朝向工具:返回 'N'/'E'/'S'/'W',让 from 转到该朝向最接近面对 to
char best_turn_to_face(const Pos& from, const Pos& to);

// 移动判断:me 在 me.last_known_pos 朝向方向上能前进一格吗
bool can_move_forward(const Sentry& me, const Pos& opp_last_known_pos,
                      const std::vector<Pos>& obstacles, int board_size);

#ifdef __cplusplus
}
#endif