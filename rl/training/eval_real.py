# eval_real.py - 用真实引擎(engine/build/runner)做验收评测
# 对指定对手各打 n_games 局(红蓝各半),统计胜率 / 崩溃 / 超时。
#
# 用法: python3 eval_real.py --ai ai/rl_ai.so --games 2000 --jobs 32
#        [--opp ai/baseline_ai.so ai/hunter_ai.so]

import argparse
import json
import os
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
RUNNER = os.path.join(REPO, "engine", "build", "runner")

WINNER_RE = re.compile(rb'"winner":"([RBD])"')
REASON_RE = re.compile(rb'"reason":"([^"]+)"')
# runner 的超时提示写在 stderr,前缀只可能是 "[engine] red AI" 或 "[engine] blue AI"
RED_TIMEOUT = b"[engine] red AI"
BLUE_TIMEOUT = b"[engine] blue AI"


def run_one(red, blue):
    """跑一局,返回 (winner, reason, red_timeout, blue_timeout, stderr)。"""
    p = subprocess.run([RUNNER, "--red", red, "--blue", blue],
                       capture_output=True, timeout=30)
    out, err = p.stdout, p.stderr
    m = WINNER_RE.search(out)
    r = REASON_RE.search(out)
    winner = m.group(1).decode() if m else "?"
    reason = r.group(1).decode() if r else "?"
    # runner 超时提示写在 stderr: "[engine] red AI 超时，蓝方 +1" 等
    red_to = RED_TIMEOUT in err
    blue_to = BLUE_TIMEOUT in err
    return winner, reason, red_to, blue_to, err.decode(errors="replace")


def eval_pair(ai_path, opp_path, n_games, jobs):
    """ai 与 opp 对战 n_games 局,红蓝各半。返回统计 dict。"""
    half = n_games // 2
    tasks = []
    for i in range(half):
        tasks.append((ai_path, opp_path, True))   # ai 执红
        tasks.append((opp_path, ai_path, False))  # ai 执蓝
    n = len(tasks)

    win = draw = loss = 0
    crash = timeout = 0
    reasons = {}
    with ThreadPoolExecutor(max_workers=jobs) as ex:
        def work(t):
            red, blue, ai_is_red = t
            return run_one(red, blue) + (ai_is_red,)

        for winner, reason, red_to, blue_to, err, ai_is_red in ex.map(work, tasks):
            ai_side = "R" if ai_is_red else "B"
            reasons[reason] = reasons.get(reason, 0) + 1
            if winner == ai_side:
                win += 1
            elif winner == "D":
                draw += 1
            else:
                loss += 1
            if reason == f"{'red' if ai_is_red else 'blue'}_crashed":
                crash += 1
            if (ai_is_red and red_to) or (not ai_is_red and blue_to):
                timeout += 1
    return {
        "opp": os.path.basename(opp_path), "games": n,
        "win": win, "draw": draw, "loss": loss,
        "win_rate": (win + 0.5 * draw) / n,
        "ai_crash": crash, "ai_timeout": timeout,
        "reasons": reasons,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ai", required=True, help="被评测的 AI .so")
    ap.add_argument("--opp", nargs="+",
                    default=[os.path.join(REPO, "ai", "baseline_ai.so"),
                             os.path.join(REPO, "ai", "hunter_ai.so")])
    ap.add_argument("--games", type=int, default=2000, help="每个对手的总局数(双色各半)")
    ap.add_argument("--jobs", type=int, default=32)
    args = ap.parse_args()

    if not os.path.exists(RUNNER):
        print(f"runner 不存在: {RUNNER}", file=sys.stderr)
        sys.exit(1)

    all_ok = True
    for opp in args.opp:
        st = eval_pair(args.ai, opp, args.games, args.jobs)
        ok = st["win_rate"] >= 0.90 and st["ai_crash"] == 0 and st["ai_timeout"] == 0
        all_ok &= ok
        print(json.dumps(st, ensure_ascii=False))
        print(f"  => {'PASS' if ok else 'FAIL'} (需要 win_rate>=0.90 且 0 崩溃 0 超时)")
    sys.exit(0 if all_ok else 1)


if __name__ == "__main__":
    main()
