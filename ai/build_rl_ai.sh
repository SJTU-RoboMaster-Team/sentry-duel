#!/usr/bin/env bash
# build_rl_ai.sh - 编译 RL 部署 AI
# 先用 rl/training/export_cpp.py 生成 ai/rl_weights.h,再执行本脚本。
set -e
cd "$(dirname "$0")"
g++ -std=c++17 -O2 -fPIC -shared -Wl,-z,lazy -Wl,--allow-shlib-undefined \
    -I../engine/include -I.. -I../rl/env \
    rl_ai.cpp -o rl_ai.so
echo "built ai/rl_ai.so"
