// obs_builder.h - 观测构建 + 敌方信念追踪(rl/SPEC.md §2/§5)
//
// 关键设计:本头文件只使用选手可得的公开信息(act() 收到的 Board 视图、
// 行动返回的 ActionObservation、utils.h 工具函数),训练环境与部署 .so 共用
// 同一份代码,从构造上保证训练/部署观测一致,杜绝信息泄漏。
//
// 用法(与选手 act() 的生命周期一致):
//   ObsBuilder ob;             // 每局一个实例(部署侧为静态单例,turn==0 时 reset)
//   ob.reset();                // 开局
//   ob.act_start(view, 'R');   // 每次 act() 开始
//   ob.on_observation(res.observation, res.consumed);  // 每次行动返回后
//   ob.encode(buf);            // buf 为 412 个 float(rl::OBS_DIM)

#pragma once

#include <array>
#include <cstring>
#include <vector>

#include "sentry_duel.h"
#include "utils.h"

namespace rl {

inline constexpr int kBoardSize = 7;
inline constexpr int kCells = kBoardSize * kBoardSize;
inline constexpr int kPlanes = 8;
inline constexpr int kScalars = 20;
inline constexpr int kObsDim = kPlanes * kCells + kScalars;  // 412

class ObsBuilder {
public:
    void reset() {
        belief_.fill(0.0f);
        belief_[cell(6, 6)] = 1.0f;  // 对方出生在镜像视角的 (6,6)
        intel_pos_ = {-1, -1};
        intel_facing_ = '?';
        intel_turn_ = -100;
        my_pos_ = {0, 0};
        my_facing_ = 'E';
        my_score_ = opp_score_ = 0;
        fire_cd_ = scan_cd_ = 0;
        turn_ = 0;
        actions_used_ = 0;
        is_blue_ = false;
        opp_visible_ = opp_directly_visible_ = false;
        free_turn_ = false;
        first_act_ = true;
        prev_act_end_pos_ = {0, 0};
        seen_now_ = {-1, -1};
    }

    // act() 开始:view 为引擎传入的(已镜像)Board 快照。
    void act_start(const Board& view, char my_color) {
        const Sentry& me = (my_color == 'R') ? view.red : view.blue;
        const Sentry& opp = (my_color == 'R') ? view.blue : view.red;
        is_blue_ = (my_color == 'B');
        turn_ = view.turn;
        obstacles_ = view.obstacles;
        zones_ = view.score_zones;

        // --- 击杀/死亡侦查(分数差是公开信息)---
        // 对方 +2 的唯一来源是击杀我(占点只有 +1),比位置判断更可靠
        const int my_delta = me.score - my_score_;
        const int opp_delta = opp.score - opp_score_;
        const bool i_died = opp_delta >= 2 ||
                            (same(me.last_known_pos, Pos{0, 0}) &&
                             !same(me.last_known_pos, prev_act_end_pos_) && !first_act_);
        if (my_delta >= 2) {
            // 我击杀了对方:对方回其出生点(公开规则),情报立刻更新
            belief_.fill(0.0f);
            belief_[cell(6, 6)] = 1.0f;
            intel_pos_ = {6, 6};
            intel_facing_ = 'W';
            intel_turn_ = turn_;
        }
        // 免费 TURN 仅在复活后第一个 act 内有效;本 act 未被杀则收回
        free_turn_ = i_died;

        my_score_ = me.score;
        opp_score_ = opp.score;
        my_pos_ = me.last_known_pos;
        my_facing_ = me.last_known_facing;
        fire_cd_ = me.fire_cd;
        scan_cd_ = me.scan_cd;
        actions_used_ = 0;

        // --- 信念时间推进:敌方自上一来我方 act 起行动过一个阶段 ---
        if (!first_act_) dilate_belief();

        // --- 视野证伪:当前 T 形视野内的格子若有人我必看到 ---
        opp_visible_ = opp.visible;
        opp_directly_visible_ = opp.visible && can_see_me(opp.last_known_pos);
        if (opp.visible && opp.last_known_pos.x >= 0) {
            belief_.fill(0.0f);
            belief_[cell(opp.last_known_pos.x, opp.last_known_pos.y)] = 1.0f;
            intel_pos_ = opp.last_known_pos;
            intel_facing_ = opp.last_known_facing;
            intel_turn_ = turn_;
        } else if (first_act_ && opp.last_known_pos.x >= 0) {
            // 蓝方回合 0 的先手补偿情报:引擎直接给出红方回合末位置(visible=false)
            belief_.fill(0.0f);
            belief_[cell(opp.last_known_pos.x, opp.last_known_pos.y)] = 1.0f;
            intel_pos_ = opp.last_known_pos;
            intel_facing_ = opp.last_known_facing;
            intel_turn_ = turn_;
        }
        if (!opp_directly_visible_) subtract_visible_cells();

        seen_now_ = opp_directly_visible_ ? opp.last_known_pos : Pos{-1, -1};
        first_act_ = false;
    }

    // 每次行动函数返回后调用,更新观测(行动立即结算)。
    void on_observation(const ActionObservation& o, bool consumed) {
        // 朝向变了但额度未消耗 = 复活免费 TURN 被用掉
        if (free_turn_ && !consumed && o.my_facing != my_facing_) free_turn_ = false;
        my_pos_ = o.my_pos;
        my_facing_ = o.my_facing;
        fire_cd_ = o.fire_cd;
        scan_cd_ = o.scan_cd;
        if (consumed) ++actions_used_;
        opp_visible_ = o.opp_visible;
        opp_directly_visible_ = o.opp_directly_visible;
        if (o.opp_visible && o.opp_last_known_pos.x >= 0) {
            belief_.fill(0.0f);
            belief_[cell(o.opp_last_known_pos.x, o.opp_last_known_pos.y)] = 1.0f;
            intel_pos_ = o.opp_last_known_pos;
            intel_facing_ = o.opp_last_known_facing;
            intel_turn_ = turn_;
        }
        if (!opp_directly_visible_) subtract_visible_cells();
        seen_now_ = opp_directly_visible_ ? o.opp_last_known_pos : Pos{-1, -1};
        prev_act_end_pos_ = my_pos_;
    }

    // 当前观测编码到 out[kObsDim]。
    void encode(float* out) const {
        std::memset(out, 0, sizeof(float) * kObsDim);
        // 平面 0:障碍;1:得分区
        for (const Pos& o : obstacles_) out[0 * kCells + cell(o.x, o.y)] = 1.0f;
        for (const Pos& z : zones_) out[1 * kCells + cell(z.x, z.y)] = 1.0f;
        // 平面 2:我方位置
        out[2 * kCells + cell(my_pos_.x, my_pos_.y)] = 1.0f;
        // 平面 3:敌方信念
        std::memcpy(out + 3 * kCells, belief_.data(), sizeof(float) * kCells);
        // 平面 4:当前直接看到
        if (seen_now_.x >= 0) out[4 * kCells + cell(seen_now_.x, seen_now_.y)] = 1.0f;
        // 平面 5:最后已知情报
        if (intel_pos_.x >= 0) out[5 * kCells + cell(intel_pos_.x, intel_pos_.y)] = 1.0f;
        // 平面 6/7:出生点常量
        out[6 * kCells + cell(0, 0)] = 1.0f;
        out[7 * kCells + cell(6, 6)] = 1.0f;
        // 标量
        float* s = out + kPlanes * kCells;
        switch (my_facing_) {
            case 'N': s[0] = 1.0f; break;
            case 'E': s[1] = 1.0f; break;
            case 'S': s[2] = 1.0f; break;
            case 'W': s[3] = 1.0f; break;
            default: break;
        }
        switch (intel_facing_) {
            case 'N': s[4] = 1.0f; break;
            case 'E': s[5] = 1.0f; break;
            case 'S': s[6] = 1.0f; break;
            case 'W': s[7] = 1.0f; break;
            default: s[8] = 1.0f; break;  // 未知
        }
        s[9] = fire_cd_ / 3.0f;
        s[10] = scan_cd_ / 3.0f;
        s[11] = my_score_ / 20.0f;
        s[12] = opp_score_ / 20.0f;
        s[13] = turn_ / 24.0f;
        s[14] = actions_used_ / 3.0f;
        s[15] = is_blue_ ? 1.0f : 0.0f;
        s[16] = opp_visible_ ? 1.0f : 0.0f;
        s[17] = opp_directly_visible_ ? 1.0f : 0.0f;
        s[18] = intel_pos_.x >= 0 ? (turn_ - intel_turn_) / 24.0f : 1.0f;
        s[19] = free_turn_ ? 1.0f : 0.0f;
    }

    // 公开掩码(只用公开信息;隐形敌人占据格不掩,交给引擎拒绝,失败不消耗)
    void action_mask(float* mask) const {
        for (int i = 0; i < 8; ++i) mask[i] = 0.0f;
        if (actions_used_ >= 3) {
            mask[7] = 1.0f;  // 只能 end
            return;
        }
        // move
        int dx = 0, dy = 0;
        delta(my_facing_, dx, dy);
        const int nx = my_pos_.x + dx, ny = my_pos_.y + dy;
        if (nx >= 0 && nx < kBoardSize && ny >= 0 && ny < kBoardSize &&
            !is_obstacle(nx, ny) && !(opp_directly_visible_ && nx == intel_pos_.x && ny == intel_pos_.y))
            mask[0] = 1.0f;
        // turn 1..4 = N/E/S/W
        static const char kDirs[4] = {'N', 'E', 'S', 'W'};
        for (int i = 0; i < 4; ++i)
            if (my_facing_ != kDirs[i]) mask[1 + i] = 1.0f;
        // fire / scan
        if (fire_cd_ == 0) mask[5] = 1.0f;
        if (scan_cd_ == 0) mask[6] = 1.0f;
        // end
        mask[7] = 1.0f;
    }

    // 供环境读取的内部状态
    Pos my_pos() const { return my_pos_; }
    char my_facing() const { return my_facing_; }
    int actions_used() const { return actions_used_; }
    int fire_cd() const { return fire_cd_; }
    int scan_cd() const { return scan_cd_; }
    bool free_turn() const { return free_turn_; }
    void clear_free_turn() { free_turn_ = false; }

private:
    static int cell(int x, int y) { return y * kBoardSize + x; }
    static bool same(Pos a, Pos b) { return a.x == b.x && a.y == b.y; }
    static void delta(char f, int& dx, int& dy) {
        switch (f) {
            case 'N': dx = 0; dy = -1; break;
            case 'E': dx = 1; dy = 0; break;
            case 'S': dx = 0; dy = 1; break;
            case 'W': dx = -1; dy = 0; break;
            default: dx = 0; dy = 0; break;
        }
    }
    bool is_obstacle(int x, int y) const {
        for (const Pos& o : obstacles_)
            if (o.x == x && o.y == y) return true;
        return false;
    }
    bool can_see_me(Pos target) const {
        Sentry me{};
        me.last_known_pos = my_pos_;
        me.last_known_facing = my_facing_;
        return can_see(me, target, obstacles_);
    }

    // 敌方一个行动阶段最多移动 3 格:信念按 BFS≤3(绕障碍、不占我方格)扩张
    void dilate_belief() {
        std::array<float, kCells> next = belief_;
        for (int step = 0; step < 3; ++step) {
            std::array<float, kCells> cur = next;
            for (int y = 0; y < kBoardSize; ++y) {
                for (int x = 0; x < kBoardSize; ++x) {
                    if (cur[cell(x, y)] <= 0.0f) continue;
                    static const int kDx[4] = {0, 1, 0, -1};
                    static const int kDy[4] = {-1, 0, 1, 0};
                    for (int d = 0; d < 4; ++d) {
                        const int nx = x + kDx[d], ny = y + kDy[d];
                        if (nx < 0 || nx >= kBoardSize || ny < 0 || ny >= kBoardSize) continue;
                        if (is_obstacle(nx, ny)) continue;
                        if (nx == my_pos_.x && ny == my_pos_.y) continue;
                        next[cell(nx, ny)] = 1.0f;
                    }
                }
            }
        }
        belief_ = next;
    }

    // 当前视野内确认无人的格子从信念剔除
    void subtract_visible_cells() {
        for (int y = 0; y < kBoardSize; ++y)
            for (int x = 0; x < kBoardSize; ++x)
                if (belief_[cell(x, y)] > 0.0f && can_see_me({x, y}))
                    belief_[cell(x, y)] = 0.0f;
    }

    std::array<float, kCells> belief_{};
    Pos intel_pos_{-1, -1};
    char intel_facing_ = '?';
    int intel_turn_ = -100;
    Pos my_pos_{0, 0};
    char my_facing_ = 'E';
    int my_score_ = 0, opp_score_ = 0;
    int fire_cd_ = 0, scan_cd_ = 0;
    int turn_ = 0;
    int actions_used_ = 0;
    bool is_blue_ = false;
    bool opp_visible_ = false;
    bool opp_directly_visible_ = false;
    bool free_turn_ = false;
    bool first_act_ = true;
    Pos prev_act_end_pos_{0, 0};
    Pos seen_now_{-1, -1};
    std::vector<Pos> obstacles_;
    std::vector<Pos> zones_;
};

}  // namespace rl
