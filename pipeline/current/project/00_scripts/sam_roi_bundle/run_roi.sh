#!/usr/bin/env bash
set -euo pipefail
ROOT="${1:?environment root}"; CASE="${2:?baseline case}"; OUT="${3:?new candidate directory}"
HERE="$(cd -- "$(dirname -- "$0")" && pwd)"
export HF_HOME="$ROOT/hf-cache"
"$ROOT/sam-env/bin/python" "$HERE/tools/measure.py" --root "$ROOT" --label sam-roi -- "$ROOT/sam-env/bin/python" "$HERE/tools/sam_roi_candidate.py" --source-script "$HERE/source/segment_object_sam3.py" --case "$CASE" --out "$OUT" --profile "$HERE/configs/sam_roi_candidate_v1.json"
