// test_episode.cpp - EpisodeBuffer GAE 手算对照测试
// 构建: g++ -std=c++17 -O2 -I. test_episode.cpp -o test_episode

#include <cassert>
#include <cmath>
#include <cstdio>

#include "episode.h"

using namespace rl;

static bool close(float a, float b) { return std::fabs(a - b) < 1e-5f; }

int main() {
    // γ=1, λ=1 时 GAE 退化为"奖励到 go":adv_t = Σ_{i>=t} r_i - 与 value 的递推
    // 手算:T=3, r = [0, 0, 3](终局 +3),v = [0.5, 0.5, 0.5]
    // delta_2 = 3 + 0 - 0.5 = 2.5, adv_2 = 2.5
    // delta_1 = 0 + 0.5 - 0.5 = 0, adv_1 = 2.5
    // delta_0 = 0 + 0.5 - 0.5 = 0, adv_0 = 2.5
    EpisodeBuffer eb(1.0f, 1.0f);
    RolloutBatch batch;
    eb.begin();
    for (int i = 0; i < 3; ++i) {
        std::array<float, OBS_DIM> obs{};
        obs[0] = static_cast<float>(i);  // 标记
        std::array<float, ACT_DIM> mask{};
        mask[7] = 1.0f;
        eb.push(obs, mask, 7, -1.5f, 0.5f);
    }
    eb.reward_last(0.0f);
    eb.reward_last(0.0f);
    eb.finish(3.0f, 3.0f, 1, batch);

    assert(batch.size() == 3);
    assert(batch.episodes == 1 && batch.wins == 1 && batch.losses == 0);
    assert(close(batch.score_diff_sum, 3.0f));
    for (int i = 0; i < 3; ++i) {
        assert(close(batch.adv[i], 2.5f));
        assert(close(batch.ret[i], 3.0f));
        assert(batch.action[i] == 7);
        assert(close(batch.obs[i * OBS_DIM], static_cast<float>(i)));
    }

    // γ=0.5, λ=0(单步 TD):adv_t = r_t + γ v_{t+1} - v_t
    // r=[1,0], v=[10,20]: adv_0 = 1 + 0.5*20 - 10 = 1; adv_1 = 0 - 20 = -20
    EpisodeBuffer eb2(0.5f, 0.0f);
    RolloutBatch b2;
    eb2.begin();
    std::array<float, OBS_DIM> obs{};
    std::array<float, ACT_DIM> mask{};
    mask[0] = 1.0f;
    eb2.push(obs, mask, 0, 0.0f, 10.0f);
    eb2.reward_last(1.0f);
    eb2.push(obs, mask, 0, 0.0f, 20.0f);
    eb2.finish(0.0f, -1.0f, -1, b2);
    eb2.reward_last(0.0f);            // finish 后已清空,空操作不应崩溃
    assert(b2.size() == 2);
    assert(close(b2.adv[0], 1.0f));
    assert(close(b2.adv[1], -20.0f));

    printf("ALL EPISODE TESTS PASSED\n");
    return 0;
}
