// match.h - 无全局状态的对局对象(供 runner 与 RL 训练环境使用)
//
// 用法:
//   sentry::Match match(red_policy, blue_policy, "red.so", "blue.so",
//                       20, 0, [](const std::string& line) { /* 收集事件 */ });
//   int winner = match.run();  // 1=红 2=蓝 3=平局
//
// 策略返回值约定:0=正常 1=超时 2=崩溃。
// 超时/崩溃守护(setitimer/信号)由调用方(如 runner)在策略包装里完成,
// Match 只根据返回码执行罚分/判负语义,与历史行为一致。
//
// 选手 .so 兼容:match.cpp 内实现全局 extern "C" move/turn/fire/scan,
// 经 thread_local 的"当前 Match"转发,dlopen 加载的既有 .so 无需修改。

#pragma once

#include "sentry_duel.h"

#include <functional>
#include <string>

namespace sentry {

class Match {
public:
    // 策略接口:输入选手视图与行动方('R'/'B'),返回 0=正常 1=超时 2=崩溃
    using Policy = std::function<int(const Board&, char)>;
    // 事件输出:每次回调一行的 JSON 字符串(不含换行);不注入则默认打印到 stdout
    using Emit = std::function<void(const std::string&)>;

    // red_name/blue_name 仅用于 start 事件展示(runner 传 .so 路径)
    Match(Policy red_policy, Policy blue_policy,
          std::string red_name, std::string blue_name,
          int max_turns = 20, int game_id = 0, Emit emit = Emit{});

    // 跑完一整局(20 回合 + 平分加时最多 5 回合),返回胜者:1=红 2=蓝 3=平局
    int run();

    int winner() const { return winner_; }          // run() 结束后有效
    const std::string& reason() const { return reason_; }
    const Board& board() const { return board_; }   // 真实世界坐标棋盘

    // 行动结算入口(全局 shim 转发到这里,语义 = 原 runner 的 do_action)
    ActionResult act_move();
    ActionResult act_turn(char local_arg);
    ActionResult act_fire();
    ScanResult act_scan();
    void current_scores(int& my_score, int& opp_score) const;

private:
    // 一方对敌方的最后已知情报
    struct Intel {
        Pos pos = {-1, -1};
        char facing = '?';
        bool known = false;
    };

    Sentry& sentry_for(char side);
    const Sentry& sentry_for(char side) const;
    Intel& intel_for(char side);
    const Intel& intel_for(char side) const;
    bool& respawn_turn_pending(char side);

    // —— 视野/情报(移植自原 runner.cpp)——
    char world_facing(char local_facing, char side) const;
    Pos local_pos(Pos world, char side) const;
    char local_facing(char world, char side) const;
    void remember_enemy(char side);
    bool direct_vision(char side) const;
    ActionObservation current_observation();
    void refresh_current_vision();
    Board view_for(char side);

    // —— 事件输出(逐行 JSON,格式与原 runner 逐字节一致)——
    void emit_line(const std::string& line);
    std::string board_snapshot_json() const;
    static std::string positions_json(const std::vector<Pos>& positions);
    void emit_action(const char* action, char arg, bool has_arg, bool success, bool consumed = false);

    // —— 对局推进 ——
    ActionResult do_action(int action, char local_arg, const char* name, bool has_arg);
    int act_phase(char side);   // 设置行动上下文并调用策略,返回策略返回码

    Policy red_policy_;
    Policy blue_policy_;
    std::string red_name_;
    std::string blue_name_;
    int max_turns_;
    int game_id_;
    Emit emit_;

    Board board_{};                       // 仅引擎使用的真实世界坐标
    Intel red_intel_, blue_intel_;        // 分别是红/蓝对敌方的最后已知情报
    char action_side_ = 'R';
    int action_count_ = 0;
    bool target_visible_ = false;
    bool scanned_this_act_ = false;
    bool free_turn_available_ = false;
    bool red_respawn_turn_pending_ = false;
    bool blue_respawn_turn_pending_ = false;
    int winner_ = 0;
    std::string reason_;
    static constexpr int MAX_ACTIONS_PER_TURN = 3;
};

} // namespace sentry
