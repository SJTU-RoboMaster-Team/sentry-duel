// runner.cpp - 对局调度、选手视图与事件流

#include "sentry_duel.h"
#include "utils.h"
#include "engine_internal.h"
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <csetjmp>
#include <csignal>
#include <dlfcn.h>
#include <string>
#include <sys/time.h>

static sigjmp_buf timeout_jmp;
static void on_sigalrm(int) { siglongjmp(timeout_jmp, 1); }
static void on_sigsegv(int) { siglongjmp(timeout_jmp, 2); }

struct SideState {
    void* handle = nullptr;
    void (*act_fn)(const Board&, char) = nullptr;
    std::string name;
};

struct Intel {
    Pos pos = {-1, -1};
    char facing = '?';
    bool known = false;
};

static Board g_board;                 // 仅引擎使用的真实世界坐标
static Intel g_red_intel, g_blue_intel; // 分别是红/蓝对敌方的最后已知情报
static char g_action_side = 'R';
static int g_action_count = 0;
static bool g_target_visible = false;
static bool g_scanned_this_act = false;
static bool g_free_turn_available = false;
static bool g_red_respawn_turn_pending = false;
static bool g_blue_respawn_turn_pending = false;
static constexpr int MAX_ACTIONS_PER_TURN = 3;

static Sentry& sentry_for(Board& board, char side) {
    return side == 'R' ? board.red : board.blue;
}

static Intel& intel_for(char side) {
    return side == 'R' ? g_red_intel : g_blue_intel;
}

static char opponent_of(char side) { return side == 'R' ? 'B' : 'R'; }

static bool is_at_spawn(const Board& board, char side) {
    const Pos& pos = side == 'R' ? board.red.last_known_pos : board.blue.last_known_pos;
    const Pos spawn = side == 'R' ? Pos{0, 0} : Pos{board.size - 1, board.size - 1};
    return pos.x == spawn.x && pos.y == spawn.y;
}

static bool& respawn_turn_pending(char side) {
    return side == 'R' ? g_red_respawn_turn_pending : g_blue_respawn_turn_pending;
}

static Pos mirror_pos(Pos pos, int size) {
    return {size - 1 - pos.x, size - 1 - pos.y};
}

static char mirror_facing(char facing) {
    switch (facing) {
        case 'N': return 'S';
        case 'E': return 'W';
        case 'S': return 'N';
        case 'W': return 'E';
        default: return '?';
    }
}

static char world_facing(char local_facing, char side) {
    return side == 'B' ? mirror_facing(local_facing) : local_facing;
}

static Pos local_pos(Pos world, char side) {
    return side == 'B' ? mirror_pos(world, g_board.size) : world;
}

static char local_facing(char world, char side) {
    return side == 'B' ? mirror_facing(world) : world;
}

static void remember_enemy(char side);
static bool direct_vision(char side);

static ActionObservation current_observation() {
    const char side = g_action_side;
    const Sentry& me = sentry_for(g_board, side);
    const bool directly_visible = direct_vision(side);
    const bool visible = g_target_visible || directly_visible;
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

static void remember_enemy(char side) {
    const Sentry& enemy = sentry_for(g_board, opponent_of(side));
    Intel& intel = intel_for(side);
    intel.pos = enemy.last_known_pos;
    intel.facing = enemy.last_known_facing;
    intel.known = true;
}

static bool direct_vision(char side) {
    const Sentry& me = sentry_for(g_board, side);
    const Sentry& enemy = sentry_for(g_board, opponent_of(side));
    return can_see(me, enemy.last_known_pos, g_board.obstacles);
}

static void refresh_current_vision() {
    if (direct_vision(g_action_side)) remember_enemy(g_action_side);
    g_target_visible = g_scanned_this_act || direct_vision(g_action_side);
}

// 为选手构造坐标统一、无敌情泄漏的快照。蓝方会获得 180° 旋转后的棋盘。
static Board view_for(char side) {
    const bool visible = direct_vision(side);
    if (visible) remember_enemy(side);
    const Intel& intel = intel_for(side);
    Board view = g_board;

    if (side == 'B') {
        view.red.last_known_pos = mirror_pos(view.red.last_known_pos, view.size);
        view.blue.last_known_pos = mirror_pos(view.blue.last_known_pos, view.size);
        view.red.last_known_facing = mirror_facing(view.red.last_known_facing);
        view.blue.last_known_facing = mirror_facing(view.blue.last_known_facing);
        for (Pos& obstacle : view.obstacles) obstacle = mirror_pos(obstacle, view.size);
        for (Pos& zone : view.score_zones) zone = mirror_pos(zone, view.size);
    }

    Sentry& me = sentry_for(view, side);
    Sentry& enemy = sentry_for(view, opponent_of(side));
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

static void emit_board_snapshot() {
    const Board& board = g_board;
    const bool red_visible = direct_vision('R');
    const bool blue_visible = direct_vision('B');
    std::printf("\"red_pos\":[%d,%d],\"blue_pos\":[%d,%d],"
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
}

static void emit_positions(const std::vector<Pos>& positions) {
    std::printf("[");
    for (std::size_t i = 0; i < positions.size(); ++i) {
        if (i > 0) std::printf(",");
        std::printf("[%d,%d]", positions[i].x, positions[i].y);
    }
    std::printf("]");
}

static void emit_action(const char* action, char arg, bool has_arg, bool success,
                        bool consumed = false) {
    std::printf("{\"type\":\"action\",\"turn\":%d,\"side\":\"%c\",\"action\":\"%s\"",
                g_board.turn, g_action_side, action);
    if (has_arg) std::printf(",\"arg\":\"%c\"", arg);
    std::printf(",\"success\":%s,\"consumed\":%s,",
                success ? "true" : "false", consumed ? "true" : "false");
    emit_board_snapshot();
    std::printf("}\n");
    std::fflush(stdout);
}

static ActionResult do_action(int action, char local_arg, const char* name, bool has_arg) {
    if (g_action_count >= MAX_ACTIONS_PER_TURN) {
        emit_action(name, local_arg, has_arg, false);
        return {false, false, current_observation()};
    }
    if (g_free_turn_available && !is_at_spawn(g_board, g_action_side)) {
        g_free_turn_available = false;
    }
    const ActionOutcome outcome = apply_action(g_board, g_action_side, action,
                                                action == 1 ? world_facing(local_arg, g_action_side) : 0);
    if (outcome.success) {
        bool consumed = true;
        if (action == 1 && g_free_turn_available && is_at_spawn(g_board, g_action_side)) {
            g_free_turn_available = false;
            consumed = false;
        } else {
            ++g_action_count;
        }
        if (action == 3) {
            g_scanned_this_act = true;
            remember_enemy(g_action_side);
        } else if (outcome.hit) {
            respawn_turn_pending(opponent_of(g_action_side)) = true;
        }
        refresh_current_vision();
        emit_action(name, local_arg, has_arg, true, consumed);
        return {true, consumed, current_observation()};
    }
    emit_action(name, local_arg, has_arg, outcome.success);
    return {false, false, current_observation()};
}

extern "C" ActionResult move() { return do_action(0, 0, "move", false); }
extern "C" ActionResult turn(char c) { return do_action(1, c, "turn", true); }
extern "C" ActionResult fire() { return do_action(2, 0, "fire", false); }

extern "C" ScanResult scan() {
    const ActionResult result = do_action(3, 0, "scan", false);
    return {result.success, result.consumed, result.observation};
}

static void install_timeout() {
    itimerval timer = {};
    timer.it_value.tv_sec = 1;
    setitimer(ITIMER_REAL, &timer, nullptr);
}

static void clear_timeout() {
    itimerval timer = {};
    setitimer(ITIMER_REAL, &timer, nullptr);
}

// 返回 0=完成，1=超时，2=SIGSEGV。
static int call_ai_act(SideState& side) {
    const char side_ch = side.name == "red" ? 'R' : 'B';
    const Board view = view_for(side_ch);
    g_action_side = side_ch;
    g_action_count = 0;
    g_free_turn_available = respawn_turn_pending(side_ch);
    respawn_turn_pending(side_ch) = false;
    g_scanned_this_act = false;
    g_target_visible = direct_vision(side_ch);

    int rc = sigsetjmp(timeout_jmp, 1);
    if (rc != 0) {
        clear_timeout();
        return rc;
    }
    struct sigaction alarm_action = {}, segv_action = {};
    alarm_action.sa_handler = on_sigalrm;
    segv_action.sa_handler = on_sigsegv;
    sigemptyset(&alarm_action.sa_mask);
    sigemptyset(&segv_action.sa_mask);
    sigaction(SIGALRM, &alarm_action, nullptr);
    sigaction(SIGSEGV, &segv_action, nullptr);
    install_timeout();
    try {
        side.act_fn(view, side_ch);
    } catch (...) {
        clear_timeout();
        return 2;
    }
    clear_timeout();
    return 0;
}

static SideState load_ai(const std::string& path, const std::string& name) {
    SideState side;
    side.name = name;
    side.handle = dlopen(path.c_str(), RTLD_NOW | RTLD_LOCAL);
    if (!side.handle) {
        std::fprintf(stderr, "[engine] dlopen %s 失败: %s\n", path.c_str(), dlerror());
        std::exit(1);
    }
    side.act_fn = reinterpret_cast<void (*)(const Board&, char)>(dlsym(side.handle, "act"));
    if (!side.act_fn) {
        std::fprintf(stderr, "[engine] dlsym(act) 失败: %s\n", dlerror());
        std::exit(1);
    }
    return side;
}

int main(int argc, char** argv) {
    std::string red_path, blue_path;
    int max_turns = 20;
    int game_id = 0;
    for (int i = 1; i < argc; ++i) {
        if (std::strcmp(argv[i], "--red") == 0 && i + 1 < argc) red_path = argv[++i];
        else if (std::strcmp(argv[i], "--blue") == 0 && i + 1 < argc) blue_path = argv[++i];
        else if (std::strcmp(argv[i], "--max-turns") == 0 && i + 1 < argc) max_turns = std::atoi(argv[++i]);
        else if (std::strcmp(argv[i], "--game-id") == 0 && i + 1 < argc) game_id = std::atoi(argv[++i]);
        else { std::fprintf(stderr, "用法: %s --red X.so --blue Y.so [--max-turns 20] [--game-id 0]\n", argv[0]); return 1; }
    }
    if (red_path.empty() || blue_path.empty() || max_turns < 1) {
        std::fprintf(stderr, "必须指定双方 AI，且 max-turns 至少为 1\n");
        return 1;
    }

    SideState red = load_ai(red_path, "red");
    SideState blue = load_ai(blue_path, "blue");
    g_board = make_initial_board(7);
    g_red_respawn_turn_pending = true;
    g_blue_respawn_turn_pending = true;
    std::printf("{\"type\":\"start\",\"game_id\":%d,\"red\":\"%s\",\"blue\":\"%s\",\"size\":%d,",
                game_id, red_path.c_str(), blue_path.c_str(), g_board.size);
    emit_board_snapshot();
    std::printf(",\"obstacles\":");
    emit_positions(g_board.obstacles);
    std::printf(",\"score_zones\":");
    emit_positions(g_board.score_zones);
    std::printf("}\n");
    std::fflush(stdout);

    int winner = 0;
    std::string reason;
    const int regulation_turns = max_turns;
    const int overtime_limit = 5;
    bool overtime = false;
    while (winner == 0 && g_board.turn < regulation_turns + (overtime ? overtime_limit : 0)) {
        std::printf("{\"type\":\"turn_start\",\"turn\":%d}\n", g_board.turn);
        std::fflush(stdout);
        const int red_result = call_ai_act(red);
        if (red_result == 2) { winner = 2; reason = "red_crashed"; break; }
        if (red_result == 1) { g_board.blue.score++; std::fprintf(stderr, "[engine] red AI 超时，蓝方 +1\n"); }
        end_side_turn(g_board, 'R');
        const int blue_result = call_ai_act(blue);
        if (blue_result == 2) { winner = 1; reason = "blue_crashed"; break; }
        if (blue_result == 1) { g_board.red.score++; std::fprintf(stderr, "[engine] blue AI 超时，红方 +1\n"); }
        end_side_turn(g_board, 'B');
        end_round(g_board);
        const bool red_visible = direct_vision('R');
        const bool blue_visible = direct_vision('B');
        std::printf("{\"type\":\"turn_end\",\"turn\":%d,\"red_pos\":[%d,%d],\"blue_pos\":[%d,%d],"
                    "\"red_score\":%d,\"blue_score\":%d,\"red_visible\":%s,\"blue_visible\":%s}\n",
                    g_board.turn - 1, g_board.red.last_known_pos.x, g_board.red.last_known_pos.y,
                    g_board.blue.last_known_pos.x, g_board.blue.last_known_pos.y,
                    g_board.red.score, g_board.blue.score,
                    red_visible ? "true" : "false", blue_visible ? "true" : "false");
        std::fflush(stdout);

        if (!overtime && g_board.turn == regulation_turns && g_board.red.score == g_board.blue.score) overtime = true;
        if (overtime && g_board.turn > regulation_turns && g_board.red.score != g_board.blue.score) {
            winner = g_board.red.score > g_board.blue.score ? 1 : 2;
            reason = "overtime_score";
        }
    }

    if (winner == 0) {
        if (g_board.red.score > g_board.blue.score) { winner = 1; reason = overtime ? "overtime_score" : "score"; }
        else if (g_board.blue.score > g_board.red.score) { winner = 2; reason = overtime ? "overtime_score" : "score"; }
        else { winner = 3; reason = overtime ? "overtime_draw" : "draw"; }
    }
    const char* winner_text = winner == 1 ? "R" : winner == 2 ? "B" : "D";
    std::printf("{\"type\":\"game_over\",\"winner\":\"%s\",\"reason\":\"%s\",\"red_score\":%d,\"blue_score\":%d,\"turns\":%d}\n",
                winner_text, reason.c_str(), g_board.red.score, g_board.blue.score, g_board.turn);
    std::fflush(stdout);
    dlclose(red.handle);
    dlclose(blue.handle);
    return 0;
}
