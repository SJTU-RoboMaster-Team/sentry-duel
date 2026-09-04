#!/usr/bin/env bash
# run_traces.sh - 用指定 runner 对全部确定性用例生成 trace(stdout/stderr 分文件)
#
# 用法:
#   ./run_traces.sh <runner 路径> <输出目录> [det .so 目录,默认 /tmp/det]
#
# 说明:trace 的 start 事件包含 .so 路径字符串,因此两次对比运行
#       必须使用相同的 .so 路径(默认都放在 /tmp/det 下)。
#       用例:sleep 每场约 20s(20 回合 × 1s 超时),全套约 50s。

set -euo pipefail

RUNNER="$(readlink -f "$1")"
OUT="$2"
DET="${3:-/tmp/det}"

mkdir -p "$OUT"

# runner 依赖同目录的 libsentry_duel_engine.so
export LD_LIBRARY_PATH="$(dirname "$RUNNER")${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"

run() {  # <用例名> <红方.so> <蓝方.so> [额外参数...]
    local name="$1" red="$2" blue="$3"
    shift 3
    "$RUNNER" --red "$DET/$red" --blue "$DET/$blue" "$@" \
        >"$OUT/$name.out" 2>"$OUT/$name.err"
    echo "[trace] $name"
}

run a_vs_b      det_ai_a.so     det_ai_b.so
run b_vs_a      det_ai_b.so     det_ai_a.so
run a_vs_a      det_ai_a.so     det_ai_a.so
run a_vs_b_t25  det_ai_a.so     det_ai_b.so   --max-turns 25
run sleep_vs_a  det_ai_sleep.so det_ai_a.so
run a_vs_sleep  det_ai_a.so     det_ai_sleep.so
run crash_vs_a  det_ai_crash.so det_ai_a.so
run a_vs_crash  det_ai_a.so     det_ai_crash.so

echo "[trace] 全部完成,输出目录: $OUT"
