// test_obs.cpp - ObsBuilder / Mlp 的独立单元冒烟测试
// 用假 Board 驱动 ObsBuilder,校验观测维度、信念平面合法性、掩码基本性质。
// 构建: g++ -std=c++17 -O2 -I../engine/include -I. test_obs.cpp ../engine/src/utils.cpp -o test_obs

#include <cassert>
#include <cmath>
#include <cstdio>

#include "mlp.h"
#include "obs_builder.h"

using namespace rl;

static Board fake_board() {
    Board b{};
    b.size = 7;
    b.turn = 0;
    b.red.last_known_pos = {0, 0};
    b.red.last_known_facing = 'E';
    b.red.visible = true;
    b.red.fire_cd = 0; b.red.scan_cd = 0; b.red.score = 0;
    b.blue.last_known_pos = {-1, -1};   // 红方初始无情报
    b.blue.last_known_facing = '?';
    b.blue.visible = false;
    b.blue.fire_cd = -1; b.blue.scan_cd = -1; b.blue.score = 0;
    b.obstacles = {{1, 1}, {5, 5}};
    b.score_zones = {{3, 2}, {2, 3}, {3, 3}, {4, 3}, {3, 4}};
    return b;
}

static float belief_sum(const float* obs) {
    float s = 0;
    for (int i = 0; i < kCells; ++i) s += obs[3 * kCells + i];
    return s;
}

int main() {
    ObsBuilder ob;
    ob.reset();
    float obs[kObsDim], mask[8];

    // --- 回合 0 红方 act_start ---
    Board b = fake_board();
    ob.act_start(b, 'R');
    ob.encode(obs);
    ob.action_mask(mask);

    // 信念应只剩对方出生点 (6,6):第一回合不扩张,但视野证伪会剔除
    // (0,0) 朝 E 的 T 形视野不含 (6,6),所以 belief_sum == 1
    assert(std::fabs(belief_sum(obs) - 1.0f) < 1e-6);
    assert(obs[3 * kCells + 6 * 7 + 6] == 1.0f);
    // 我方位置平面
    assert(obs[2 * kCells + 0] == 1.0f);
    // 掩码:朝 E,(1,0) 可走;turn W 可选(当前 E);fire/scan 可用;end 可用
    assert(mask[0] == 1.0f);
    assert(mask[1] == 1.0f && mask[2] == 0.0f && mask[3] == 1.0f && mask[4] == 1.0f);
    assert(mask[5] == 1.0f && mask[6] == 1.0f && mask[7] == 1.0f);

    // --- 模拟 move() 成功到 (1,0) ---
    ActionObservation o{};
    o.my_pos = {1, 0}; o.my_facing = 'E';
    o.opp_last_known_pos = {-1, -1}; o.opp_last_known_facing = '?';
    o.opp_visible = false; o.opp_directly_visible = false;
    o.fire_cd = 0; o.scan_cd = 0;
    ob.on_observation(o, true);
    ob.encode(obs);
    assert(obs[2 * kCells + 0 * 7 + 1] == 1.0f);
    // 视野证伪后信念仍应只含 (6,6)
    assert(std::fabs(belief_sum(obs) - 1.0f) < 1e-6);

    // --- 下一回合:信念应扩张(曼哈顿球半径 3,不越界/不穿障碍)---
    b.turn = 1;
    b.red.last_known_pos = {1, 0};  // board 在 act 开始是权威,模拟我已走到 (1,0)
    ob.act_start(b, 'R');
    ob.encode(obs);
    const float s1 = belief_sum(obs);
    // 角隅 (6,6) 半径 3 的曼哈顿球共 10 格,扣掉障碍 (5,5) 剩 9;我方视野够不到
    assert(std::fabs(s1 - 9.0f) < 1e-6);
    printf("belief after 1 enemy phase: %.0f cells\n", s1);

    // --- 扫描命中:信念塌缩 ---
    o.opp_last_known_pos = {4, 4}; o.opp_last_known_facing = 'W';
    o.opp_visible = true; o.opp_directly_visible = false;
    ob.on_observation(o, true);
    ob.encode(obs);
    assert(std::fabs(belief_sum(obs) - 1.0f) < 1e-6);
    assert(obs[5 * kCells + 4 * 7 + 4] == 1.0f);  // last_known 平面
    assert(obs[4 * kCells + 4 * 7 + 4] == 0.0f);  // 非直接可见,seen_now 为空
    // 标量:opp_visible=1, directly=0
    const float* sc = obs + kPlanes * kCells;
    assert(sc[16] == 1.0f && sc[17] == 0.0f);

    // --- 观测维度自检:非零元素数量合理 ---
    int nnz = 0;
    for (int i = 0; i < kObsDim; ++i) if (obs[i] != 0.0f) ++nnz;
    printf("obs nnz=%d / %d\n", nnz, kObsDim);
    assert(nnz > 10 && nnz < kObsDim);

    // --- Mlp 加载(若存在测试权重)---
    FILE* f = std::fopen("/tmp/test.sdw", "rb");
    if (f) {
        std::fclose(f);
        Mlp m = Mlp::load("/tmp/test.sdw");
        std::array<float, ACT_DIM> logits{};
        float value = 0;
        m.forward(obs, logits, value);
        printf("mlp logits[0]=%.4f value=%.4f\n", logits[0], value);
        assert(std::isfinite(logits[0]) && std::isfinite(value));
    } else {
        printf("skip mlp test (no /tmp/test.sdw)\n");
    }

    printf("ALL OBS BUILDER TESTS PASSED\n");
    return 0;
}
