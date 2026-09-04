# monitor.py - 训练进度摘要(读 rl/logs/train_log.jsonl)
# 用法: python3 monitor.py [--tail 20]

import argparse
import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG = os.path.join(ROOT, "logs", "train_log.jsonl")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tail", type=int, default=20)
    args = ap.parse_args()

    if not os.path.exists(LOG):
        print("尚无日志:", LOG)
        return
    iters, evals = [], []
    with open(LOG) as f:
        for line in f:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("type") == "iter":
                iters.append(rec)
            elif rec.get("type") == "eval":
                evals.append(rec)
            elif rec.get("type") in ("stop", "done"):
                print("终止记录:", rec)

    print(f"=== 迭代记录 {len(iters)} 条,评测记录 {len(evals)} 条 ===")
    print(f"{'iter':>6} {'steps':>10} {'sps':>7} {'win':>6} {'diff':>6} "
          f"{'ent':>6} {'kl':>7} {'ep_len':>6}")
    for r in iters[-args.tail:]:
        print(f"{r['iter']:>6} {r['total_steps']:>10} {r['sps']:>7} "
              f"{r['win']:>6.3f} {r['score_diff']:>6.2f} "
              f"{r['ent']:>6.3f} {r['approx_kl']:>7.4f} {r['ep_len']:>6.1f}")
    print("--- 最近评测 ---")
    for r in evals[-max(3, args.tail // 4):]:
        keys = {k: v for k, v in r.items() if k not in ("type", "iter")}
        print(f"iter={r['iter']}", {k: round(v, 3) for k, v in keys.items()})


if __name__ == "__main__":
    main()
