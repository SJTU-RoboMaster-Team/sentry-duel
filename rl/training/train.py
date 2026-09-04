# train.py - PPO + league 主训练循环
# 依赖 rl/env 构建出的 pybind 模块 sentry_env(C++ 侧收集 rollout 与 GAE)。
#
# 用法:
#   python3 train.py --phase smoke    # 只对脚本池,验证学习曲线
#   python3 train.py --phase league   # 正式自我对弈训练

import argparse
import json
import os
import shutil
import signal
import sys
import time

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # rl/
sys.path.insert(0, os.path.join(ROOT, "env"))  # sentry_env 模块所在

import sentry_env
from model import ActorCritic
from ppo import PPO

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # rl/
REPO = os.path.dirname(ROOT)
WEIGHTS_DIR = os.path.join(ROOT, "weights")
CKPT_DIR = os.path.join(ROOT, "checkpoints")
LOG_DIR = os.path.join(ROOT, "logs")
LATEST = os.path.join(WEIGHTS_DIR, "latest.sdw")

BASELINE_SO = os.path.join(REPO, "ai", "baseline_ai.so")
HUNTER_SO = os.path.join(REPO, "ai", "hunter_ai.so")

STOP = False


def _sigterm(_sig, _frm):
    global STOP
    STOP = True


def log_json(rec):
    os.makedirs(LOG_DIR, exist_ok=True)
    with open(os.path.join(LOG_DIR, "train_log.jsonl"), "a") as f:
        f.write(json.dumps(rec) + "\n")
    print(json.dumps(rec, ensure_ascii=False), flush=True)


def make_env(args, opponent_spec):
    return sentry_env.VecEnv(
        n_envs=args.n_envs,
        opponent_spec=opponent_spec,
        w_score=args.w_score,
        seed=args.seed,
        baseline_so=BASELINE_SO,
        hunter_so=HUNTER_SO,
        latest_weights=LATEST,
        pool_dir=CKPT_DIR,
        gamma=args.gamma,
        gae_lambda=args.gae_lambda,
        n_threads=args.n_threads,
    )


def snapshot_pool(iteration, cap=20):
    """把当前 latest.sdw 存进对手池,超出容量则淘汰最旧。"""
    os.makedirs(CKPT_DIR, exist_ok=True)
    dst = os.path.join(CKPT_DIR, f"ckpt_{iteration:06d}.sdw")
    shutil.copyfile(LATEST, dst)
    ckpts = sorted(f for f in os.listdir(CKPT_DIR) if f.startswith("ckpt_"))
    while len(ckpts) > cap:
        os.remove(os.path.join(CKPT_DIR, ckpts.pop(0)))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--phase", choices=["smoke", "league"], default="league")
    ap.add_argument("--n-envs", type=int, default=64)
    ap.add_argument("--n-threads", type=int, default=16)
    ap.add_argument("--steps-per-iter", type=int, default=65536)
    ap.add_argument("--max-iters", type=int, default=100000)
    ap.add_argument("--w-score", type=float, default=0.1)
    ap.add_argument("--gamma", type=float, default=0.99)
    ap.add_argument("--gae-lambda", type=float, default=0.95)
    ap.add_argument("--lr", type=float, default=3e-4)
    ap.add_argument("--seed", type=int, default=1234)
    ap.add_argument("--eval-every", type=int, default=10, help="每 N iter 快评一次")
    ap.add_argument("--eval-games", type=int, default=400)
    ap.add_argument("--ckpt-every", type=int, default=10, help="每 N iter 把 latest 存进对手池")
    ap.add_argument("--init-from", default="", help="从已有 sdw 权重继续训练")
    args = ap.parse_args()

    signal.signal(signal.SIGTERM, _sigterm)
    signal.signal(signal.SIGINT, _sigterm)
    os.makedirs(WEIGHTS_DIR, exist_ok=True)

    ppo = PPO(lr=args.lr, gamma=args.gamma, gae_lambda=args.gae_lambda)
    if args.init_from:
        ppo.net.load_weights_bin(args.init_from)
    ppo.net.save_weights_bin(LATEST)

    if args.phase == "smoke":
        opponent_spec = "random:0.2,baseline:0.4,hunter:0.4"
    else:
        opponent_spec = "latest:0.5,pool:0.3,scripted:0.2"

    env = make_env(args, opponent_spec)

    total_steps = 0
    t0 = time.time()
    for it in range(1, args.max_iters + 1):
        if STOP:
            log_json({"type": "stop", "iter": it})
            break
        batch = env.collect(args.steps_per_iter)
        total_steps += int(batch["obs"].shape[0])
        stats = ppo.update(batch)
        ppo.net.save_weights_bin(LATEST)  # 下一次 collect 时环境重载

        rec = {
            "type": "iter", "iter": it, "total_steps": total_steps,
            "sps": int(total_steps / (time.time() - t0)),
            "ep_len": float(np.mean(batch["ep_len"])) if len(batch["ep_len"]) else 0.0,
            "win": float(batch["win_rate"]), "score_diff": float(batch["score_diff"]),
            **{k: float(v) for k, v in stats.items() if k != "n"},
        }
        log_json(rec)

        if args.phase == "league" and it % args.ckpt_every == 0:
            snapshot_pool(it)

        if it % args.eval_every == 0:
            ev = env.eval(LATEST, args.eval_games)  # 对 baseline/hunter 各 eval_games 局(双色各半)
            log_json({"type": "eval", "iter": it, **ev})

    # 收尾:保存最终权重副本
    final = os.path.join(WEIGHTS_DIR, "final.sdw")
    ppo.net.save_weights_bin(final)
    log_json({"type": "done", "final": final, "total_steps": total_steps})


if __name__ == "__main__":
    main()
