#!/bin/bash
set -euo pipefail
ROOT="/home/cw-wfm/Documents/Codex/2026-10-20/https-drive-google-com-drive-folders/outputs/WFM_KAIST/workspace"
PY="$ROOT/phystwin-env/bin/python"
source "/home/cw-wfm/Documents/Codex/2026-10-20/https-drive-google-com-drive-folders/outputs/WFM_KAIST/stepper/toolchain.sh"
uv pip install --python "$PY" --no-build-isolation 'git+https://github.com/facebookresearch/pytorch3d.git@v0.7.9'
uv pip install --python "$PY" --no-build-isolation 'git+https://github.com/NVlabs/nvdiffrast.git@v0.4.0'
if [ ! -d "$ROOT/diffoctreerast/.git" ]; then git clone --recurse-submodules https://github.com/JeffreyXiang/diffoctreerast.git $ROOT/diffoctreerast; fi
uv pip install --python "$PY" --no-build-isolation $ROOT/diffoctreerast
if [ ! -d "$ROOT/mip-splatting/.git" ]; then git clone --recurse-submodules https://github.com/autonomousvision/mip-splatting.git $ROOT/mip-splatting; fi
uv pip install --python "$PY" --no-build-isolation $ROOT/mip-splatting/submodules/diff-gaussian-rasterization
uv pip check --python "$PY"
# The existing runtime already has time, ffmpeg, xvfb, libosmesa6, libegl1,
# libgl1-mesa-dri installed. Desktop review requires an actual display, not Xvfb.
