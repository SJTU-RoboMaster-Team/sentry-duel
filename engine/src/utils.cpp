// utils.cpp - 工具函数实现
// 视野: T 字形 4 格(正前方 1 + 距离 2 的横向 3 格) + 障碍遮挡
// 详见 rules.md §五

#include "utils.h"
#include <cmath>
#include <algorithm>

namespace {

// (x, y) 是否是障碍
inline bool is_obstacle(int x, int y, const std::vector<Pos>& obstacles) {
    for (const auto& o : obstacles) {
        if (o.x == x && o.y == y) return true;
    }
    return false;
}

// 转向字符 → (dx, dy)
inline void facing_delta(char f, int& dx, int& dy) {
    switch (f) {
        case 'N': dx = 0;  dy = -1; break;
        case 'E': dx = 1;  dy = 0;  break;
        case 'S': dx = 0;  dy = 1;  break;
        case 'W': dx = -1; dy = 0;  break;
        default:  dx = 0;  dy = 0;
    }
}

// T 字形视野格子(相对 me 的偏移,共 4 格)
inline void vision_offsets(char facing, int dx[], int dy[]) {
    // 距离 1: 正前方 1 格
    // 距离 2: 第二行的横向 3 格
    switch (facing) {
        case 'N':
            dx[0] = 0;  dy[0] = -1;  // (col, row-1)
            dx[1] = -1; dy[1] = -2;
            dx[2] = 0;  dy[2] = -2;
            dx[3] = 1;  dy[3] = -2;
            break;
        case 'E':
            dx[0] = 1;  dy[0] = 0;   // (col+1, row)
            dx[1] = 2;  dy[1] = -1;
            dx[2] = 2;  dy[2] = 0;
            dx[3] = 2;  dy[3] = 1;
            break;
        case 'S':
            dx[0] = 0;  dy[0] = 1;   // (col, row+1)
            dx[1] = -1; dy[1] = 2;
            dx[2] = 0;  dy[2] = 2;
            dx[3] = 1;  dy[3] = 2;
            break;
        case 'W':
            dx[0] = -1; dy[0] = 0;   // (col-1, row)
            dx[1] = -2; dy[1] = -1;
            dx[2] = -2; dy[2] = 0;
            dx[3] = -2; dy[3] = 1;
            break;
    }
}

// T 字形视野遮挡:从 me 到 (mx+dx, my+dy) 之间是否被障碍阻挡
// 简化规则 - 直线 LOS 检查
inline bool blocked_by_obstacle(int mx, int my, int tx, int ty,
                                 const std::vector<Pos>& obstacles) {
    // 走 Bresenham 风格的线段采样
    int x = mx, y = my;
    int sx = (tx > x) ? 1 : (tx < x ? -1 : 0);
    int sy = (ty > y) ? 1 : (ty < y ? -1 : 0);
    int steps = std::max(std::abs(tx - x), std::abs(ty - y));
    for (int i = 1; i < steps; ++i) {
        x += sx;
        y += sy;
        if (is_obstacle(x, y, obstacles)) return true;
    }
    return false;
}

} // namespace

extern "C" bool can_see(const Sentry& me, const Pos& target,
                        const std::vector<Pos>& obstacles) {
    int mx = me.last_known_pos.x;
    int my = me.last_known_pos.y;
    int dx[4], dy[4];
    vision_offsets(me.last_known_facing, dx, dy);

    for (int i = 0; i < 4; ++i) {
        int tx = mx + dx[i];
        int ty = my + dy[i];
        if (tx == target.x && ty == target.y) {
            // 自己所在格也算可见
            return !blocked_by_obstacle(mx, my, tx, ty, obstacles);
        }
    }
    // 自己所在的格子也算可见
    if (target.x == mx && target.y == my) return true;
    return false;
}

extern "C" int manhattan_distance(const Pos& a, const Pos& b) {
    return std::abs(a.x - b.x) + std::abs(a.y - b.y);
}

extern "C" bool in_score_zone(const Pos& pos,
                              const std::vector<Pos>& score_zones) {
    for (const auto& p : score_zones) {
        if (p.x == pos.x && p.y == pos.y) return true;
    }
    return false;
}

extern "C" char best_turn_to_face(const Pos& from, const Pos& to) {
    int dx = to.x - from.x;
    int dy = to.y - from.y;
    if (std::abs(dx) >= std::abs(dy)) {
        return (dx >= 0) ? 'E' : 'W';
    } else {
        return (dy >= 0) ? 'S' : 'N';
    }
}

extern "C" bool can_move_forward(const Sentry& me, const Pos& opp_last_known_pos,
                                 const std::vector<Pos>& obstacles, int board_size) {
    int dx, dy;
    facing_delta(me.last_known_facing, dx, dy);
    int nx = me.last_known_pos.x + dx;
    int ny = me.last_known_pos.y + dy;

    if (nx < 0 || nx >= board_size || ny < 0 || ny >= board_size) return false;
    if (is_obstacle(nx, ny, obstacles)) return false;
    if (nx == opp_last_known_pos.x && ny == opp_last_known_pos.y) return false;
    return true;
}