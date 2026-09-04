// episode.h - 单局轨迹缓冲 + GAE 计算(与引擎/Match 完全解耦的纯组件)
//
// 一局对战中,learner 侧每个决策点 push 一条 (obs, mask, action, logprob, value);
// 奖励在下一个决策点(或终局)按 rl/SPEC.md §4 结算:
//   r_t = W_SCORE * Δ(my_score - opp_score) (+ 终局 ±1/0)
// 对局必定自然终止(20+5 回合上限),无需 bootstrap,GAE 末值取 0。

#pragma once

#include <array>
#include <vector>

#include "mlp.h"  // rl::OBS_DIM / rl::ACT_DIM

namespace rl {

struct Transition {
    std::array<float, OBS_DIM> obs{};
    std::array<float, ACT_DIM> mask{};
    int action = 0;
    float logprob = 0.0f;
    float value = 0.0f;
    float reward = 0.0f;  // 在下一个决策点或终局时回填
};

// 训练产出一个 rollout 批次(多局拼接,GAE 已算好)
struct RolloutBatch {
    std::vector<float> obs;      // N × OBS_DIM
    std::vector<float> mask;     // N × ACT_DIM
    std::vector<long> action;    // N
    std::vector<float> logprob;  // N
    std::vector<float> adv;      // N
    std::vector<float> ret;      // N
    // 统计
    int episodes = 0;
    int wins = 0, losses = 0, draws = 0;
    float score_diff_sum = 0.0f;
    float ep_len_sum = 0.0f;

    void clear() { *this = RolloutBatch(); }
    size_t size() const { return action.size(); }
};

class EpisodeBuffer {
public:
    EpisodeBuffer(float gamma, float lambda) : gamma_(gamma), lambda_(lambda) {}

    void begin() { traj_.clear(); }

    void push(const std::array<float, OBS_DIM>& obs,
              const std::array<float, ACT_DIM>& mask,
              int action, float logprob, float value) {
        Transition t;
        t.obs = obs;
        t.mask = mask;
        t.action = action;
        t.logprob = logprob;
        t.value = value;
        traj_.push_back(t);
    }

    // 给上一条 transition 回填奖励( score 差塑形在当前决策点可见 )
    void reward_last(float r) {
        if (!traj_.empty()) traj_.back().reward += r;
    }

    // 终局:terminal 为终局奖励(+1/0/-1,learner 视角);
    // my_diff 为终局 (my_score - opp_score)。outcome: 1=胜 0=平 -1=负。
    void finish(float terminal, float my_diff, int outcome, RolloutBatch& out) {
        reward_last(terminal);
        const size_t T = traj_.size();
        //  backward GAE,末值 0(对局必终止)
        float gae = 0.0f;
        std::vector<float> adv(T), ret(T);
        for (size_t i = T; i-- > 0;) {
            const float next_v = (i + 1 < T) ? traj_[i + 1].value : 0.0f;
            const float delta = traj_[i].reward + gamma_ * next_v - traj_[i].value;
            gae = delta + gamma_ * lambda_ * gae;
            adv[i] = gae;
            ret[i] = gae + traj_[i].value;
        }
        for (size_t i = 0; i < T; ++i) {
            const Transition& t = traj_[i];
            out.obs.insert(out.obs.end(), t.obs.begin(), t.obs.end());
            out.mask.insert(out.mask.end(), t.mask.begin(), t.mask.end());
            out.action.push_back(t.action);
            out.logprob.push_back(t.logprob);
            out.adv.push_back(adv[i]);
            out.ret.push_back(ret[i]);
        }
        out.episodes += 1;
        if (outcome > 0) ++out.wins;
        else if (outcome < 0) ++out.losses;
        else ++out.draws;
        out.score_diff_sum += my_diff;
        out.ep_len_sum += static_cast<float>(T);
        traj_.clear();
    }

private:
    float gamma_, lambda_;
    std::vector<Transition> traj_;
};

}  // namespace rl
