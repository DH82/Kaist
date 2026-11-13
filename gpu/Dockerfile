# syntax=docker/dockerfile:1.7
# KAIST GPU 파이프라인 이미지: RAW → QC/전처리 → SAM3 → 추적 → 3D 복원 → HaMBA·MANO → NAS 저장
#
# GPU에서 검증된 기존 환경(sam-env / phystwin-env / hamba-env, CUDA 12.8 툴체인, 모델 가중치)을
# 다시 빌드하지 않고 원래 절대경로 그대로 복사합니다. venv·editable 설치·config.env가 절대경로를
# 쓰므로 경로를 바꾸면 동작이 달라질 수 있기 때문입니다. 호스트 경로는 실행 시 같은 경로로 마운트합니다.
#
# 빌드는 compose.yaml의 gpu-worker 서비스(또는 gpu/build.sh)가 아래 추가 컨텍스트를 넘겨서 합니다.
#   dep    = WFM_KAIST/workspace       (Python, 환경 3개, 툴체인, 모델 캐시)
#   rev    = Kaist pipeline revising/02_current (00_scripts, workspace 코드)
#   worker = kaist-pc-stack/worker/app  (GPU Worker 코드)
FROM ubuntu:26.04

ARG DEP_ROOT=/home/cw-wfm/Documents/Codex/2026-10-20/https-drive-google-com-drive-folders/outputs/WFM_KAIST/workspace
ARG REV_ROOT="/home/cw-wfm/Desktop/worker1/Kaist pipeline revising/02_current"
ARG WORKER_ROOT=/home/cw-wfm/Desktop/worker1/airflow
ARG HOST_UID=1000

ENV DEBIAN_FRONTEND=noninteractive LANG=C.UTF-8 LC_ALL=C.UTF-8 TZ=Asia/Seoul \
    NVIDIA_VISIBLE_DEVICES=all NVIDIA_DRIVER_CAPABILITIES=compute,utility,graphics,video

# control_python(/usr/bin/python3)은 호스트와 같은 Ubuntu 26.04의 Python 3.14 + PIL/yaml입니다.
# 나머지는 환경 3개의 확장 모듈이 링크하는 시스템 라이브러리(ldd 조사)와 EGL·xvfb·ffmpeg입니다.
RUN apt-get update && apt-get install -y --no-install-recommends \
      python3 python3-pil python3-yaml ca-certificates tzdata tini \
      ffmpeg xvfb xauth \
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

# ---- 큰 층: 자주 바뀌지 않음 (Python, 환경, 툴체인, 모델) ----
COPY --from=dep --chown=${HOST_UID} python        ${DEP_ROOT}/python
COPY --from=dep --chown=${HOST_UID} toolchain     ${DEP_ROOT}/toolchain
COPY --from=dep --chown=${HOST_UID} sam-env       ${DEP_ROOT}/sam-env
COPY --from=dep --chown=${HOST_UID} phystwin-env  ${DEP_ROOT}/phystwin-env
COPY --from=dep --chown=${HOST_UID} hamba-env     ${DEP_ROOT}/hamba-env
COPY --from=dep --chown=${HOST_UID} kaist-full    ${DEP_ROOT}/kaist-full
COPY --from=dep --chown=${HOST_UID} co-tracker    ${DEP_ROOT}/co-tracker
COPY --from=dep --chown=${HOST_UID} sam3          ${DEP_ROOT}/sam3
COPY --from=dep --chown=${HOST_UID} diffoctreerast ${DEP_ROOT}/diffoctreerast
COPY --from=dep --chown=${HOST_UID} mip-splatting ${DEP_ROOT}/mip-splatting
COPY --from=dep --chown=${HOST_UID} episodes.json episodes.all.json ${DEP_ROOT}/
# 모델 캐시만 복사합니다. hf-cache의 token/stored_tokens(로그인 정보)는 이미지에 넣지 않습니다.
COPY --from=dep --chown=${HOST_UID} hf-cache/hub  ${DEP_ROOT}/hf-cache/hub
COPY --from=dep --chown=${HOST_UID} runtime-cache ${DEP_ROOT}/runtime-cache

# ---- 파이프라인 코드 (02_current: workspace 코드 + 00_scripts) ----
COPY --from=rev --chown=${HOST_UID} ["workspace", "${REV_ROOT}/workspace"]
COPY --from=rev --chown=${HOST_UID} ["project", "${REV_ROOT}/project"]

# ---- GPU Worker (UI·Airflow가 호출하는 HTTP 서비스) ----
RUN "${DEP_ROOT}/python/cpython-3.10.22-linux-x86_64-gnu/bin/python3.10" -m venv ${WORKER_ROOT}/venv \
 && ${WORKER_ROOT}/venv/bin/pip install --no-cache-dir -q \
      fastapi==0.136.3 uvicorn==0.52.4 httpx==0.28.1 pydantic==2.13.5 \
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
