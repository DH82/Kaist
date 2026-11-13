#!/usr/bin/env bash
# Adapted from Colab setup; fresh RTX 5090 execution NOT yet validated.
set -euo pipefail
ROOT="${1:?Pass prepared workspace root}"
PY="$ROOT/hamba-env/bin/python"
HAMBA="$ROOT/kaist-full/PhysTwin/Mono/data_process/Hamba"
export TORCH_CUDA_ARCH_LIST="${TORCH_CUDA_ARCH_LIST:-12.0}"
export MAX_JOBS="${MAX_JOBS:-4}"
export FORCE_CUDA=1
uv pip install --python "$PY" --no-build-isolation 'chumpy @ git+https://github.com/mattloper/chumpy@580566eafc9ac68b2614b64d6f7aaa84eebb70da' mmcv==1.3.9
uv pip install --python "$PY" --no-build-isolation 'detectron2 @ git+https://github.com/facebookresearch/detectron2@b4a4a3bd136852dae5fb1de37978dee412653e31'
uv pip install --python "$PY" --no-build-isolation 'git+https://github.com/facebookresearch/pytorch3d.git@33824be3cbc87a7dd1db0f6a9a9de9ac81b2d0ba'
uv pip install --python "$PY" --no-build-isolation "$HAMBA/VMamba/kernels/selective_scan"
uv pip install --python "$PY" --no-deps -e "$HAMBA/third-party/ViTPose" -e "$HAMBA"
uv pip install --python "$PY" protobuf==4.25.9 tensorboard==2.18.0 json-tricks munkres
uv pip check --python "$PY"
uv pip freeze --python "$PY" > "$ROOT/hamba-resolved-freeze.txt"
