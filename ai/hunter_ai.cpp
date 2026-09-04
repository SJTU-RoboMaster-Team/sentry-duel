// hunter_ai.cpp - 扫描压制 AI
// 优先级：SCAN 后立即击杀 > 一步前压击杀 > 进入中心得分区。

#include "sentry_duel.h"
#include "utils.h"
#include "navigation.h"

#include <chrono>
#include <cstdint>

namespace {

class HunterState {
public:
    std::uint32_t random;
    Pos route_target = {-1, -1};
    Pos route_waypoint = {-1, -1};
    Pos previous_pos = {-1, -1};
    int route_kind = 0;
    bool route_initialized = false;
    bool spawn_turn_pending = false;
    bool skip_scan_once = false;

    HunterState()
        : random(static_cast<std::uint32_t>(
              std::chrono::steady_clock::now().time_since_epoch().count())) {}

    std::uint32_t next_random() {
        random ^= random << 13;
        random ^= random >> 17;
        random ^= random << 5;
        return random;
    }
};

HunterState& state() {
    static HunterState value;
    return value;
}

Pos route_destination(const Board& board, const Pos& position) {
    HunterState& hunter = state();
    if (hunter.route_target.x < 0) {
        hunter.route_target = {board.size / 2, board.size / 2};
    }
    if (!same_pos(hunter.route_waypoint, Pos{-1, -1}) &&
        !same_pos(position, hunter.route_waypoint)) {
        return hunter.route_waypoint;
    }
    return hunter.route_target;
}

bool can_step_fire(const Board& board, const Pos& from, char facing,
                   const Pos& opponent) {
    int dx, dy;
    direction_delta(facing, dx, dy);
    const Pos next = {from.x + dx, from.y + dy};
    if (blocked(board, next.x, next.y, opponent) ||
        !in_fire_range(next, facing, opponent)) return false;

    const int forward = (facing == 'E' || facing == 'W')
        ? std::abs(opponent.x - next.x) : std::abs(opponent.y - next.y);
    for (int distance = 1; distance < forward; ++distance) {
        const int x = (facing == 'E') ? next.x + distance
                    : (facing == 'W') ? next.x - distance : opponent.x;
        const int y = (facing == 'S') ? next.y + distance
                    : (facing == 'N') ? next.y - distance : opponent.y;
        if (blocked(board, x, y, Pos{-1, -1})) return false;
    }
    return true;
}

bool fire_if_in_range(const Pos& from, char facing, const Pos& opponent,
                      int& actions) {
    if (!in_fire_range(from, facing, opponent)) return false;
    if (fire().success) {
        ++actions;
        return true;
    }
    return false;
}

} // namespace

extern "C" void act(const Board& board, char my_color) {
    const Sentry& me = my_color == 'R' ? board.red : board.blue;
    const Sentry& opp = my_color == 'R' ? board.blue : board.red;
    Pos position = me.last_known_pos;
    char facing = me.last_known_facing;
    Pos enemy = opp.last_known_pos;
    char enemy_facing = opp.last_known_facing;
    bool enemy_known = opp.last_known_pos.x >= 0 && opp.last_known_pos.y >= 0;
    const bool holding_score_zone = in_score_zone(position, board.score_zones);
    int actions = 0;
    bool fired_this_turn = false;
    HunterState& hunter = state();

    // 出生点再次出现表示刚复活；下一次进攻改选另一条中心入口。
    if (same_pos(position, Pos{0, 0}) && !same_pos(hunter.previous_pos, position)) {
        if (!hunter.route_initialized) {
            hunter.route_kind = static_cast<int>(hunter.next_random() % 4U);
            hunter.route_initialized = true;
        } else {
            hunter.route_kind = (hunter.route_kind + 1 +
                                 static_cast<int>(hunter.next_random() % 3U)) % 4;
        }
        hunter.route_target = {-1, -1};
        hunter.route_waypoint = hunter.route_kind == 1 ? Pos{2, 1}
                               : hunter.route_kind == 3 ? Pos{1, 2} : Pos{-1, -1};
        hunter.spawn_turn_pending = true;
        hunter.skip_scan_once = true;
    }
    hunter.previous_pos = position;

    // SCAN 是超视距攻击的入口；开火后仅跳过下一个己方回合。
    if (!hunter.skip_scan_once && me.fire_cd == 0 && me.scan_cd == 0) {
        const ScanResult result = scan();
        if (result.success) {
            actions += result.consumed ? 1 : 0;
            enemy = result.observation.opp_last_known_pos;
            enemy_facing = result.observation.opp_last_known_facing;
            enemy_known = true;
        }
    }
    hunter.skip_scan_once = false;

    if (enemy_known && me.fire_cd == 0) {
        const char firing_direction = best_turn_to_face(position, enemy);

        // 已处于火力范围时，SCAN 后用 TURN+FIRE 立即击杀。
        if (in_fire_range(position, firing_direction, enemy)) {
            if (facing != firing_direction) {
                const ActionResult turned = turn(firing_direction);
                if (!turned.success) return;
                actions += turned.consumed ? 1 : 0;
                facing = firing_direction;
            }
            const ActionResult fired = fire();
            if (fired.success) {
                ++actions;
                fired_this_turn = true;
                enemy_known = false;
            }
        }

        // 超视距前压：当前朝向不变时，一步 MOVE 后落入 3×3 火力范围，立即 MOVE+FIRE。
        if (!fired_this_turn && !holding_score_zone && actions <= 1 &&
            can_step_fire(board, position, facing, enemy)) {
            const ActionResult moved = move();
            if (!moved.success) return;
            actions += moved.consumed ? 1 : 0;
            position = moved.observation.my_pos;
            const ActionResult fired = fire();
            if (fired.success) {
                ++actions;
                fired_this_turn = true;
                enemy_known = false;
            }
        }
    }

    // 没有本回合必杀时，优先向中心得分区前压；不为追踪旧敌情偏离占点路线。
    if (holding_score_zone) {
        hunter.route_target = {-1, -1};
        hunter.route_waypoint = {-1, -1};
        return;
    }
    const Pos target = route_destination(board, position);
    ActionObservation observation{};
    if (hunter.spawn_turn_pending) {
        const char route_direction = hunter.route_kind < 2 ? 'E' : 'S';
        if (facing != route_direction && actions < 3) {
            const ActionResult turned = turn(route_direction);
            if (!turned.success) return;
            actions += turned.consumed ? 1 : 0;
            facing = route_direction;
        }
        if (actions < 3) {
            const ActionResult first_move = move();
            if (!first_move.success) return;
            actions += first_move.consumed ? 1 : 0;
            position = first_move.observation.my_pos;
            observation = first_move.observation;
        }
        hunter.spawn_turn_pending = false;
    }
    actions += advance_toward(board, position, facing,
                              enemy_known ? enemy : Pos{-1, -1}, target,
                              3 - actions, &observation, false,
                              enemy_known ? &enemy : nullptr, enemy_facing);

    // 前压途中获得实时视野后，立刻用剩余行动反击。
    if (observation.opp_visible && observation.fire_cd == 0 && actions < 3) {
        const char firing_direction = best_turn_to_face(position, observation.opp_last_known_pos);
        if (facing != firing_direction && actions < 2) {
            const ActionResult turned = turn(firing_direction);
            if (turned.success) {
                actions += turned.consumed ? 1 : 0;
                facing = firing_direction;
            }
        }
        if (actions < 3 && facing == firing_direction) {
            fire_if_in_range(position, facing, observation.opp_last_known_pos, actions);
        }
    }
}
