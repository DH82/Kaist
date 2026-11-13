# syntax=docker/dockerfile:1.7
# KAIST GPU 파이프라인 이미지: RAW → QC/전처리 → SAM3 → 추적 → 3D 복원 → HaMBA·MANO → NAS 저장
#
# Python 환경 3개(sam-env / phystwin-env / hamba-env)는 빌드할 때 인터넷에서 받아 새로 만듭니다.
#   - 패키지 버전: gpu/env/*.lock (검증된 pc1 환경의 freeze, 2026-11-13)
#   - git·로컬 CUDA 확장: gpu/env/build_envs.sh (원본 setup-adapters와 같은 커밋·옵션)
# 모델 weight는 이미지에 넣지 않습니다. 실행할 때 compose가 호스트의 weight 폴더를 읽기 전용으로 마운트합니다.
#
# 원래 환경의 절대경로(DEP_ROOT / REV_ROOT)를 그대로 씁니다. config.env·editable 설치가 이 경로를
# 기준으로 하기 때문입니다. 새 PC의 사용자 이름·clone 위치와는 무관합니다.
#
# 빌드는 compose.yaml의 gpu-worker 서비스가 아래 추가 컨텍스트를 넘겨서 합니다.
#   dependency-source = pipeline/dependency-source (editable·CUDA 확장 소스: sam3, co-tracker, kaist-full …)
#   rev               = pipeline/current          (00_scripts + 실행용 workspace 코드)
#   worker            = worker/app                (GPU Worker 코드)

ARG DEP_ROOT=/home/cw-wfm/Documents/Codex/2026-10-20/https-drive-google-com-drive-folders/outputs/WFM_KAIST/workspace

# ---------------------------------------------------------------------------------------------
# 1단계: CUDA 12.8 컴파일 환경에서 Python 환경 3개를 만듭니다.
# ---------------------------------------------------------------------------------------------
FROM nvidia/cuda:12.8.1-devel-ubuntu24.04 AS envs
ARG DEP_ROOT
# GPU 세대(compute capability). RTX 4090 = 8.9, RTX 5090 = 12.0. 여러 개는 "8.9;12.0".
ARG TORCH_CUDA_ARCH_LIST=8.9
ARG MAX_JOBS=4
ENV DEBIAN_FRONTEND=noninteractive KAIST_DEP_ROOT=${DEP_ROOT} \
    TORCH_CUDA_ARCH_LIST=${TORCH_CUDA_ARCH_LIST} MAX_JOBS=${MAX_JOBS} \
    UV_CACHE_DIR=/root/.cache/uv
RUN apt-get update && apt-get install -y --no-install-recommends \
      git ca-certificates build-essential ninja-build pkg-config \
      libgl-dev libegl-dev libglvnd-dev libgles-dev \
    && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:0.13.4 /uv /usr/local/bin/uv
COPY env/ /opt/kaist-env/

RUN --mount=type=cache,target=/root/.cache/uv /opt/kaist-env/build_envs.sh python
# 소스는 해당 환경 직전에 복사해 한 소스가 바뀌어도 다른 환경 층은 캐시를 재사용합니다.
COPY --from=dependency-source sam3 ${DEP_ROOT}/sam3
RUN --mount=type=cache,target=/root/.cache/uv /opt/kaist-env/build_envs.sh sam
COPY --from=dependency-source co-tracker     ${DEP_ROOT}/co-tracker
COPY --from=dependency-source diffoctreerast ${DEP_ROOT}/diffoctreerast
COPY --from=dependency-source mip-splatting  ${DEP_ROOT}/mip-splatting
RUN --mount=type=cache,target=/root/.cache/uv /opt/kaist-env/build_envs.sh phystwin
COPY --from=dependency-source kaist-full     ${DEP_ROOT}/kaist-full
RUN --mount=type=cache,target=/root/.cache/uv /opt/kaist-env/build_envs.sh hamba

# ---------------------------------------------------------------------------------------------
# 2단계: 실행 이미지 (컴파일러 없음)
# ---------------------------------------------------------------------------------------------
FROM ubuntu:26.04
ARG DEP_ROOT
ARG REV_ROOT="/home/cw-wfm/Desktop/worker1/Kaist pipeline revising/02_current"
ARG WORKER_ROOT=/home/cw-wfm/Desktop/worker1/airflow
ARG HOST_UID=1000

ENV DEBIAN_FRONTEND=noninteractive LANG=C.UTF-8 LC_ALL=C.UTF-8 TZ=Asia/Seoul \
    NVIDIA_VISIBLE_DEVICES=all NVIDIA_DRIVER_CAPABILITIES=compute,utility,graphics,video

# control_python(/usr/bin/python3)은 Ubuntu 26.04의 Python 3.14 + PIL/yaml입니다.
# 나머지는 환경 3개의 확장 모듈이 링크하는 시스템 라이브러리(ldd 조사)와 EGL·xvfb·ffmpeg입니다.
RUN apt-get update && apt-get install -y --no-install-recommends \
      python3 python3-pil python3-yaml ca-certificates tzdata tini \
      ffmpeg xvfb xauth rsync \
      libgl1 libegl1 libgles2 libopengl0 libglvnd0 libglx0 libglib2.0-0t64 \
      libsm6 libice6 libx11-6 libxext6 libxau6 libxcb1 libxdmcp6 libxrender1 \
      libgomp1 libatomic1 libudev1 libibverbs1 ibverbs-providers libnl-3-200 libnl-route-3-200 \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /usr/share/glvnd/egl_vendor.d \
    && printf '{"file_format_version":"1.0.0","ICD":{"library_path":"libEGL_nvidia.so.0"}}\n' \
         > /usr/share/glvnd/egl_vendor.d/10_nvidia.json \
    && (userdel -r ubuntu 2>/dev/null || true) \
    && useradd -m -u ${HOST_UID} -s /bin/bash kaist \
    && mkdir -p /home/cw-wfm && chown ${HOST_UID} /home/cw-wfm

# ---- 큰 층: Python 3.12/3.10과 환경 3개 + editable 소스 ----
COPY --from=envs --chown=${HOST_UID} ${DEP_ROOT} ${DEP_ROOT}
# weight 마운트 자리와 쓰기 가능한 실행 캐시(warp·matplotlib 등, gpu-cache 볼륨)
RUN mkdir -p "${DEP_ROOT}/hf-cache/hub" "${DEP_ROOT}/runtime-cache" \
 && chown -R ${HOST_UID} "${DEP_ROOT}/hf-cache" "${DEP_ROOT}/runtime-cache"

# ---- 파이프라인 코드 (02_current: workspace 코드 + 00_scripts) ----
COPY --from=rev --chown=${HOST_UID} ["workspace", "${REV_ROOT}/workspace"]
COPY --from=rev --chown=${HOST_UID} ["project", "${REV_ROOT}/project"]

# 원래 PC와 같은 심볼릭 링크: 환경 3개, 그리고 weight(실행 시 DEP_ROOT 아래에 마운트됨)
COPY env/link_workspace.sh env/weights.txt /opt/kaist/
RUN /opt/kaist/link_workspace.sh "${DEP_ROOT}" "${REV_ROOT}/workspace" \
 && chown -h -R ${HOST_UID} "${REV_ROOT}/workspace"

# ---- GPU Worker (UI·Airflow가 호출하는 HTTP 서비스) ----
COPY requirements-worker.txt /opt/kaist/requirements-worker.txt
RUN "${DEP_ROOT}/python/cpython-3.10.22-linux-x86_64-gnu/bin/python3.10" -m venv ${WORKER_ROOT}/venv \
 && ${WORKER_ROOT}/venv/bin/pip install --no-cache-dir -q \
      -r /opt/kaist/requirements-worker.txt \
 && mkdir -p ${WORKER_ROOT}/config ${WORKER_ROOT}/worker/jobs \
 && chown -R ${HOST_UID} ${WORKER_ROOT}
# worker/ 폴더는 실행 시 호스트 상태 폴더(gpu_state/worker: secret, jobs, 등록 폴더, GPU lock)를
# 마운트하는 자리입니다. 코드는 /opt/kaist/worker-code에 두고 entrypoint가 매번 덮어씁니다.
COPY --from=worker --chown=${HOST_UID} worker  /opt/kaist/worker-code
COPY --from=worker --chown=${HOST_UID} dags    ${WORKER_ROOT}/dags
COPY --from=worker --chown=${HOST_UID} review  ${WORKER_ROOT}/review
COPY --chown=${HOST_UID} entrypoint.sh kaist_run.py selfcheck.py /opt/kaist/
RUN chmod +x /opt/kaist/entrypoint.sh /opt/kaist/kaist_run.py \
 && ln -s /opt/kaist/kaist_run.py /usr/local/bin/kaist-run

ENV KAIST_DEP_ROOT="${DEP_ROOT}" KAIST_REV_ROOT="${REV_ROOT}" KAIST_WORKER_ROOT="${WORKER_ROOT}" \
    KAIST_AIRFLOW_CONFIG_DIR=${WORKER_ROOT}/config \
    HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 PYTHONDONTWRITEBYTECODE=1 HOME=/home/kaist
USER ${HOST_UID}
WORKDIR /home/kaist
ENTRYPOINT ["/usr/bin/tini", "--", "/opt/kaist/entrypoint.sh"]
CMD ["worker"]
