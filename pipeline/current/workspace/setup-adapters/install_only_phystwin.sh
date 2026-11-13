#!/usr/bin/env bash
set -euo pipefail
export PATH="/home/cw-wfm/Documents/Codex/2026-10-20/https-drive-google-com-drive-folders/outputs/WFM_KAIST/workspace/bootstrap-tools/bin:$PATH"
export UV_CACHE_DIR="/home/cw-wfm/Documents/Codex/2026-10-20/https-drive-google-com-drive-folders/outputs/WFM_KAIST/workspace/uv-cache" UV_PYTHON_INSTALL_DIR="/home/cw-wfm/Documents/Codex/2026-10-20/https-drive-google-com-drive-folders/outputs/WFM_KAIST/workspace/python"
ROOT="${1:?Pass prepared workspace root}"
export TORCH_CUDA_ARCH_LIST=8.9 MAX_JOBS=4 FORCE_CUDA=1
PY="$ROOT/phystwin-env/bin/python"
if [ ! -x "$ROOT/phystwin-env/bin/python" ]; then uv venv --python 3.10 $ROOT/phystwin-env; fi
uv pip install --python "$PY" torch==2.7.0 torchvision==0.22.0 --index-url https://download.pytorch.org/whl/cu128
uv pip install --python "$PY" numpy==2.2.6 opencv-python==4.11.0.86 opencv-python-headless==4.11.0.86 open3d==0.19.0 imageio imageio-ffmpeg matplotlib scipy scikit-learn einops tqdm setuptools==69.5.1 pip wheel ninja packaging easydict rembg onnxruntime trimesh==4.10.0 xatlas pyvista pymeshfix igraph openpyxl transformers==4.46.3 plyfile spconv-cu120==2.3.6 kornia==0.8.2 rtree 'pyglet<2' pyrender
