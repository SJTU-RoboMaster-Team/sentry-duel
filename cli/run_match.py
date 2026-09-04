#!/usr/bin/env python3
"""CLI:跑一局哨兵大战并落盘 JSON 录像。

用法:
  python3 run_match.py --red ai/reactive_ai.so --blue ai/baseline_ai.so \
                      --game-id demo1 --out replays/

输出:
  replays/game_<id>.json - 完整事件序列(每回合)
"""
import argparse
import json
import os
import subprocess
import sys
from pathlib import Path


def main():
    p = argparse.ArgumentParser(description="跑一局哨兵大战")
    p.add_argument("--red", required=True, help="红方 .so 路径")
    p.add_argument("--blue", required=True, help="蓝方 .so 路径")
    p.add_argument("--game-id", default="0001", help="对局 ID,用于文件名")
    p.add_argument("--out", default="replays/", help="录像输出目录")
    p.add_argument("--max-turns", type=int, default=20)
    p.add_argument("--engine", default="engine/build/runner")
    args = p.parse_args()

    # 找 runner 绝对路径
    project_root = Path(__file__).parent.parent.resolve()
    engine = project_root / args.engine
    if not engine.exists():
        sys.exit(f"找不到引擎: {engine} - 请先 cmake 构建")

    red = project_root / args.red
    blue = project_root / args.blue
    if not red.exists():
        sys.exit(f"找不到红方 .so: {red}")
    if not blue.exists():
        sys.exit(f"找不到蓝方 .so: {blue}")

    out_dir = project_root / args.out
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"game_{args.game_id}.json"

    env = os.environ.copy()
    env["LD_LIBRARY_PATH"] = str(project_root / "engine/build") + ":" + env.get("LD_LIBRARY_PATH", "")

    proc = subprocess.run(
        [str(engine), "--red", str(red), "--blue", str(blue),
         "--game-id", args.game_id, "--max-turns", str(args.max_turns)],
        capture_output=True, text=True, env=env,
    )

    if proc.returncode != 0:
        print(f"[runner] 退出码 {proc.returncode}", file=sys.stderr)
        print(proc.stderr, file=sys.stderr)
        sys.exit(1)

    # 解析 NDJSON 输出
    events = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError as e:
            print(f"[warn] 跳过非 JSON 行: {line[:80]}", file=sys.stderr)

    out_path.write_text("\n".join(json.dumps(e, ensure_ascii=False) for e in events) + "\n")
    print(f"[ok] 录像写入 {out_path} ({len(events)} 个事件)")

    # 输出摘要
    over = next((e for e in events if e["type"] == "game_over"), None)
    if over:
        print(f"     比分 R={over['red_score']} B={over['blue_score']} 胜方={over['winner']}")
        print(f"     原因: {over['reason']}  回合数: {over['turns']}")


if __name__ == "__main__":
    main()