#!/usr/bin/env bash
# build.sh - 构建 sentry_env pybind 模块
set -e
cd "$(dirname "$0")"
REPO=$(cd ../.. && pwd)
g++ -std=c++17 -O2 -fPIC -Wall -Wextra -shared \
    sentry_env.cpp \
    -I"$REPO/engine/include" -I. \
    $(python3 -m pybind11 --includes) \
    -L"$REPO/engine/build" -lsentry_duel_engine \
    -Wl,-rpath,"$REPO/engine/build" \
    -ldl \
    -o "sentry_env$(python3-config --extension-suffix)"
echo "built sentry_env$(python3-config --extension-suffix)"
