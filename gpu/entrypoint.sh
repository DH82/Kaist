#!/usr/bin/env bash
# KAIST GPU 컨테이너 진입점
#   worker            GPU Worker HTTP 서비스 (UI·Airflow가 호출, 기본)
#   run <episode> …   SAM3 → 추적 → 3D 복원 → MANO → NAS 저장을 한 번에 실행 (kaist-run --help)
#   check             GPU·모델·환경 자체 점검 (추론은 하지 않음)
#   shell             bash
set -euo pipefail
W="$KAIST_WORKER_ROOT"
SCRIPTS="$KAIST_REV_ROOT/project/00_scripts"
: "${PC_ID:=pc1}" "${NAS_MOUNT:=/mnt/kaist-data}"
: "${RAW_ROOT:=$NAS_MOUNT/rgbd_raw}" "${RESULTS_SUBDIR:=results/$PC_ID}"
: "${PROJECT_DIR:?PROJECT_DIR(로컬 작업 폴더)를 지정하세요}" "${WORKER_PORT:=8090}"

# 1) Worker 코드는 이미지에서, 상태(secret·jobs·등록 폴더·lock)는 마운트된 폴더에 둡니다.
mkdir -p "$W/worker/jobs" "$W/config"
cp -f /opt/kaist/worker-code/* "$W/worker/"
touch "$W/worker/gpu_4090_a.lock"
[[ -s "$W/worker/secret" ]] || (umask 077; python3 -c 'import secrets;print(secrets.token_hex(32))' > "$W/worker/secret")

# 2) NAS 확인. 컨테이너 안에서는 바인드 마운트가 항상 '마운트'로 보이므로 실제 내용으로 확인합니다.
if [[ ! -r "$RAW_ROOT" || ! -x "$RAW_ROOT" || ! -d "$NAS_MOUNT/results" ]]; then
  echo "NAS를 읽을 수 없습니다: $RAW_ROOT (호스트 NFS 마운트, .env의 NAS_GID 그룹 권한 확인. 현재 그룹: $(id -G))" >&2
  exit 1
fi
mkdir -p "$PROJECT_DIR" "$NAS_MOUNT/$RESULTS_SUBDIR"

# 3) Worker 설정 생성. 파일 이름(gpu_4090_a.json)은 Worker 코드 호환용이며 모든 PC 동일합니다.
GPU_UUID=${GPU_UUID:-$(nvidia-smi --query-gpu=uuid --format=csv,noheader | head -1)}
[[ "$GPU_UUID" == GPU-* ]] || { echo "GPU를 찾지 못했습니다 (nvidia-container-toolkit 확인)" >&2; exit 1; }
export GPU_UUID SCRIPTS PC_ID NAS_MOUNT RAW_ROOT RESULTS_SUBDIR PROJECT_DIR WORKER_PORT
python3 - "$W/config/gpu_4090_a.json" <<'PY'
import json,sys,os
e=os.environ
cfg={"scripts_dir":e["SCRIPTS"],"pipeline_root":e["KAIST_REV_ROOT"]+"/workspace","project_dir":e["PROJECT_DIR"],
 "control_python":"/usr/bin/python3","gpu_uuid":e["GPU_UUID"],"nas_mount":e["NAS_MOUNT"],
 "nas_results_subdir":e["RESULTS_SUBDIR"],
 "artifacts":["final_data.pkl","shape/matching/final_mesh.glb","hands/hand_v1/hand_reg.pkl",
  "hands/hand_v1/hand_reg_params.pkl","hands/hand_v1/hand_reg_cidx.pkl","hands/hand_v1/integrity.json",
  "hands/hand_v1/shared_shape.pkl","hands/settings.json","hands/review/mano_review.mp4","hands/review/validation.json"],
 "raw_root":e["RAW_ROOT"]}
open(sys.argv[1],"w").write(json.dumps(cfg,indent=2)+"\n")
PY

mode=${1:-worker}; shift || true
case "$mode" in
  worker)
    cd "$W/worker"
    echo "KAIST GPU Worker · $PC_ID · $GPU_UUID · 127.0.0.1:$WORKER_PORT · 작업 $PROJECT_DIR · 결과 $NAS_MOUNT/$RESULTS_SUBDIR"
    exec "$W/venv/bin/python" -m uvicorn worker_service:app --host 127.0.0.1 --port "$WORKER_PORT" ;;
  run)   exec python3 /opt/kaist/kaist_run.py "$@" ;;
  check) exec python3 /opt/kaist/selfcheck.py "$@" ;;
  shell) exec bash "$@" ;;
  *)     exec "$mode" "$@" ;;
esac
