"""Batch match execution for formal matches and qualifiers."""
from __future__ import annotations

import json
import os
import secrets
import subprocess
from pathlib import Path
from typing import Callable, Optional

from jobs import ENGINE_BIN

ProgressCallback = Callable[[dict], None]


def play_game(red_so: Path, blue_so: Path, max_turns: int = 20) -> dict:
    """Run one isolated game and return its final event."""
    env = os.environ.copy()
    env["LD_LIBRARY_PATH"] = (
        str(ENGINE_BIN.parent) + ":" + env.get("LD_LIBRARY_PATH", "")
    )
    game_id = str(secrets.randbelow(2_000_000_000) + 1)
    try:
        result = subprocess.run(
            [str(ENGINE_BIN), "--red", str(red_so), "--blue", str(blue_so),
             "--max-turns", str(max_turns), "--game-id", game_id],
            capture_output=True, text=True, timeout=30, env=env,
            cwd=str(ENGINE_BIN.parent.parent.parent),
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("单局运行超过 30 秒") from exc
    if result.returncode != 0:
        error = result.stderr.strip()[:500] or f"退出码 {result.returncode}"
        raise RuntimeError(f"单局运行失败: {error}")
    for line in reversed(result.stdout.splitlines()):
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "game_over":
            return event
    raise RuntimeError("单局没有返回 game_over 结果")


def run_balanced_series(ai_a: Path, ai_b: Path, games_per_side: int = 50,
                        progress: Optional[ProgressCallback] = None) -> dict:
    """Play A-red then B-red, and report all numbers from A's perspective."""
    if not ENGINE_BIN.is_file():
        raise RuntimeError(f"引擎未构建: {ENGINE_BIN}")
    if not ai_a.is_file() or not ai_b.is_file():
        raise RuntimeError("参赛 AI 文件不存在")

    totals = {"games": 0, "a_score": 0, "b_score": 0,
              "a_wins": 0, "b_wins": 0, "draws": 0, "outcomes": []}
    schedule = [(ai_a, ai_b, True)] * games_per_side
    schedule += [(ai_b, ai_a, False)] * games_per_side
    for red, blue, a_is_red in schedule:
        event = play_game(red, blue)
        red_score = int(event.get("red_score", 0))
        blue_score = int(event.get("blue_score", 0))
        a_score, b_score = ((red_score, blue_score) if a_is_red
                            else (blue_score, red_score))
        totals["games"] += 1
        totals["a_score"] += a_score
        totals["b_score"] += b_score
        if a_score > b_score:
            totals["a_wins"] += 1
            totals["outcomes"].append("a")
        elif b_score > a_score:
            totals["b_wins"] += 1
            totals["outcomes"].append("b")
        else:
            totals["draws"] += 1
            totals["outcomes"].append("draw")
        if progress:
            progress({**totals, "outcomes": list(totals["outcomes"])})
    return totals
