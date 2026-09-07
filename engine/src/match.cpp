// match.cpp - 无全局状态的对局对象实现
// 由原 runner.cpp 的对局调度逻辑重构而来:全局状态全部成员化,
// 事件通过构造时注入的 emit 回调逐行输出(每行一个 JSON 字符串,不含换行)。
// 规则判定继续复用 board.cpp/utils.cpp,不在此处复制。

#include "match.h"
#include "engine_internal.h"
#include "utils.h"

#include <cstdio>
#include <utility>
#include <vector>

namespace {

// 当前正在结算行动的 Match(选手 .so 的全局行动函数经此转发)。
// thread_local 保证多线程并行跑多局时互不干扰。
thread_local sentry::Match* g_current_match = nullptr;

char opponent_of(char side) { return side == 'R' ? 'B' : 'R'; }

bool is_at_spawn(const Board& board, char side) {
    const Pos& pos = side == 'R' ? board.red.last_known_pos : board.blue.last_known_pos;
    const Pos spawn = side == 'R' ? Pos{0, 0} : Pos{board.size - 1, board.size - 1};
    return pos.x == spawn.x && pos.y == spawn.y;
}

Pos mirror_pos(Pos pos, int size) {
    return {size - 1 - pos.x, size - 1 - pos.y};
}

char mirror_facing(char facing) {
    switch (facing) {
        case 'N': return 'S';
        case 'E': return 'W';
        case 'S': return 'N';
        case 'W': return 'E';
        default: return '?';
    }
}

} // namespace

namespace sentry {

Match::Match(Policy red_policy, Policy blue_policy,
             std::string red_name, std::string blue_name,
             int max_turns, int game_id, Emit emit)
    : red_policy_(std::move(red_policy)),
      blue_policy_(std::move(blue_policy)),
      red_name_(std::move(red_name)),
      blue_name_(std::move(blue_name)),
      max_turns_(max_turns),
      game_id_(game_id),
      emit_(std::move(emit)) {}

Sentry& Match::sentry_for(char side) {
    return side == 'R' ? board_.red : board_.blue;
}

const Sentry& Match::sentry_for(char side) const {
    return side == 'R' ? board_.red : board_.blue;
}

Match::Intel& Match::intel_for(char side) {
    return side == 'R' ? red_intel_ : blue_intel_;
}

const Match::Intel& Match::intel_for(char side) const {
    return side == 'R' ? red_intel_ : blue_intel_;
}

bool& Match::respawn_turn_pending(char side) {
    return side == 'R' ? red_respawn_turn_pending_ : blue_respawn_turn_pending_;
}

char Match::world_facing(char local_facing, char side) const {
    return side == 'B' ? mirror_facing(local_facing) : local_facing;
}

Pos Match::local_pos(Pos world, char side) const {
    return side == 'B' ? mirror_pos(world, board_.size) : world;
}

char Match::local_facing(char world, char side) const {
    return side == 'B' ? mirror_facing(world) : world;
}

void Match::remember_enemy(char side) {
    const Sentry& enemy = sentry_for(opponent_of(side));
    Intel& intel = intel_for(side);
    intel.pos = enemy.last_known_pos;
    intel.facing = enemy.last_known_facing;
    intel.known = true;
}

bool Match::direct_vision(char side) const {
    const Sentry& me = sentry_for(side);
    const Sentry& enemy = sentry_for(opponent_of(side));
    return can_see(me, enemy.last_known_pos, board_.obstacles);
}

ActionObservation Match::current_observation() {
    const char side = action_side_;
    const Sentry& me = sentry_for(side);
    const bool directly_visible = direct_vision(side);
    const bool visible = target_visible_ || directly_visible;
    if (visible) remember_enemy(side);
    const Intel& intel = intel_for(side);
    ActionObservation observation{};
    observation.my_pos = local_pos(me.last_known_pos, side);
    observation.my_facing = local_facing(me.last_known_facing, side);
    observation.opp_visible = visible;
    observation.opp_directly_visible = directly_visible;
    observation.opp_last_known_pos = intel.known ? local_pos(intel.pos, side) : Pos{-1, -1};
    observation.opp_last_known_facing = intel.known ? local_facing(intel.facing, side) : '?';
    observation.fire_cd = me.fire_cd;
    observation.scan_cd = me.scan_cd;
    return observation;
}

void Match::current_scores(int& my_score, int& opp_score) const {
    const Sentry& me = sentry_for(action_side_);
    const Sentry& opp = sentry_for(opponent_of(action_side_));
    my_score = me.score;
    opp_score = opp.score;
}

void Match::refresh_current_vision() {
    if (direct_vision(action_side_)) remember_enemy(action_side_);
    target_visible_ = scanned_this_act_ || direct_vision(action_side_);
}

// 为选手构造坐标统一、无敌情泄漏的快照。蓝方会获得 180° 旋转后的棋盘。
Board Match::view_for(char side) {
    const bool visible = direct_vision(side);
    if (visible) remember_enemy(side);
    const Intel& intel = intel_for(side);
    Board view = board_;

    if (side == 'B') {
        view.red.last_known_pos = mirror_pos(view.red.last_known_pos, view.size);
        view.blue.last_known_pos = mirror_pos(view.blue.last_known_pos, view.size);
        view.red.last_known_facing = mirror_facing(view.red.last_known_facing);
        view.blue.last_known_facing = mirror_facing(view.blue.last_known_facing);
        for (Pos& obstacle : view.obstacles) obstacle = mirror_pos(obstacle, view.size);
        for (Pos& zone : view.score_zones) zone = mirror_pos(zone, view.size);
    }

    Sentry& me = side == 'R' ? view.red : view.blue;
    Sentry& enemy = side == 'R' ? view.blue : view.red;
    me.visible = true;
    enemy.visible = visible;
    enemy.fire_cd = -1;
    enemy.scan_cd = -1;
    if (intel.known) {
        enemy.last_known_pos = local_pos(intel.pos, side);
        enemy.last_known_facing = local_facing(intel.facing, side);
    } else {
        enemy.last_known_pos = {-1, -1};
        enemy.last_known_facing = '?';
    }
    return view;
}

void Match::emit_line(const std::string& line) {
    if (emit_) {
        emit_(line);
        return;
    }
    // 未注入 emit 时默认打印到 stdout(与 runner 行为一致)
    std::printf("%s\n", line.c_str());
    std::fflush(stdout);
}

std::string Match::board_snapshot_json() const {
    const Board& board = board_;
    const bool red_visible = direct_vision('R');
    const bool blue_visible = direct_vision('B');
    char buf[512];
    std::snprintf(buf, sizeof buf,
                  "\"red_pos\":[%d,%d],\"blue_pos\":[%d,%d],"
                  "\"red_facing\":\"%c\",\"blue_facing\":\"%c\","
                  "\"red_fire_cd\":%d,\"blue_fire_cd\":%d,"
                  "\"red_scan_cd\":%d,\"blue_scan_cd\":%d,"
                  "\"red_score\":%d,\"blue_score\":%d,"
                  "\"red_visible\":%s,\"blue_visible\":%s",
                  board.red.last_known_pos.x, board.red.last_known_pos.y,
                  board.blue.last_known_pos.x, board.blue.last_known_pos.y,
                  board.red.last_known_facing, board.blue.last_known_facing,
                  board.red.fire_cd, board.blue.fire_cd,
                  board.red.scan_cd, board.blue.scan_cd,
                  board.red.score, board.blue.score,
                  red_visible ? "true" : "false", blue_visible ? "true" : "false");
    return buf;
}

std::string Match::positions_json(const std::vector<Pos>& positions) {
    std::string out = "[";
    for (std::size_t i = 0; i < positions.size(); ++i) {
        if (i > 0) out += ",";
        char buf[32];
        std::snprintf(buf, sizeof buf, "[%d,%d]", positions[i].x, positions[i].y);
        out += buf;
    }
    out += "]";
    return out;
}

void Match::emit_action(const char* action, char arg, bool has_arg, bool success, bool consumed) {
    char head[256];
    std::snprintf(head, sizeof head,
                  "{\"type\":\"action\",\"turn\":%d,\"side\":\"%c\",\"action\":\"%s\"",
                  board_.turn, action_side_, action);
    std::string line = head;
    if (has_arg) {
        char arg_buf[16];
        std::snprintf(arg_buf, sizeof arg_buf, ",\"arg\":\"%c\"", arg);
        line += arg_buf;
    }
    line += ",\"success\":";
    line += success ? "true" : "false";
    line += ",\"consumed\":";
    line += consumed ? "true" : "false";
    line += ",";
    line += board_snapshot_json();
    line += "}";
    emit_line(line);
}

ActionResult Match::do_action(int action, char local_arg, const char* name, bool has_arg) {
    if (action_count_ >= MAX_ACTIONS_PER_TURN) {
        emit_action(name, local_arg, has_arg, false, false);
        return {false, false, current_observation()};
    }
    if (free_turn_available_ && !is_at_spawn(board_, action_side_)) {
        free_turn_available_ = false;
    }
    const ActionOutcome outcome = apply_action(board_, action_side_, action,
                                               action == 1 ? world_facing(local_arg, action_side_) : 0);
    if (outcome.success) {
        bool consumed = true;
        if (action == 1 && free_turn_available_ && is_at_spawn(board_, action_side_)) {
            free_turn_available_ = false;
            consumed = false;
        } else {
            ++action_count_;
        }
        if (action == 3) {
            scanned_this_act_ = true;
            remember_enemy(action_side_);
        } else if (outcome.hit) {
            scanned_this_act_ = false;
            respawn_turn_pending(opponent_of(action_side_)) = true;
        }
        refresh_current_vision();
        emit_action(name, local_arg, has_arg, true, consumed);
        return {true, consumed, current_observation()};
    }
    emit_action(name, local_arg, has_arg, outcome.success, false);
    return {false, false, current_observation()};
}

ActionResult Match::act_move() { return do_action(0, 0, "move", false); }
ActionResult Match::act_turn(char local_arg) { return do_action(1, local_arg, "turn", true); }
ActionResult Match::act_fire() { return do_action(2, 0, "fire", false); }

ScanResult Match::act_scan() {
    const ActionResult result = do_action(3, 0, "scan", false);
    return {result.success, result.consumed, result.observation};
}

// 设置行动上下文并调用策略,返回策略返回码(0=正常 1=超时 2=崩溃)
int Match::act_phase(char side) {
    const Board view = view_for(side);
    action_side_ = side;
    action_count_ = 0;
    free_turn_available_ = respawn_turn_pending(side);
    respawn_turn_pending(side) = false;
    scanned_this_act_ = false;
    target_visible_ = direct_vision(side);

    Policy& policy = (side == 'R') ? red_policy_ : blue_policy_;
    if (!policy) return 0;   // 空策略视为本回合不行动
    g_current_match = this;
    const int rc = policy(view, side);
    g_current_match = nullptr;
    return rc;
}

int Match::run() {
    board_ = make_initial_board(7);
    // 初始出生与被击杀后的复活规则一致：双方首回合各有一次免费转向。
    red_respawn_turn_pending_ = true;
    blue_respawn_turn_pending_ = true;

    // start 事件
    {
        char head[512];
        std::snprintf(head, sizeof head,
                      "{\"type\":\"start\",\"game_id\":%d,\"red\":\"%s\",\"blue\":\"%s\",\"size\":%d,",
                      game_id_, red_name_.c_str(), blue_name_.c_str(), board_.size);
        std::string line = head;
        line += board_snapshot_json();
        line += ",\"obstacles\":";
        line += positions_json(board_.obstacles);
        line += ",\"score_zones\":";
        line += positions_json(board_.score_zones);
        line += "}";
        emit_line(line);
    }

    winner_ = 0;
    reason_.clear();
    const int regulation_turns = max_turns_;
    const int overtime_limit = 5;
    bool overtime = false;
    while (winner_ == 0 && board_.turn < regulation_turns + (overtime ? overtime_limit : 0)) {
        {
            char buf[64];
            std::snprintf(buf, sizeof buf, "{\"type\":\"turn_start\",\"turn\":%d}", board_.turn);
            emit_line(buf);
        }
        const int red_result = act_phase('R');
        if (red_result == 2) { winner_ = 2; reason_ = "red_crashed"; break; }
        if (red_result == 1) { board_.blue.score++; }
        end_side_turn(board_, 'R');
        // [实验:取消先手补偿] 原本:蓝方获得红方初始位置情报作为先手补偿
        // if (board_.turn == 0) remember_enemy('B');  // 2026-08-21 关闭,测试胜率

        const int blue_result = act_phase('B');
        if (blue_result == 2) { winner_ = 1; reason_ = "blue_crashed"; break; }
        if (blue_result == 1) { board_.red.score++; }
        end_side_turn(board_, 'B');
        end_round(board_);
        {
            const bool red_visible = direct_vision('R');
            const bool blue_visible = direct_vision('B');
            char buf[256];
            std::snprintf(buf, sizeof buf,
                          "{\"type\":\"turn_end\",\"turn\":%d,\"red_pos\":[%d,%d],\"blue_pos\":[%d,%d],"
                          "\"red_score\":%d,\"blue_score\":%d,\"red_fire_cd\":%d,\"blue_fire_cd\":%d,"
                          "\"red_scan_cd\":%d,\"blue_scan_cd\":%d,\"red_visible\":%s,\"blue_visible\":%s}",
                          board_.turn - 1, board_.red.last_known_pos.x, board_.red.last_known_pos.y,
                          board_.blue.last_known_pos.x, board_.blue.last_known_pos.y,
                          board_.red.score, board_.blue.score,
                          board_.red.fire_cd, board_.blue.fire_cd,
                          board_.red.scan_cd, board_.blue.scan_cd,
                          red_visible ? "true" : "false", blue_visible ? "true" : "false");
            emit_line(buf);
        }

        if (!overtime && board_.turn == regulation_turns && board_.red.score == board_.blue.score) overtime = true;
        if (overtime && board_.turn > regulation_turns && board_.red.score != board_.blue.score) {
            winner_ = board_.red.score > board_.blue.score ? 1 : 2;
            reason_ = "overtime_score";
        }
    }

    if (winner_ == 0) {
        if (board_.red.score > board_.blue.score) { winner_ = 1; reason_ = overtime ? "overtime_score" : "score"; }
        else if (board_.blue.score > board_.red.score) { winner_ = 2; reason_ = overtime ? "overtime_score" : "score"; }
        else { winner_ = 3; reason_ = overtime ? "overtime_draw" : "draw"; }
    }
    {
        const char* winner_text = winner_ == 1 ? "R" : winner_ == 2 ? "B" : "D";
        char buf[256];
        std::snprintf(buf, sizeof buf,
                      "{\"type\":\"game_over\",\"winner\":\"%s\",\"reason\":\"%s\",\"red_score\":%d,\"blue_score\":%d,\"turns\":%d}",
                      winner_text, reason_.c_str(), board_.red.score, board_.blue.score, board_.turn);
        emit_line(buf);
    }
    return winner_;
}

} // namespace sentry

// —— 选手 .so 兼容层 ——
// 全局行动函数:转发到 thread_local 的"当前 Match"(Match 在调用策略前设置自己)。
// 符号位于 libsentry_duel_engine.so 中,runner 与未来工具二进制都能
// 直接 dlopen 既有选手 .so(ABI 与原版完全一致)。
extern "C" ActionResult move() {
    if (!g_current_match) return ActionResult{};
    return g_current_match->act_move();
}

extern "C" ActionResult turn(char c) {
    if (!g_current_match) return ActionResult{};
    return g_current_match->act_turn(c);
}

extern "C" ActionResult fire() {
    if (!g_current_match) return ActionResult{};
    return g_current_match->act_fire();
}

extern "C" ScanResult scan() {
    if (!g_current_match) return ScanResult{};
    return g_current_match->act_scan();
}

extern "C" void scores(int* my_score, int* opp_score) {
    if (!g_current_match || !my_score || !opp_score) return;
    g_current_match->current_scores(*my_score, *opp_score);
}
