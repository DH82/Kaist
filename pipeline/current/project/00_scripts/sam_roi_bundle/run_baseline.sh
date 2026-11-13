#!/usr/bin/env bash
set -euo pipefail
ROOT="${1:?environment root}"; INPUT="${2:?preprocessed episode}"; OUT="${3:?new output directory}"; PROMPT="${4:?object prompt}"; SCENE="${5:-2d}"
HERE="$(cd -- "$(dirname -- "$0")" && pwd)"
export HF_HOME="$ROOT/hf-cache"
"$ROOT/sam-env/bin/python" - "$INPUT" "$OUT" <<'PY'
import sys,json,shutil
from pathlib import Path
src,out=map(Path,sys.argv[1:]); m=json.loads((src/'metadata.json').read_text())
assert 0<int(m['frame_num'])<=300, 'Use 1..300 frames'
assert not out.exists(), 'Use a new output directory'
for c in range(int(m.get('num_cameras',4))):
 assert (src/f'color/{c}.mp4').is_file(), f'Missing camera video {c}'
 assert (src/f'depth/{c}').is_dir(), f'Missing depth folder {c}'
 for f in range(int(m['frame_num'])):
  assert (src/f'color/{c}/{f}.png').is_file(), f'Missing RGB {c}/{f}'
case=out/'case'; case.mkdir(parents=True)
for folder in ['color','depth']: shutil.copytree(src/folder,case/folder)
shutil.copy2(src/'metadata.json',case/'metadata.json')
PY
"$ROOT/sam-env/bin/python" "$HERE/tools/measure.py" --root "$ROOT" --label sam-baseline -- "$ROOT/sam-env/bin/python" "$HERE/source/segment_object_sam3.py" --base_path "$OUT" --case_name case --object_prompts "$PROMPT" --scene_type "$SCENE" --chunk_frames 0 --version sam3.1
