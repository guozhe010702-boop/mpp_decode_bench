#!/bin/sh
set -eu
cd "$(dirname "$0")"
seconds=${1:-60}
codec=${2:-hevc}
run="results/$(date +%Y%m%d-%H%M%S)-${codec}"
mkdir -p "$run"
for mode in drm nv12; do
    python3 live_mpp_bench.py --codec "$codec" --seconds "$seconds" \
        --output-mode "$mode" --output "$run/$mode"
done
printf 'Results: %s/%s\n' "$PWD" "$run"
