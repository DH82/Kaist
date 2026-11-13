#!/usr/bin/env bash
# 새 GPU PC에 KAIST GPU Worker(포트 8090)를 설치합니다. 이미 Worker가 있는 PC에서는 실행하지 마세요.
#
# 전제: 이 PC에 KAIST 파이프라인(scripts_dir / pipeline_root / 모델 / control_python 환경)이
#       이미 설치되어 있고, NAS가 NFS로 마운트되어 있어야 합니다.
#
# 사용 예:
#   PC_ID=pc2 \
#   SCRIPTS_DIR="/home/user/Kaist pipeline revising/02_current/project/00_scripts" \
#   PIPELINE_ROOT="/home/user/Kaist pipeline revising/02_current/workspace" \
#   CONTROL_PYTHON=/usr/bin/python3 \
#   ./install_worker.sh
#
# 선택 변수(기본값): WORKER_HOME=~/kaist-worker  NAS_MOUNT=/mnt/kaist-data
#   RAW_ROOT=$NAS_MOUNT/rgbd_raw  PROJECT_DIR=~/kaist_work/$PC_ID  RESULTS_SUBDIR=results/$PC_ID
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
die(){ echo "✘ $*" >&2; exit 1; }

: "${PC_ID:?PC_ID를 지정하세요 (예: pc2). 결과는 NAS results/<PC_ID>에 저장됩니다.}"
: "${SCRIPTS_DIR:?SCRIPTS_DIR (파이프라인 00_scripts 경로)를 지정하세요.}"
: "${PIPELINE_ROOT:?PIPELINE_ROOT (파이프라인 workspace 경로)를 지정하세요.}"
CONTROL_PYTHON=${CONTROL_PYTHON:-/usr/bin/python3}
WORKER_HOME=${WORKER_HOME:-$HOME/kaist-worker}
NAS_MOUNT=${NAS_MOUNT:-/mnt/kaist-data}
RAW_ROOT=${RAW_ROOT:-$NAS_MOUNT/rgbd_raw}
PROJECT_DIR=${PROJECT_DIR:-$HOME/kaist_work/$PC_ID}
RESULTS_SUBDIR=${RESULTS_SUBDIR:-results/$PC_ID}

[[ -d "$SCRIPTS_DIR" ]] || die "SCRIPTS_DIR 없음: $SCRIPTS_DIR"
[[ -f "$SCRIPTS_DIR/runtime.local.json" ]] || die "$SCRIPTS_DIR/runtime.local.json 이 없습니다."
[[ -d "$PIPELINE_ROOT" ]] || die "PIPELINE_ROOT 없음: $PIPELINE_ROOT"
[[ -x "$CONTROL_PYTHON" ]] || die "CONTROL_PYTHON 실행 불가: $CONTROL_PYTHON"
mountpoint -q "$NAS_MOUNT" || die "NAS가 $NAS_MOUNT 에 마운트되어 있지 않습니다."
[[ -d "$RAW_ROOT" ]] || die "RAW 폴더 없음: $RAW_ROOT"
GPU_UUID=${GPU_UUID:-$(nvidia-smi --query-gpu=uuid --format=csv,noheader | head -1)}
[[ "$GPU_UUID" == GPU-* ]] || die "GPU UUID를 확인하지 못했습니다."
ss -ltn | grep -q ':8090 ' && die "8090 포트를 이미 쓰는 프로세스가 있습니다 (기존 Worker?)."

echo "▶ 코드 복사 → $WORKER_HOME"
mkdir -p "$WORKER_HOME"/{worker/jobs,dags,review,config} "$PROJECT_DIR" "$NAS_MOUNT/$RESULTS_SUBDIR"
cp "$HERE"/app/worker/*.py "$HERE"/app/worker/start_worker.sh "$WORKER_HOME/worker/"
cp "$HERE"/app/dags/*.py "$WORKER_HOME/dags/"
cp "$HERE"/app/review/* "$WORKER_HOME/review/"
chmod +x "$WORKER_HOME/worker/start_worker.sh"

echo "▶ Python 환경"
[[ -x "$WORKER_HOME/venv/bin/python" ]] || python3 -m venv "$WORKER_HOME/venv"
"$WORKER_HOME/venv/bin/pip" install -q fastapi==0.136.3 uvicorn==0.52.4 httpx==0.28.1 pydantic==2.13.5

echo "▶ 토큰·설정"
if [[ ! -s "$WORKER_HOME/worker/secret" ]]; then
  (umask 077; openssl rand -hex 32 > "$WORKER_HOME/worker/secret")
fi
CFG="$WORKER_HOME/config/gpu_4090_a.json"   # 파일 이름은 Worker 내부 호환용입니다 (모든 PC 동일).
if [[ -f "$CFG" ]]; then
  echo "  기존 설정 유지: $CFG"
else
  python3 - "$CFG" <<PY
import json,sys
cfg={"scripts_dir":"$SCRIPTS_DIR","pipeline_root":"$PIPELINE_ROOT","project_dir":"$PROJECT_DIR",
 "control_python":"$CONTROL_PYTHON","gpu_uuid":"$GPU_UUID","nas_mount":"$NAS_MOUNT",
 "nas_results_subdir":"$RESULTS_SUBDIR","raw_root":"$RAW_ROOT",
 "artifacts":["final_data.pkl","shape/matching/final_mesh.glb","hands/hand_v1/hand_reg.pkl",
  "hands/hand_v1/hand_reg_params.pkl","hands/hand_v1/hand_reg_cidx.pkl","hands/hand_v1/integrity.json",
  "hands/hand_v1/shared_shape.pkl","hands/settings.json","hands/review/mano_review.mp4","hands/review/validation.json"]}
open(sys.argv[1],"w").write(json.dumps(cfg,indent=2)+"\n")
PY
  echo "  생성: $CFG"
fi

echo "▶ systemd 서비스 (sudo 필요)"
UNIT=$(mktemp)
cat > "$UNIT" <<EOF
[Unit]
Description=KAIST GPU Worker
Wants=network-online.target
After=network-online.target
RequiresMountsFor=$NAS_MOUNT

[Service]
Type=simple
User=$(id -un)
ExecStartPre=/usr/bin/mountpoint -q $NAS_MOUNT
ExecStartPre=/usr/bin/test -d $RAW_ROOT
ExecStart=/bin/bash $WORKER_HOME/worker/start_worker.sh
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF
sudo install -m 644 "$UNIT" /etc/systemd/system/kaist-worker.service
rm -f "$UNIT"
sudo systemctl daemon-reload
sudo systemctl enable --now kaist-worker
sleep 3
curl -sf -m 10 -H "Authorization: Bearer $(cat "$WORKER_HOME/worker/secret")" http://127.0.0.1:8090/worker/info >/dev/null \
  && echo "✔ Worker 실행 중 (127.0.0.1:8090)" || die "Worker 응답 없음: journalctl -u kaist-worker -n 50"

cat <<EOF

다음 단계: 스택 폴더의 .env에서 아래 값을 지정하고 ./setup.sh 를 실행하세요.
  WORKER_SECRET_FILE=$WORKER_HOME/worker/secret
  NAS_MOUNT=$NAS_MOUNT
  NAS_RAW_HOST_PATH=$RAW_ROOT
EOF
