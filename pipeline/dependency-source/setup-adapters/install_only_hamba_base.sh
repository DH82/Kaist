#!/usr/bin/env bash
# Base environment tested on Colab A100 2026-09-22; CUDA extensions are separate.
set -euo pipefail
export PATH="/home/cw-wfm/Documents/Codex/2026-10-20/https-drive-google-com-drive-folders/outputs/WFM_KAIST/workspace/bootstrap-tools/bin:$PATH"
export UV_CACHE_DIR="/home/cw-wfm/Documents/Codex/2026-10-20/https-drive-google-com-drive-folders/outputs/WFM_KAIST/workspace/uv-cache" UV_PYTHON_INSTALL_DIR="/home/cw-wfm/Documents/Codex/2026-10-20/https-drive-google-com-drive-folders/outputs/WFM_KAIST/workspace/python"
ROOT="${1:?Pass prepared workspace root}"
# On a Colab/root session. On a desktop, install these once using sudo.
command -v uv >/dev/null
if [ ! -x "$ROOT/hamba-env/bin/python" ]; then uv venv --python 3.10 "$ROOT/hamba-env"; fi
PY="$ROOT/hamba-env/bin/python"
uv pip install --python "$PY" torch==2.7.0 torchvision==0.22.0 --index-url https://download.pytorch.org/whl/cu128
uv pip install --python "$PY" numpy==1.26.4 setuptools==69.5.1 pip wheel ninja packaging scipy==1.15.3 pandas==2.3.3 scikit-learn==1.7.2 opencv-python==4.11.0.86 open3d==0.19.0 smplx==0.1.28 mediapipe==0.10.14 pytorch-lightning==2.6.5 timm==1.0.28 einops==0.8.2 yacs pyrender hydra-core hydra-submitit-launcher hydra-colorlog pyrootutils rich webdataset gdown scikit-image xtcocotools==1.14.3
