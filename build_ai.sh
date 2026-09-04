#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

case "${1:-}" in
    hunter|hunter_ai|hunter_ai.so)
        TARGET="hunter_ai.so"
        ;;
    baseline|baseline_ai|baseline_ai.so)
        TARGET="baseline_ai.so"
        ;;
    all)
        TARGET="all"
        ;;
    *)
        echo "Usage: $0 {hunter|baseline|all}" >&2
        exit 2
        ;;
esac

make -C "$SCRIPT_DIR/ai" -j"$(nproc)" "$TARGET"
if [[ "$TARGET" == "all" ]]; then
    echo "Built: $SCRIPT_DIR/ai/baseline_ai.so and $SCRIPT_DIR/ai/hunter_ai.so"
else
    echo "Built: $SCRIPT_DIR/ai/$TARGET"
fi
