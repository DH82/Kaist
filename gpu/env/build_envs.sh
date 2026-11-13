#!/usr/bin/env bash
# Docker 빌드 단계에서 검증된 Python 환경 3개를 인터넷에서 받아 다시 만듭니다 (모델 weight 제외).
#   python   : uv로 CPython 3.12.15 / 3.10.22 설치 (원래와 같은 경로)
#   sam      : sam-env      (SAM3.1)
#   phystwin : phystwin-env (CoTracker · TRELLIS · 3D 복원)
#   hamba    : hamba-env    (HaMBA · ViTPose · MANO)
# 버전은 gpu/env/*.lock(원본 pc1 환경 freeze)으로 고정하고 --no-deps로 그대로 설치합니다.
# git·로컬 소스 패키지는 원본 setup-adapters와 같은 커밋·옵션으로 CUDA 확장을 컴파일합니다.
set -euo pipefail
DEP=${KAIST_DEP_ROOT:?}
LOCK=$(cd "$(dirname "$0")" && pwd)
export UV_PYTHON_INSTALL_DIR="$DEP/python" UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=automatic
export CUDA_HOME=${CUDA_HOME:-/usr/local/cuda} FORCE_CUDA=1
export TORCH_CUDA_ARCH_LIST=${TORCH_CUDA_ARCH_LIST:-8.9} MAX_JOBS=${MAX_JOBS:-4}
TORCH_INDEX=https://download.pytorch.org/whl/cu128
KAOLIN_LINKS=https://nvidia-kaolin.s3.us-east-2.amazonaws.com/torch-2.7.0_cu128.html

pinned() {  # 고정 버전 목록을 의존성 해석 없이 그대로 설치
  uv pip install --python "$1" --no-deps \
    --extra-index-url "$TORCH_INDEX" --find-links "$KAOLIN_LINKS" -r "$2"
}
source_build() {  # 이미 설치된 torch로 CUDA 확장 빌드
  uv pip install --python "$1" --no-deps --no-build-isolation "${@:2}"
}
editable() {  # 원본과 같이 격리 빌드로 소스 폴더를 편집 설치
  uv pip install --python "$1" --no-deps "${@:2}"
}
check() { uv pip check --python "$1"; }

case "${1:?python|sam|phystwin|hamba}" in
  python)
    uv python install 3.12.15 3.10.22 ;;
  sam)
    PY="$DEP/sam-env/bin/python"
    uv venv --python 3.12.15 "$DEP/sam-env"
    pinned "$PY" "$LOCK/sam.lock"
    editable "$PY" -e "$DEP/sam3"
    # decord 휠은 원본 환경에서도 'different platform' 경고가 납니다(동작 정상). 경고만 표시합니다.
    check "$PY" || true ;;
  phystwin)
    PY="$DEP/phystwin-env/bin/python"
    uv venv --python 3.10.22 "$DEP/phystwin-env"
    pinned "$PY" "$LOCK/phystwin.lock"
    source_build "$PY" \
      'pytorch3d @ git+https://github.com/facebookresearch/pytorch3d.git@33824be3cbc87a7dd1db0f6a9a9de9ac81b2d0ba' \
      'nvdiffrast @ git+https://github.com/NVlabs/nvdiffrast.git@253ac4fcea7de5f396371124af597e6cc957bfae' \
      'utils3d @ git+https://github.com/EasternJournalist/utils3d.git@9a4eb15e4021b67b12c460c7057d642626897ec8' \
      "$DEP/diffoctreerast" \
      "$DEP/mip-splatting/submodules/diff-gaussian-rasterization"
    editable "$PY" -e "$DEP/co-tracker"
    check "$PY" ;;
  hamba)
    PY="$DEP/hamba-env/bin/python"
    HAMBA="$DEP/kaist-full/PhysTwin/Mono/data_process/Hamba"
    uv venv --python 3.10.22 "$DEP/hamba-env"
    pinned "$PY" "$LOCK/hamba.lock"
    source_build "$PY" mmcv==1.3.9 \
      'chumpy @ git+https://github.com/mattloper/chumpy@580566eafc9ac68b2614b64d6f7aaa84eebb70da'
    source_build "$PY" \
      'detectron2 @ git+https://github.com/facebookresearch/detectron2@b4a4a3bd136852dae5fb1de37978dee412653e31' \
      'pytorch3d @ git+https://github.com/facebookresearch/pytorch3d.git@33824be3cbc87a7dd1db0f6a9a9de9ac81b2d0ba' \
      "$HAMBA/VMamba/kernels/selective_scan"
    editable "$PY" -e "$HAMBA/third-party/ViTPose" -e "$HAMBA"
    check "$PY" ;;
  *) echo "unknown stage: $1" >&2; exit 2 ;;
esac
