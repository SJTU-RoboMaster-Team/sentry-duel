// sentry_env.cpp - RL 训练环境(pybind11 模块)
//
// 基于 engine 的 sentry::Match 跑完整对局;learner 为 PPO 采样策略,
// 对手池:random / latest(最新权重 argmax)/ pool(历史检查点 argmax)/
//         baseline / hunter(脚本 .so,每局 dlopen 新副本保证状态全新)。
// 观测与奖励契约:rl/SPEC.md。GAE 在 C++ 侧按 episode 计算(episode.h)。
//
// Python 接口:
//   VecEnv(n_envs, opponent_spec, w_score, seed, baseline_so, hunter_so,
//          latest_weights, pool_dir, gamma, gae_lambda, n_threads)
//   collect(n_steps) -> dict(obs,mask,action,logprob,adv,ret,ep_len,win_rate,score_diff)
//   eval(weights_path, n_games) -> dict(vs_baseline, vs_hunter, ...)

#include <pybind11/pybind11.h>
#include <pybind11/numpy.h>
#include <pybind11/stl.h>

#include <algorithm>
#include <atomic>
#include <cmath>
#include <cstdio>
#include <cstring>
#include <dlfcn.h>
#include <link.h>
#include <filesystem>
#include <mutex>
#include <random>
#include <string>
#include <thread>
#include <vector>

#include "match.h"
#include "mlp.h"
#include "obs_builder.h"
#include "episode.h"

namespace py = pybind11;

namespace rl {

namespace {

// 把引擎 .so 提升到全局符号域,使 dlopen 的选手 .so 能解析 move/turn/fire/scan
// (pybind 模块默认 RTLD_LOCAL 加载,其依赖库不进全局域,需要显式提升)
void promote_engine_symbols() {
    Dl_info info{};
    if (dladdr(reinterpret_cast<void*>(&can_see), &info) && info.dli_fname) {
        if (!dlopen(info.dli_fname, RTLD_NOW | RTLD_GLOBAL))
            throw std::runtime_error(std::string("提升引擎符号失败: ") + dlerror());
    } else {
        throw std::runtime_error("dladdr(can_see) 失败");
    }
}

// ---------------- 对手规格 ----------------
enum class OppKind { RANDOM, LATEST, POOL, BASELINE, HUNTER };

struct OppEntry {
    OppKind kind;
    double weight;
};

std::vector<OppEntry> parse_spec(const std::string& spec) {
    std::vector<OppEntry> out;
    size_t i = 0;
    while (i < spec.size()) {
        size_t comma = spec.find(',', i);
        std::string tok = spec.substr(i, comma == std::string::npos ? comma : comma - i);
        i = (comma == std::string::npos) ? spec.size() : comma + 1;
        size_t colon = tok.find(':');
        if (colon == std::string::npos) continue;
        std::string name = tok.substr(0, colon);
        double w = std::stod(tok.substr(colon + 1));
        if (w <= 0) continue;
        if (name == "random") out.push_back({OppKind::RANDOM, w});
        else if (name == "latest") out.push_back({OppKind::LATEST, w});
        else if (name == "pool") out.push_back({OppKind::POOL, w});
        else if (name == "baseline") out.push_back({OppKind::BASELINE, w});
        else if (name == "hunter") out.push_back({OppKind::HUNTER, w});
        else if (name == "scripted") {  // baseline/hunter 各半
            out.push_back({OppKind::BASELINE, w / 2});
            out.push_back({OppKind::HUNTER, w / 2});
        } else {
            throw std::runtime_error("未知对手类型: " + name);
        }
    }
    if (out.empty()) throw std::runtime_error("opponent_spec 为空");
    return out;
}

// 按权重抽样
OppKind sample_opp(const std::vector<OppEntry>& spec, std::mt19937& rng) {
    double total = 0;
    for (const auto& e : spec) total += e.weight;
    double r = std::uniform_real_distribution<double>(0, total)(rng);
    for (const auto& e : spec) {
        r -= e.weight;
        if (r <= 0) return e.kind;
    }
    return spec.back().kind;
}

// ---------------- 脚本对手(每局 dlopen 独立副本,保证单例状态全新)----------------
class ScriptedBot {
public:
    // copy_path: 本线程专用的 .so 文件副本(同路径 dlopen 会共享单例)
    explicit ScriptedBot(std::string copy_path) : path_(std::move(copy_path)) {}
    ~ScriptedBot() { close(); }

    void load() {
        close();
        handle_ = dlopen(path_.c_str(), RTLD_NOW | RTLD_LOCAL);
        if (!handle_) throw std::runtime_error("dlopen 失败: " + path_ + " " + dlerror());
        act_fn_ = reinterpret_cast<ActFn>(dlsym(handle_, "act"));
        if (!act_fn_) throw std::runtime_error("dlsym(act) 失败: " + path_);
    }
    void close() {
        if (handle_) { dlclose(handle_); handle_ = nullptr; act_fn_ = nullptr; }
    }
    void act(const Board& b, char c) { act_fn_(b, c); }

private:
    using ActFn = void (*)(const Board&, char);
    std::string path_;
    void* handle_ = nullptr;
    ActFn act_fn_ = nullptr;
};

// ---------------- NN 决策 ----------------
// masked softmax 采样;返回 logprob
int sample_action(const float* logits, const float* mask, std::mt19937& rng,
                  float* logprob_out) {
    float mx = -1e30f;
    for (int a = 0; a < ACT_DIM; ++a)
        if (mask[a] > 0.0f) mx = std::max(mx, logits[a]);
    float sum = 0.0f;
    float prob[ACT_DIM];
    for (int a = 0; a < ACT_DIM; ++a) {
        prob[a] = (mask[a] > 0.0f) ? std::exp(logits[a] - mx) : 0.0f;
        sum += prob[a];
    }
    float r = std::uniform_real_distribution<float>(0, sum)(rng);
    int chosen = ACT_DIM - 1;
    for (int a = 0; a < ACT_DIM; ++a) {
        if (prob[a] <= 0.0f) continue;
        r -= prob[a];
        if (r <= 0) { chosen = a; break; }
        chosen = a;
    }
    if (logprob_out) *logprob_out = std::log(prob[chosen] / sum + 1e-12f);
    return chosen;
}

int argmax_action(const float* logits, const float* mask) {
    int best = -1;
    float bv = -1e30f;
    for (int a = 0; a < ACT_DIM; ++a)
        if (mask[a] > 0.0f && logits[a] > bv) { bv = logits[a]; best = a; }
    return best < 0 ? 7 : best;
}

// 执行动作 id(契约 §3):0=move 1..4=NESW 5=fire 6=scan 7=end
ActionResult exec_action(int a) {
    switch (a) {
        case 0: return ::move();
        case 1: return ::turn('N');
        case 2: return ::turn('E');
        case 3: return ::turn('S');
        case 4: return ::turn('W');
        case 5: return ::fire();
        case 6: { ScanResult r = ::scan();
                  return {r.success, r.consumed, r.observation}; }
        default: return {false, false, {}};
    }
}

// ---------------- 一局对局 ----------------
// learner 侧:nn 采样 + 记录;对手侧按 kind。
// 返回 outcome(learner 视角 1/0/-1)。episodes 数据写入 ep(调用方已 begin)。
struct GameContext {
    ObsBuilder learner_ob;
    ObsBuilder opp_ob;      // NN/random 对手用
    EpisodeBuffer* ep;      // learner 轨迹
    const Mlp* learner_net;
    const Mlp* opp_net;     // LATEST/POOL 时有效
    ScriptedBot* bot;       // BASELINE/HUNTER 时有效
    OppKind opp_kind;
    char learner_color;
    float w_score;
    std::mt19937* rng;
    float prev_diff = 0.0f;  // 上一 learner 决策点的 (my-opp) 分差
    bool has_pending = false;
};

int learner_policy_impl(GameContext& ctx, const Board& view, char color) {
    (void)color;
    const Sentry& me = (ctx.learner_color == 'R') ? view.red : view.blue;
    const Sentry& opp = (ctx.learner_color == 'R') ? view.blue : view.red;
    ctx.learner_ob.act_start(view, ctx.learner_color);
    // 奖励回填:自上一决策点的分差变化
    const float diff = static_cast<float>(me.score - opp.score);
    if (ctx.has_pending) ctx.ep->reward_last(ctx.w_score * (diff - ctx.prev_diff));
    ctx.prev_diff = diff;
    ctx.has_pending = true;

    float obs[OBS_DIM], mask[ACT_DIM], banned[ACT_DIM] = {0};
    std::array<float, OBS_DIM> obs_a;
    std::array<float, ACT_DIM> mask_a;
    while (ctx.learner_ob.actions_used() < 3) {
        ctx.learner_ob.encode(obs);
        ctx.learner_ob.action_mask(mask);
        std::array<float, ACT_DIM> logits{};
        float value = 0.0f;
        ctx.learner_net->forward(obs, logits, value);
        float logprob = 0.0f;
        int a = sample_action(logits.data(), mask, *ctx.rng, &logprob);
        if (banned[a] > 0.0f) {  // 采样到已失败动作:重采样(最多 8 次)
            bool ok = false;
            for (int t = 0; t < 8; ++t) {
                a = sample_action(logits.data(), mask, *ctx.rng, &logprob);
                if (banned[a] <= 0.0f) { ok = true; break; }
            }
            if (!ok) a = 7;
        }
        if (a == 7) {  // end:记录后结束本 act
            std::copy(obs, obs + OBS_DIM, obs_a.begin());
            std::copy(mask, mask + ACT_DIM, mask_a.begin());
            ctx.ep->push(obs_a, mask_a, a, logprob, value);
            break;
        }
        ActionResult r = exec_action(a);
        ctx.learner_ob.on_observation(r.observation, r.consumed);
        if (!r.success) {
            banned[a] = 1.0f;  // 引擎拒绝(不消耗):不记 transition,重选
            continue;
        }
        std::copy(obs, obs + OBS_DIM, obs_a.begin());
        std::copy(mask, mask + ACT_DIM, mask_a.begin());
        ctx.ep->push(obs_a, mask_a, a, logprob, value);
    }
    return 0;
}

int opp_policy_impl(GameContext& ctx, const Board& view, char color) {
    switch (ctx.opp_kind) {
        case OppKind::BASELINE:
        case OppKind::HUNTER:
            ctx.bot->act(view, color);
            return 0;
        case OppKind::RANDOM:
        case OppKind::LATEST:
        case OppKind::POOL: {
            ctx.opp_ob.act_start(view, color);
            float obs[OBS_DIM], mask[ACT_DIM], banned[ACT_DIM] = {0};
            while (ctx.opp_ob.actions_used() < 3) {
                ctx.opp_ob.action_mask(mask);
                int a = 7;
                if (ctx.opp_kind == OppKind::RANDOM) {
                    float logits[ACT_DIM] = {0};  // 均匀
                    a = sample_action(logits, mask, *ctx.rng, nullptr);
                } else {
                    ctx.opp_ob.encode(obs);
                    std::array<float, ACT_DIM> logits{};
                    float v = 0.0f;
                    ctx.opp_net->forward(obs, logits, v);
                    a = argmax_action(logits.data(), mask);
                }
                if (banned[a] > 0.0f) {
                    bool ok = false;
                    for (int t = 0; t < 8; ++t) {
                        ctx.opp_ob.action_mask(mask);
                        float lg[ACT_DIM] = {0};
                        a = (ctx.opp_kind == OppKind::RANDOM)
                            ? sample_action(lg, mask, *ctx.rng, nullptr) : 7;
                        if (ctx.opp_kind != OppKind::RANDOM) a = 7;
                        if (banned[a] <= 0.0f) { ok = true; break; }
                    }
                    if (!ok) return 0;
                }
                if (a == 7) return 0;
                ActionResult r = exec_action(a);
                ctx.opp_ob.on_observation(r.observation, r.consumed);
                if (!r.success) banned[a] = 1.0f;
            }
            return 0;
        }
    }
    return 0;
}

}  // namespace

// ---------------- VecEnv ----------------
class VecEnv {
public:
    VecEnv(int /*n_envs*/, std::string opponent_spec, double w_score, int seed,
           std::string baseline_so, std::string hunter_so,
           std::string latest_weights, std::string pool_dir,
           double gamma, double gae_lambda, int n_threads)
        : spec_(parse_spec(opponent_spec)), w_score_(static_cast<float>(w_score)),
          seed_(seed), baseline_so_(std::move(baseline_so)), hunter_so_(std::move(hunter_so)),
          latest_weights_(std::move(latest_weights)), pool_dir_(std::move(pool_dir)),
          gamma_(static_cast<float>(gamma)), lambda_(static_cast<float>(gae_lambda)),
          n_threads_(std::max(1, n_threads)) {
        promote_engine_symbols();  // 在复制/加载任何选手 .so 之前
        // 每线程一份脚本对手 .so 副本(同路径 dlopen 共享单例,必须按文件隔离)
        // copy_dir 由 pool_dir(绝对路径)推导,避免依赖运行时 CWD
        copy_dir_ = (std::filesystem::path(pool_dir_).parent_path() / "runs" / "so_copies")
                        .lexically_normal().string();
        std::filesystem::create_directories(copy_dir_);
        for (int t = 0; t < n_threads_; ++t) {
            for (int bi = 0; bi < 2; ++bi) {
                const std::string& so = bi == 0 ? baseline_so_ : hunter_so_;
                std::filesystem::copy_file(
                    so, copy_path(bi, t),
                    std::filesystem::copy_options::overwrite_existing);
            }
        }
    }

    py::dict collect(int n_steps) {
        // 每次 collect:各线程加载一次最新权重(行为策略一致性)
        std::vector<std::string> pool = list_pool();
        std::vector<RolloutBatch> parts(n_threads_);
        std::exception_ptr worker_err;
        std::mutex err_mu;
        {
            py::gil_scoped_release release;
            std::vector<std::thread> threads;
            std::atomic<int> game_counter{0};
            const int quota = n_steps / n_threads_;
            for (int t = 0; t < n_threads_; ++t) {
                const int my_quota = (t == n_threads_ - 1)
                    ? n_steps - quota * (n_threads_ - 1) : quota;
                threads.emplace_back([&, t, my_quota]() {
                    try {
                        worker_collect(t, my_quota, pool, game_counter, parts[t]);
                    } catch (...) {
                        std::lock_guard<std::mutex> lk(err_mu);
                        if (!worker_err) worker_err = std::current_exception();
                    }
                });
            }
            for (auto& th : threads) th.join();
        }
        if (worker_err) std::rethrow_exception(worker_err);
        RolloutBatch all;
        for (auto& p : parts) merge_into(std::move(p), all);
        return to_py(all);
    }

    py::dict eval(std::string weights_path, int n_games) {
        Mlp net = Mlp::load(weights_path);
        // 对 baseline / hunter 各 n_games 局,红蓝各半,learner argmax
        std::vector<std::thread> threads;
        std::vector<std::array<double, 4>> results(2);  // [win, draw, loss, diff_sum]
        std::exception_ptr eval_err;
        std::mutex err_mu;
        {
            py::gil_scoped_release release;
            for (int oi = 0; oi < 2; ++oi) {
                threads.emplace_back([&, oi]() {
                    try {
                    std::mt19937 rng(seed_ * 7919 + oi);
                    Mlp local = net;  // 线程本地副本
                    for (int g = 0; g < n_games; ++g) {
                        const char learner_color = (g % 2 == 0) ? 'R' : 'B';
                        ScriptedBot bot(copy_path(oi == 0 ? 0 : 1, 0));
                        bot.load();
                        float diff = 0.0f;
                        int outcome = run_eval_game(local, bot, learner_color, rng, diff);
                        bot.close();
                        auto& r = results[oi];
                        if (outcome > 0) r[0] += 1;
                        else if (outcome == 0) r[1] += 1;
                        else r[2] += 1;
                        r[3] += diff;
                    }
                    } catch (...) {
                        std::lock_guard<std::mutex> lk(err_mu);
                        if (!eval_err) eval_err = std::current_exception();
                    }
                });
            }
            for (auto& th : threads) th.join();
        }
        if (eval_err) std::rethrow_exception(eval_err);
        py::dict d;
        for (int oi = 0; oi < 2; ++oi) {
            const char* name = oi == 0 ? "baseline" : "hunter";
            const auto& r = results[oi];
            d[("vs_" + std::string(name)).c_str()] = (r[0] + 0.5 * r[1]) / n_games;
            d[("vs_" + std::string(name) + "_diff").c_str()] = r[3] / n_games;
        }
        return d;
    }

    // 自对弈:双方同一权重、温度采样,统计红/蓝胜场(测颜色平衡)
    py::dict eval_self(std::string weights_path, int n_games, double temperature) {
        Mlp net = Mlp::load(weights_path);
        std::atomic<int> red_win{0}, blue_win{0}, draw{0};
        {
            py::gil_scoped_release release;
            std::vector<std::thread> threads;
            const int per = n_games / n_threads_;
            for (int t = 0; t < n_threads_; ++t) {
                const int my = (t == n_threads_ - 1) ? n_games - per * (n_threads_ - 1) : per;
                threads.emplace_back([&, t, my]() {
                    std::mt19937 rng(seed_ * 31337u + t * 733u);
                    Mlp local = net;  // 线程本地副本
                    for (int g = 0; g < my; ++g) {
                        ObsBuilder obr, obb;
                        obr.reset();
                        obb.reset();
                        auto mk = [&](char side) {
                            return [&, side](const Board& view, char /*c*/) -> int {
                                ObsBuilder& ob = side == 'R' ? obr : obb;
                                ob.act_start(view, side);
                                float obs[OBS_DIM], mask[ACT_DIM], banned[ACT_DIM] = {0};
                                while (ob.actions_used() < 3) {
                                    ob.encode(obs);
                                    ob.action_mask(mask);
                                    std::array<float, ACT_DIM> logits{};
                                    float v = 0.0f;
                                    local.forward(obs, logits, v);
                                    for (int a = 0; a < ACT_DIM; ++a)
                                        logits[a] /= static_cast<float>(temperature);
                                    int a = sample_action(logits.data(), mask, rng, nullptr);
                                    int tries = 0;
                                    while (banned[a] > 0.0f && tries++ < 8)
                                        a = sample_action(logits.data(), mask, rng, nullptr);
                                    if (banned[a] > 0.0f || a == 7) return 0;
                                    ActionResult r = exec_action(a);
                                    ob.on_observation(r.observation, r.consumed);
                                    if (!r.success) banned[a] = 1.0f;
                                }
                                return 0;
                            };
                        };
                        sentry::Match match(mk('R'), mk('B'), "r", "b", 20, 0,
                                            [](const std::string&) {});
                        const int w = match.run();
                        if (w == 1) ++red_win;
                        else if (w == 2) ++blue_win;
                        else ++draw;
                    }
                });
            }
            for (auto& th : threads) th.join();
        }
        py::dict d;
        d["red_win"] = red_win.load();
        d["blue_win"] = blue_win.load();
        d["draw"] = draw.load();
        return d;
    }

    // 交叉对局:红蓝双方可用不同权重,温度采样,统计红/蓝胜场
    py::dict eval_cross(std::string red_weights, std::string blue_weights,
                        int n_games, double temperature) {
        Mlp net_r = Mlp::load(red_weights);
        Mlp net_b = Mlp::load(blue_weights);
        std::atomic<int> red_win{0}, blue_win{0}, draw{0};
        {
            py::gil_scoped_release release;
            std::vector<std::thread> threads;
            const int per = n_games / n_threads_;
            for (int t = 0; t < n_threads_; ++t) {
                const int my = (t == n_threads_ - 1) ? n_games - per * (n_threads_ - 1) : per;
                threads.emplace_back([&, t, my]() {
                    std::mt19937 rng(seed_ * 31337u + t * 733u);
                    Mlp local_r = net_r, local_b = net_b;  // 线程本地副本
                    for (int g = 0; g < my; ++g) {
                        ObsBuilder obr, obb;
                        obr.reset();
                        obb.reset();
                        auto mk = [&](char side) {
                            return [&, side](const Board& view, char /*c*/) -> int {
                                ObsBuilder& ob = side == 'R' ? obr : obb;
                                const Mlp& net = side == 'R' ? local_r : local_b;
                                ob.act_start(view, side);
                                float obs[OBS_DIM], mask[ACT_DIM], banned[ACT_DIM] = {0};
                                while (ob.actions_used() < 3) {
                                    ob.encode(obs);
                                    ob.action_mask(mask);
                                    std::array<float, ACT_DIM> logits{};
                                    float v = 0.0f;
                                    net.forward(obs, logits, v);
                                    for (int a = 0; a < ACT_DIM; ++a)
                                        logits[a] /= static_cast<float>(temperature);
                                    int a = sample_action(logits.data(), mask, rng, nullptr);
                                    int tries = 0;
                                    while (banned[a] > 0.0f && tries++ < 8)
                                        a = sample_action(logits.data(), mask, rng, nullptr);
                                    if (banned[a] > 0.0f || a == 7) return 0;
                                    ActionResult r = exec_action(a);
                                    ob.on_observation(r.observation, r.consumed);
                                    if (!r.success) banned[a] = 1.0f;
                                }
                                return 0;
                            };
                        };
                        sentry::Match match(mk('R'), mk('B'), "r", "b", 20, 0,
                                            [](const std::string&) {});
                        const int w = match.run();
                        if (w == 1) ++red_win;
                        else if (w == 2) ++blue_win;
                        else ++draw;
                    }
                });
            }
            for (auto& th : threads) th.join();
        }
        py::dict d;
        d["red_win"] = red_win.load();
        d["blue_win"] = blue_win.load();
        d["draw"] = draw.load();
        return d;
    }

private:
    std::string copy_path(int bot_idx, int thread) const {
        return copy_dir_ + "/t" + std::to_string(thread) +
               (bot_idx == 0 ? "_baseline.so" : "_hunter.so");
    }

    std::vector<std::string> list_pool() const {
        std::vector<std::string> out;
        if (!std::filesystem::exists(pool_dir_)) return out;
        for (const auto& e : std::filesystem::directory_iterator(pool_dir_)) {
            const std::string name = e.path().filename().string();
            if (name.rfind("ckpt_", 0) == 0 && e.path().extension() == ".sdw")
                out.push_back(e.path().string());
        }
        std::sort(out.begin(), out.end());
        return out;
    }

    void worker_collect(int tid, int quota, const std::vector<std::string>& pool,
                        std::atomic<int>& game_counter, RolloutBatch& out) {
        Mlp learner = Mlp::load(latest_weights_);
        std::mt19937 rng(seed_ * 131u + tid * 1009u);
        ScriptedBot bot_base(copy_path(0, tid)), bot_hunt(copy_path(1, tid));
        while (static_cast<int>(out.size()) < quota) {
            const OppKind kind = sample_opp(spec_, rng);
            const int gi = game_counter.fetch_add(1);
            const char learner_color = (gi % 2 == 0) ? 'R' : 'B';
            run_train_game(learner, kind, pool, bot_base, bot_hunt,
                           learner_color, rng, out);
        }
    }

    void run_train_game(const Mlp& learner, OppKind kind,
                        const std::vector<std::string>& pool,
                        ScriptedBot& bot_base, ScriptedBot& bot_hunt,
                        char learner_color, std::mt19937& rng, RolloutBatch& out) {
        GameContext ctx;
        ctx.ep = nullptr;  // 下面设置
        ctx.learner_net = &learner;
        ctx.opp_kind = kind;
        ctx.learner_color = learner_color;
        ctx.w_score = w_score_;
        ctx.rng = &rng;

        Mlp opp_hold;  // 仅 POOL 对手用(值语义持有)
        if (kind == OppKind::LATEST) {
            ctx.opp_net = &learner;  // 同一份最新权重(argmax)
        } else if (kind == OppKind::POOL) {
            if (pool.empty()) {
                ctx.opp_net = &learner;
            } else {
                std::uniform_int_distribution<size_t> pick(0, pool.size() - 1);
                opp_hold = Mlp::load(pool[pick(rng)]);
                ctx.opp_net = &opp_hold;
            }
        }
        if (kind == OppKind::BASELINE) { bot_base.load(); ctx.bot = &bot_base; }
        if (kind == OppKind::HUNTER) { bot_hunt.load(); ctx.bot = &bot_hunt; }

        EpisodeBuffer ep(gamma_, lambda_);
        ep.begin();
        ctx.ep = &ep;
        ctx.learner_ob.reset();
        ctx.opp_ob.reset();

        sentry::Match::Policy red = [&](const Board& b, char c) -> int {
            return (learner_color == 'R') ? learner_policy_impl(ctx, b, c)
                                          : opp_policy_impl(ctx, b, c);
        };
        sentry::Match::Policy blue = [&](const Board& b, char c) -> int {
            return (learner_color == 'B') ? learner_policy_impl(ctx, b, c)
                                          : opp_policy_impl(ctx, b, c);
        };
        sentry::Match match(red, blue, "rl", "opp", 20, 0,
                            [](const std::string&) {});  // 丢弃事件
        const int winner = match.run();

        // 终局结算
        const Board& fb = match.board();
        const float my = static_cast<float>(
            learner_color == 'R' ? fb.red.score : fb.blue.score);
        const float op = static_cast<float>(
            learner_color == 'R' ? fb.blue.score : fb.red.score);
        const float final_diff = my - op;
        if (ctx.has_pending) ep.reward_last(w_score_ * (final_diff - ctx.prev_diff));
        int outcome = 0;
        if ((winner == 1 && learner_color == 'R') || (winner == 2 && learner_color == 'B'))
            outcome = 1;
        else if (winner != 3)
            outcome = -1;
        ep.finish(static_cast<float>(outcome), final_diff, outcome, out);

        if (kind == OppKind::BASELINE) bot_base.close();
        if (kind == OppKind::HUNTER) bot_hunt.close();
    }

    int run_eval_game(const Mlp& net, ScriptedBot& bot, char learner_color,
                      std::mt19937& rng, float& diff_out) {
        (void)rng;
        GameContext ctx;
        ctx.learner_net = &net;
        ctx.opp_kind = OppKind::BASELINE;  // 占位
        ctx.learner_color = learner_color;
        ctx.w_score = 0.0f;
        ctx.rng = &rng;
        ctx.bot = &bot;
        ctx.learner_ob.reset();
        ctx.opp_ob.reset();
        EpisodeBuffer ep(gamma_, lambda_);  // 只挂空轨迹,不记录
        ep.begin();
        ctx.ep = &ep;
        // eval:learner argmax(复用 learner_policy_impl 但改 argmax 不采样)
        // 简单起见:这里直接内联 argmax 版 learner 策略
        auto learner = [&](const Board& view, char /*c*/) -> int {
            const Sentry& me = (learner_color == 'R') ? view.red : view.blue;
            const Sentry& opp = (learner_color == 'R') ? view.blue : view.red;
            ctx.learner_ob.act_start(view, learner_color);
            (void)me; (void)opp;
            float obs[OBS_DIM], mask[ACT_DIM], banned[ACT_DIM] = {0};
            while (ctx.learner_ob.actions_used() < 3) {
                ctx.learner_ob.encode(obs);
                ctx.learner_ob.action_mask(mask);
                std::array<float, ACT_DIM> logits{};
                float v = 0.0f;
                net.forward(obs, logits, v);
                int a = argmax_action(logits.data(), mask);
                while (banned[a] > 0.0f) {
                    logits[a] = -1e30f;
                    a = argmax_action(logits.data(), mask);
                    if (banned[a] > 0.0f && a == 7) break;
                }
                if (a == 7) return 0;
                ActionResult r = exec_action(a);
                ctx.learner_ob.on_observation(r.observation, r.consumed);
                if (!r.success) banned[a] = 1.0f;
            }
            return 0;
        };
        auto opponent = [&](const Board& view, char c) -> int {
            bot.act(view, c);
            return 0;
        };
        sentry::Match::Policy red = learner_color == 'R' ?
            sentry::Match::Policy(learner) : sentry::Match::Policy(opponent);
        sentry::Match::Policy blue = learner_color == 'B' ?
            sentry::Match::Policy(learner) : sentry::Match::Policy(opponent);
        sentry::Match match(red, blue, "rl", "opp", 20, 0, [](const std::string&) {});
        const int winner = match.run();
        const Board& fb = match.board();
        diff_out = static_cast<float>(
            (learner_color == 'R' ? fb.red.score - fb.blue.score
                                  : fb.blue.score - fb.red.score));
        if ((winner == 1 && learner_color == 'R') || (winner == 2 && learner_color == 'B'))
            return 1;
        return winner == 3 ? 0 : -1;
    }

    static void merge_into(RolloutBatch&& src, RolloutBatch& dst) {
        dst.obs.insert(dst.obs.end(), src.obs.begin(), src.obs.end());
        dst.mask.insert(dst.mask.end(), src.mask.begin(), src.mask.end());
        dst.action.insert(dst.action.end(), src.action.begin(), src.action.end());
        dst.logprob.insert(dst.logprob.end(), src.logprob.begin(), src.logprob.end());
        dst.adv.insert(dst.adv.end(), src.adv.begin(), src.adv.end());
        dst.ret.insert(dst.ret.end(), src.ret.begin(), src.ret.end());
        dst.episodes += src.episodes;
        dst.wins += src.wins; dst.losses += src.losses; dst.draws += src.draws;
        dst.score_diff_sum += src.score_diff_sum;
        dst.ep_len_sum += src.ep_len_sum;
    }

    static py::dict to_py(const RolloutBatch& b) {
        py::dict d;
        const size_t n = b.size();
        // py::array_t(shape, ptr) 不拷贝,会悬垂;必须先分配再 memcpy
        auto obs = py::array_t<float>({n, static_cast<size_t>(OBS_DIM)});
        auto mask = py::array_t<float>({n, static_cast<size_t>(ACT_DIM)});
        auto action = py::array_t<long>(n);
        auto logprob = py::array_t<float>(n);
        auto adv = py::array_t<float>(n);
        auto ret = py::array_t<float>(n);
        auto ep_len = py::array_t<float>(b.episodes);
        if (n) {
            std::memcpy(obs.mutable_data(), b.obs.data(), n * OBS_DIM * sizeof(float));
            std::memcpy(mask.mutable_data(), b.mask.data(), n * ACT_DIM * sizeof(float));
            std::memcpy(action.mutable_data(), b.action.data(), n * sizeof(long));
            std::memcpy(logprob.mutable_data(), b.logprob.data(), n * sizeof(float));
            std::memcpy(adv.mutable_data(), b.adv.data(), n * sizeof(float));
            std::memcpy(ret.mutable_data(), b.ret.data(), n * sizeof(float));
        }
        if (b.episodes) {
            float* p = ep_len.mutable_data();
            const float mean_len = b.ep_len_sum / b.episodes;
            for (int i = 0; i < b.episodes; ++i) p[i] = mean_len;  // 只需均值
        }
        d["obs"] = obs; d["mask"] = mask; d["action"] = action;
        d["logprob"] = logprob; d["adv"] = adv; d["ret"] = ret;
        d["ep_len"] = ep_len;
        d["win_rate"] = b.episodes
            ? (b.wins + 0.5 * b.draws) / static_cast<double>(b.episodes) : 0.0;
        d["score_diff"] = b.episodes ? b.score_diff_sum / b.episodes : 0.0f;
        return d;
    }

    std::vector<OppEntry> spec_;
    float w_score_;
    int seed_;
    std::string baseline_so_, hunter_so_, latest_weights_, pool_dir_, copy_dir_;
    float gamma_, lambda_;
    int n_threads_;
};

}  // namespace rl

PYBIND11_MODULE(sentry_env, m) {
    py::class_<rl::VecEnv>(m, "VecEnv")
        .def(py::init<int, std::string, double, int, std::string, std::string,
                      std::string, std::string, double, double, int>(),
             py::arg("n_envs"), py::arg("opponent_spec"), py::arg("w_score"),
             py::arg("seed"), py::arg("baseline_so"), py::arg("hunter_so"),
             py::arg("latest_weights"), py::arg("pool_dir"),
             py::arg("gamma"), py::arg("gae_lambda"), py::arg("n_threads"))
        .def("collect", &rl::VecEnv::collect, py::arg("n_steps"))
        .def("eval", &rl::VecEnv::eval, py::arg("weights_path"), py::arg("n_games"))
        .def("eval_self", &rl::VecEnv::eval_self, py::arg("weights_path"),
             py::arg("n_games"), py::arg("temperature"))
        .def("eval_cross", &rl::VecEnv::eval_cross, py::arg("red_weights"),
             py::arg("blue_weights"), py::arg("n_games"), py::arg("temperature"));
}
