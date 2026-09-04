// rl_ai.cpp - RL 策略部署 AI(哨兵大战参赛 .so)
//
// 结构:
//   - 观测:复用 rl/env/obs_builder.h(与训练环境同一份代码,保证一致)
//   - 推理:手写 MLP 前向(412→256→256→8),权重由 rl/training/export_cpp.py
//     生成为 ai/rl_weights.h,无外部依赖
//   - 决策:masked argmax(确定性),行动被引擎拒绝时屏蔽该动作重试
//   - 安全壳:任何异常/异常状态回退到简单启发式,保证不崩溃、不超时
//
// 构建(见 ai/build_rl_ai.sh):
//   g++ -std=c++17 -O2 -fPIC -shared -Wl,-z,lazy -Wl,--allow-shlib-undefined \
//       -I../engine/include -I.. rl_ai.cpp -o rl_ai.so

#include <algorithm>
#include <cstdio>

#include "sentry_duel.h"
#include "utils.h"
#include "../rl/env/obs_builder.h"
#include "rl_weights.h"  // 生成文件:kLayerIn/kLayerOut/kW0..3/kB0..3

namespace {

rl::ObsBuilder g_ob;
bool g_game_started = false;

// ---- 手写 MLP 前向(412→256→256→8,隐藏层 ReLU)----
void matmul(const float* w, const float* b, const float* x, float* y,
            int nin, int nout, bool relu) {
    for (int o = 0; o < nout; ++o) {
        const float* wr = w + static_cast<size_t>(o) * nin;
        float s = b[o];
        for (int i = 0; i < nin; ++i) s += wr[i] * x[i];
        y[o] = relu ? (s > 0.0f ? s : 0.0f) : s;
    }
}

void forward(const float* x, float* logits /*8*/) {
    static thread_local float h1[256], h2[256];
    matmul(kW0, kB0, x, h1, kLayerIn[0], kLayerOut[0], true);
    matmul(kW1, kB1, h1, h2, kLayerIn[1], kLayerOut[1], true);
    matmul(kW2, kB2, h2, logits, kLayerIn[2], kLayerOut[2], false);
}

// ---- 执行一个动作,返回引擎结果 ----
bool do_action(int a, ActionObservation& ob, bool& consumed) {
    switch (a) {
        case 0: { ActionResult r = move();     ob = r.observation; consumed = r.consumed; return r.success; }
        case 1: { ActionResult r = turn('N');  ob = r.observation; consumed = r.consumed; return r.success; }
        case 2: { ActionResult r = turn('E');  ob = r.observation; consumed = r.consumed; return r.success; }
        case 3: { ActionResult r = turn('S');  ob = r.observation; consumed = r.consumed; return r.success; }
        case 4: { ActionResult r = turn('W');  ob = r.observation; consumed = r.consumed; return r.success; }
        case 5: { ActionResult r = fire();     ob = r.observation; consumed = r.consumed; return r.success; }
        case 6: { ScanResult  r = scan();      ob = r.observation; consumed = r.consumed; return r.success; }
        default: return false;  // 7 = end,由调用方处理
    }
}

// ---- 启发式回退(任何异常时使用):看得见就打,否则雷达,再朝中心走 ----
void fallback_act(const Board& board, char my_color) {
    const Sentry& me = (my_color == 'R') ? board.red : board.blue;
    const Sentry& opp = (my_color == 'R') ? board.blue : board.red;
    if (opp.visible && me.fire_cd == 0) { fire(); return; }
    if (me.scan_cd == 0) { scan(); return; }
    if (can_move_forward(me, opp.last_known_pos, board.obstacles, board.size)) move();
}

void rl_act(const Board& board, char my_color) {
    if (board.turn == 0 || !g_game_started) {
        g_ob.reset();
        g_game_started = true;
    }
    g_ob.act_start(board, my_color);

    float obs[rl::kObsDim];
    float mask[8];
    float banned[8] = {0};
    int used = 0;
    while (used < 3) {
        g_ob.encode(obs);
        g_ob.action_mask(mask);
        float logits[8];
        forward(obs, logits);
        // masked argmax(跳过已失败的动作)
        int best = -1;
        float best_v = -1e30f;
        for (int a = 0; a < 8; ++a) {
            if (mask[a] <= 0.0f || banned[a] > 0.0f) continue;
            if (logits[a] > best_v) { best_v = logits[a]; best = a; }
        }
        if (best < 0 || best == 7) break;  // end
        ActionObservation ob{};
        bool consumed = false;
        const bool ok = do_action(best, ob, consumed);
        g_ob.on_observation(ob, consumed);
        if (!ok) {
            banned[best] = 1.0f;  // 该动作被引擎拒绝(不消耗额度),换次优
            continue;
        }
        if (consumed) ++used;
    }
}

}  // namespace

extern "C" void act(const Board& board, char my_color) {
    try {
        rl_act(board, my_color);
    } catch (...) {
        // 安全壳:任何异常都不得让 act 崩溃(崩溃整局判负)
        try { fallback_act(board, my_color); } catch (...) {}
    }
}
