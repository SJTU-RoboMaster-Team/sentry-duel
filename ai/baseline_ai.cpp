// baseline_ai.cpp - 占点+攻击 AI(⭐⭐⭐☆☆)
// 已在得分区:留在原地攻击
// 不在得分区:朝中心走,沿途攻击
// 维护上次 SCAN 位置用于估算

#include "sentry_duel.h"
#include "utils.h"
#include "navigation.h"

#include <chrono>
#include <cstdint>

namespace {

class BaselineState {
public:
    std::uint32_t random;
    Pos route_target = {-1, -1};
    Pos previous_pos = {-1, -1};
    int route_side = 0;
    bool route_initialized = false;
    bool spawn_turn_pending = false;

    BaselineState()
        : random(static_cast<std::uint32_t>(
              std::chrono::steady_clock::now().time_since_epoch().count())) {}

    bool coin_flip() {
        random ^= random << 13;
        random ^= random >> 17;
        random ^= random << 5;
        return (random & 1U) != 0;
    }
};

BaselineState& state() {
    static BaselineState value;
    return value;
}

Pos choose_entry() {
    const Pos right_entry = {3, 2};
    const Pos lower_entry = {2, 3};
    return state().route_side == 0 ? right_entry : lower_entry;
}

Pos route_entry() {
    BaselineState& baseline = state();
    if (baseline.route_target.x < 0) {
        baseline.route_target = choose_entry();
    }
    return baseline.route_target;
}

} // namespace

extern "C" void act(const Board& board, char my_color) {
    const Sentry& me  = (my_color == 'R') ? board.red  : board.blue;
    const Sentry& opp = (my_color == 'R') ? board.blue : board.red;
    int actions = 0;
    BaselineState& baseline = state();

    if (same_pos(me.last_known_pos, Pos{0, 0}) &&
        !same_pos(baseline.previous_pos, me.last_known_pos)) {
        if (!baseline.route_initialized) {
            baseline.route_side = baseline.coin_flip() ? 1 : 0;
            baseline.route_initialized = true;
        } else {
            baseline.route_side = 1 - baseline.route_side;
        }
        baseline.route_target = {-1, -1};
        baseline.spawn_turn_pending = true;
    }
    baseline.previous_pos = me.last_known_pos;

    // 视野中的敌人必定处于 3×3 火力范围；fire_cd 为 0 时立即斩杀。
    if (opp.visible && me.fire_cd == 0) {
        const char firing_direction = best_turn_to_face(me.last_known_pos, opp.last_known_pos);
        if (me.last_known_facing != firing_direction) {
            turn(firing_direction);
            ++actions;
        }
        fire();
        ++actions;
        if (me.scan_cd == 0) {
            scan();
            ++actions;
        }
        return;
    }

    if (me.scan_cd == 0) {
        const ScanResult result = scan();
        if (result.success) ++actions;
        const char firing_direction = best_turn_to_face(me.last_known_pos,
                                                         result.observation.opp_last_known_pos);
        if (me.fire_cd == 0 && in_fire_range(me.last_known_pos, firing_direction,
                                               result.observation.opp_last_known_pos)) {
            if (me.last_known_facing != firing_direction) {
                turn(firing_direction);
                ++actions;
            }
            fire();
            ++actions;
            return;
        }
    }

    Pos position = me.last_known_pos;
    char facing = me.last_known_facing;
    if (in_score_zone(position, board.score_zones)) {
        baseline.route_target = {-1, -1};
        return;
    }
    ActionObservation observation{};
    const Pos target = route_entry();
    if (baseline.spawn_turn_pending) {
        const char route_direction = baseline.route_side == 0 ? 'E' : 'S';
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
        baseline.spawn_turn_pending = false;
    }
    actions += advance_toward(board, position, facing, opp.last_known_pos,
                              target, 3 - actions, &observation, false);

    // MOVE/TURN 后返回的是实时观测；一旦新视野发现敌人，立即用剩余行动反击。
    if (observation.opp_visible && observation.fire_cd == 0 && actions < 3) {
        const char firing_direction = best_turn_to_face(position, observation.opp_last_known_pos);
        if (facing != firing_direction && actions < 2) {
            const ActionResult turned = turn(firing_direction);
            if (turned.success) { ++actions; facing = firing_direction; }
        }
        if (actions < 3 && facing == firing_direction && fire().success) ++actions;
    }

    // 行动不足 3 次时直接结束回合，避免重复 TURN 到当前朝向。
}
