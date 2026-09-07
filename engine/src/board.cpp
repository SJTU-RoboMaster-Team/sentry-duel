// board.cpp - 引擎内部规则判定 + 行动应用
// 不暴露给选手,只供 runner.cpp 使用

#include "sentry_duel.h"
#include "utils.h"
#include "engine_internal.h"
#include <cstdint>
#include <cstdio>
#include <string>
#include <cmath>

namespace internal {

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

inline bool is_obstacle(const Board& b, int x, int y) {
    for (const auto& o : b.obstacles) if (o.x == x && o.y == y) return true;
    return false;
}

inline bool is_obstacle_pos(int x, int y, const std::vector<Pos>& obstacles) {
    for (const auto& o : obstacles) if (o.x == x && o.y == y) return true;
    return false;
}

// 给定方向,枚举前方 3×3 正方形 9 格相对坐标
inline void fire_offsets(char facing, int dx[], int dy[]) {
    switch (facing) {
        case 'N':
            dx[0]=-1; dy[0]=-1; dx[1]=0; dy[1]=-1; dx[2]=1; dy[2]=-1;
            dx[3]=-1; dy[3]=-2; dx[4]=0; dy[4]=-2; dx[5]=1; dy[5]=-2;
            dx[6]=-1; dy[6]=-3; dx[7]=0; dy[7]=-3; dx[8]=1; dy[8]=-3;
            break;
        case 'E':
            dx[0]=1; dy[0]=-1; dx[1]=1; dy[1]=0; dx[2]=1; dy[2]=1;
            dx[3]=2; dy[3]=-1; dx[4]=2; dy[4]=0; dx[5]=2; dy[5]=1;
            dx[6]=3; dy[6]=-1; dx[7]=3; dy[7]=0; dx[8]=3; dy[8]=1;
            break;
        case 'S':
            dx[0]=-1; dy[0]=1; dx[1]=0; dy[1]=1; dx[2]=1; dy[2]=1;
            dx[3]=-1; dy[3]=2; dx[4]=0; dy[4]=2; dx[5]=1; dy[5]=2;
            dx[6]=-1; dy[6]=3; dx[7]=0; dy[7]=3; dx[8]=1; dy[8]=3;
            break;
        case 'W':
            dx[0]=-1; dy[0]=-1; dx[1]=-1; dy[1]=0; dx[2]=-1; dy[2]=1;
            dx[3]=-2; dy[3]=-1; dx[4]=-2; dy[4]=0; dx[5]=-2; dy[5]=1;
            dx[6]=-3; dy[6]=-1; dx[7]=-3; dy[7]=0; dx[8]=-3; dy[8]=1;
            break;
    }
}

// FIRE 命中:前向 3×3 内，同一横/纵火力通道上的障碍会挡住后方格子。
inline Pos fire_hit(const Sentry& me, const Pos& opp_pos,
                    const std::vector<Pos>& obstacles) {
    int odx[9], ody[9];
    fire_offsets(me.last_known_facing, odx, ody);

    int fx = -1, fy = -1;
    for (int i = 0; i < 9; ++i) {
        int tx = me.last_known_pos.x + odx[i];
        int ty = me.last_known_pos.y + ody[i];
        if (tx == opp_pos.x && ty == opp_pos.y) {
            fx = tx; fy = ty;
            break;
        }
    }
    if (fx < 0) return {-1, -1};

    const int forward = (me.last_known_facing == 'E' || me.last_known_facing == 'W')
        ? std::abs(fx - me.last_known_pos.x) : std::abs(fy - me.last_known_pos.y);
    for (int distance = 1; distance < forward; ++distance) {
        int x = me.last_known_pos.x;
        int y = me.last_known_pos.y;
        if (me.last_known_facing == 'E') x += distance;
        if (me.last_known_facing == 'W') x -= distance;
        if (me.last_known_facing == 'S') y += distance;
        if (me.last_known_facing == 'N') y -= distance;
        if (me.last_known_facing == 'E' || me.last_known_facing == 'W') {
            y = fy;
        } else {
            x = fx;
        }
        if (is_obstacle_pos(x, y, obstacles)) return {-1, -1};
    }
    return {fx, fy};
}

} // namespace internal

// 公开 helper: 初始 Board(供 runner.cpp 使用)
Board make_initial_board(int size) {
    Board b;
    b.size = size;
    b.turn = 0;

    // 红方出生 (0,0) 朝 E,蓝方出生 (size-1, size-1) 朝 W
// visible 字段在传给选手 act() 时由 runner.cpp 重新计算:
//   me.visible = true (自己总能看到自己)
//   opp.visible = (真实「对方是否在 me 视野内」)
// 所以这里 Board 里的 red.visible/blue.visible 暂时无意义,先设 false
b.red.last_known_pos = {0, 0};
b.red.last_known_facing = 'E';
b.red.visible = false;
b.red.fire_cd = 0;
b.red.scan_cd = 0;
b.red.score = 0;

b.blue.last_known_pos = {size - 1, size - 1};
b.blue.last_known_facing = 'W';
b.blue.visible = false;
b.blue.fire_cd = 0;
b.blue.scan_cd = 0;
b.blue.score = 0;

    // 7×7 地图障碍物。
    b.obstacles = {{1, 1}, {5, 5}};

    // 得分区:中心十字 5 格。
    const int m = size / 2;
    b.score_zones = {{m, m - 1}, {m - 1, m}, {m, m},
                     {m + 1, m}, {m, m + 1}};

    return b;
}

// 应用一次行动
// action: 0=move, 1=turn, 2=fire, 3=scan
// arg: turn 的目标朝向(其他行动忽略)
ActionOutcome apply_action(Board& b, char side, int action, char arg) {
    Sentry& me   = (side == 'R') ? b.red  : b.blue;
    Sentry& opp  = (side == 'R') ? b.blue : b.red;
    Pos&   my_pos   = me.last_known_pos;
    Pos    opp_pos  = opp.last_known_pos;
    auto&  obstacles = b.obstacles;

    if (action == 0) { // MOVE
        int dx, dy;
        internal::facing_delta(me.last_known_facing, dx, dy);
        int nx = my_pos.x + dx;
        int ny = my_pos.y + dy;
        if (nx < 0 || nx >= b.size || ny < 0 || ny >= b.size) return {false, false};
        if (internal::is_obstacle(b, nx, ny)) return {false, false};
        if (nx == opp_pos.x && ny == opp_pos.y) return {false, false};
        my_pos.x = nx;
        my_pos.y = ny;
        return {true, false};
    } else if (action == 1) { // TURN
        if (arg != 'N' && arg != 'E' && arg != 'S' && arg != 'W') return {false, false};
        // api.md §4.2:即使转向不变(已是该朝向)也正常生效,计入次数
        me.last_known_facing = arg;
        return {true, false};
    } else if (action == 2) { // FIRE
        if (me.fire_cd > 0) return {false, false};
        Pos hit = internal::fire_hit(me, opp.last_known_pos, obstacles);
        // rules.md §4.3:开火后 fire_cd = 2,本回合结束 -1,下一回合为 1，再下一回合恢复
        me.fire_cd = 2;
        if (hit.x >= 0) {
            // rules.md §6.3:命中 - 对方回出生点、朝向重置,但 fire_cd/scan_cd 保留
            opp.last_known_pos = (side == 'R') ? Pos{b.size - 1, b.size - 1} : Pos{0, 0};
            opp.last_known_facing = (side == 'R') ? 'W' : 'E';
            me.score += 2;
            return {true, true};
        }
        return {true, false};
    } else if (action == 3) { // SCAN
        if (me.scan_cd > 0) return {false, false};
        me.scan_cd = 3;
        return {true, false};
    }
    return {false, false};
}

void end_side_turn(Board& b, char side) {
    Sentry& sentry = side == 'R' ? b.red : b.blue;
    if (in_score_zone(sentry.last_known_pos, b.score_zones)) sentry.score++;
}

// 双方行动结束处理:CD -1,回合数 +1
void end_round(Board& b) {
    // CD -1
    if (b.red.fire_cd > 0) b.red.fire_cd--;
    if (b.red.scan_cd > 0) b.red.scan_cd--;
    if (b.blue.fire_cd > 0) b.blue.fire_cd--;
    if (b.blue.scan_cd > 0) b.blue.scan_cd--;

    b.turn++;
}
