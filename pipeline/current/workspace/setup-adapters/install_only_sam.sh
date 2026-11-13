#!/usr/bin/env bash
set -euo pipefail
export PATH="/home/cw-wfm/Documents/Codex/2026-10-20/https-drive-google-com-drive-folders/outputs/WFM_KAIST/workspace/bootstrap-tools/bin:$PATH"
export UV_CACHE_DIR="/home/cw-wfm/Documents/Codex/2026-10-20/https-drive-google-com-drive-folders/outputs/WFM_KAIST/workspace/uv-cache" UV_PYTHON_INSTALL_DIR="/home/cw-wfm/Documents/Codex/2026-10-20/https-drive-google-com-drive-folders/outputs/WFM_KAIST/workspace/python"
# Verified import + CUDA matmul on Colab A100, Ubuntu 24.04.1, 2026-09-22.
# Weight download and actual inference still require approved HF access.
ROOT="${1:?Pass prepared workspace root}"
if [ ! -x "$ROOT/sam-env/bin/python" ]; then uv venv --python 3.12 "$ROOT/sam-env"; fi
PY="$ROOT/sam-env/bin/python"
uv pip install --python "$PY" torch==2.10.0 torchvision==0.25.0 --index-url https://download.pytorch.org/whl/cu128
if [ ! -d "$ROOT/sam3/.git" ]; then
  git clone --branch sam3.1 https://github.com/facebookresearch/sam3.git "$ROOT/sam3"
fi
git -C "$ROOT/sam3" checkout 20dba30
uv pip install --python "$PY" -e "$ROOT/sam3" numpy==1.26.4 opencv-python-headless==4.11.0.86 decord hydra-core huggingface_hub einops timm psutil pycocotools==2.0.11 scipy matplotlib pandas fairscale submitit
uv pip freeze --python "$PY" > "$ROOT/sam-resolved-freeze.txt"
