#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

echo "[1/2] Building engine..."
cmake -S "$SCRIPT_DIR/engine" -B "$SCRIPT_DIR/engine/build"
cmake --build "$SCRIPT_DIR/engine/build" --parallel

echo "[2/2] Building AIs..."
make -C "$SCRIPT_DIR/ai" -j"$(nproc)"

echo "Build complete."
echo "Run a match with:"
echo "  LD_LIBRARY_PATH=$SCRIPT_DIR/engine/build $SCRIPT_DIR/engine/build/runner \\
    --red $SCRIPT_DIR/ai/hunter_ai.so \\
    --blue $SCRIPT_DIR/ai/baseline_ai.so"
