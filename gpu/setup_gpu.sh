#!/usr/bin/env bash
# GPU 파이프라인 컨테이너(gpu-worker) 빌드·점검·실행. 여러 번 실행해도 안전합니다.
#
#   gpu/setup_gpu.sh             이미지 빌드 → 자체 점검 → 실행
#                                기존 호스트 Worker(systemd kaist-worker)가 8090을 쓰고 있으면
#                                컨테이너는 시험 포트 8091로 띄우고, UI·Airflow 연결은 바꾸지 않습니다.
#   gpu/setup_gpu.sh --switch    호스트 Worker를 끄고(sudo) 컨테이너가 8090을 넘겨받아 UI·Airflow를 연결
#   gpu/setup_gpu.sh --rollback  컨테이너를 끄고 호스트 Worker로 되돌림(sudo)
#   gpu/setup_gpu.sh --no-build  빌드 생략 (다른 PC에서 docker load로 이미지를 받은 경우)
set -euo pipefail
cd "$(dirname "$0")/.."
MODE=up; BUILD=1
for a in "$@"; do case "$a" in --switch) MODE=switch;; --rollback) MODE=rollback;; --no-build) BUILD=0;;
  *) echo "알 수 없는 옵션: $a" >&2; exit 2;; esac; done

ok(){ printf '  \e[32m✔\e[0m %s\n' "$*"; }
warn(){ printf '  \e[33m!\e[0m %s\n' "$*"; }
die(){ printf '  \e[31m✘\e[0m %s\n' "$*" >&2; exit 1; }
get_env(){ grep -E "^$1=" .env | tail -1 | cut -d= -f2- || true; }
set_env(){
  python3 - "$1" "$2" <<'PY'
import sys,re,pathlib
k,v=sys.argv[1],sys.argv[2];p=pathlib.Path('.env');t=p.read_text()
line=f'{k}={v}'
t=re.sub(rf'^{re.escape(k)}=.*$',lambda m:line,t,flags=re.M) if re.search(rf'^{re.escape(k)}=',t,re.M) else t.rstrip('\n')+'\n'+line+'\n'
p.write_text(t)
PY
}
fill(){ [[ -n "$(get_env "$1")" ]] || set_env "$1" "$2"; }
DC(){ docker compose --profile gpu "$@"; }

HOST_WORKER=/home/cw-wfm/Desktop/worker1/airflow   # 기존 호스트 Worker (PC1). 다른 PC에는 없을 수 있습니다.
host_worker_active(){ systemctl is-active -q kaist-worker 2>/dev/null; }
restart_stack(){
  if systemctl --user is-active -q kaist-airflow 2>/dev/null; then
    systemctl --user restart kaist-airflow kaist-ui; ok "UI·Airflow 재시작 (Docker 없이 실행 중인 스택)"
  elif docker compose ps -q airflow 2>/dev/null | grep -q .; then
    docker compose up -d airflow ui; ok "UI·Airflow 재시작 (Docker 스택)"
  else warn "실행 중인 UI·Airflow가 없습니다. ./setup.sh로 실행하세요."; fi
}
wait_worker(){
  local port=$1 secret=$2
  for _ in $(seq 1 30); do
    curl -sf -m 5 -H "Authorization: Bearer $(cat "$secret")" "http://127.0.0.1:$port/worker/info" >/dev/null && return 0
    sleep 2
  done; return 1
}

[[ -f .env ]] || die ".env가 없습니다. 먼저 ./setup.sh (또는 ./setup.sh --native)를 실행하세요."
echo "[1/5] Docker·GPU 런타임"
command -v docker >/dev/null || die "docker가 없습니다. ./install_docker.sh를 먼저 실행하세요."
docker info >/dev/null 2>&1 || die "docker 권한이 없습니다. 다시 로그인하거나 'newgrp docker' 후 실행하세요."
docker info --format '{{json .Runtimes}}' | grep -q nvidia || die "NVIDIA Container Toolkit이 없습니다. ./install_docker.sh를 다시 실행하세요."
ok "docker $(docker version --format '{{.Server.Version}}') + nvidia runtime"

echo "[2/5] 설정 (.env)"
fill PC_ID pc1
PC_ID=$(get_env PC_ID)
default_project="$HOME/kaist_work/$PC_ID"
if [[ -f "$HOST_WORKER/config/gpu_4090_a.json" ]]; then
  default_project=$(python3 -c "import json;print(json.load(open('$HOST_WORKER/config/gpu_4090_a.json'))['project_dir'])")
fi
fill GPU_PROJECT_DIR "$default_project"
fill GPU_STATE_DIR ./gpu_state
# NAS 접근 그룹: 호스트 계정의 kaist-<PC_ID> 그룹 (없으면 PC1 값 65536)
nas_gid=$(getent group "kaist-$PC_ID" | cut -d: -f3)
[[ -n "$nas_gid" ]] || { nas_gid=65536; warn "kaist-$PC_ID 그룹이 없어 NAS 그룹 65536을 씁니다. NAS 권한을 확인하세요."; }
set_env NAS_GID "$nas_gid"
STATE=$(get_env GPU_STATE_DIR); PROJECT=$(get_env GPU_PROJECT_DIR)
mkdir -p "$STATE/worker/jobs" "$PROJECT"
touch "$STATE/worker/gpu_4090_a.lock"
# 호스트 Worker가 남아 있는 동안에는 같은 GPU lock 파일을 공유해 GPU 작업이 겹치지 않게 합니다.
if [[ -f "$HOST_WORKER/worker/gpu_4090_a.lock" ]] && [[ $MODE != switch ]] && host_worker_active; then
  set_env GPU_LOCK_FILE "$HOST_WORKER/worker/gpu_4090_a.lock"
else
  set_env GPU_LOCK_FILE "$STATE/worker/gpu_4090_a.lock"
fi
if [[ $MODE == up ]]; then
  if host_worker_active && [[ "$(get_env GPU_WORKER_PORT)" != 8091 ]]; then set_env GPU_WORKER_PORT 8091; fi
  fill GPU_WORKER_PORT 8090
fi
ok "PC $PC_ID · 작업 폴더 $PROJECT · 상태 $STATE · 포트 $(get_env GPU_WORKER_PORT)"

if [[ $MODE == rollback ]]; then
  echo "[3/5] 컨테이너 중지 → 호스트 Worker 복귀"
  [[ -f "$HOST_WORKER/worker/secret" ]] || die "호스트 Worker가 없습니다: $HOST_WORKER"
  DC stop gpu-worker || true
  sudo systemctl enable --now kaist-worker
  wait_worker 8090 "$HOST_WORKER/worker/secret" || die "호스트 Worker가 응답하지 않습니다 (systemctl status kaist-worker)."
  set_env WORKER_URL http://127.0.0.1:8090
  set_env WORKER_SECRET_FILE "$HOST_WORKER/worker/secret"
  set_env GPU_WORKER_PORT 8091
  restart_stack; ok "호스트 Worker로 되돌렸습니다."; exit 0
fi

echo "[3/5] 이미지"
if [[ $BUILD == 1 ]]; then
  # 빌드 원본: 검증된 모델 환경(dep)과 파이프라인 코드(rev). 이 PC(PC1)에만 있습니다.
  fill KAIST_DEP_ROOT /home/cw-wfm/Documents/Codex/2026-10-20/https-drive-google-com-drive-folders/outputs/WFM_KAIST/workspace
  fill KAIST_REV_ROOT "/home/cw-wfm/Desktop/worker1/Kaist pipeline revising/02_current"
  for v in KAIST_DEP_ROOT KAIST_REV_ROOT; do
    d=$(get_env $v)
    [[ -d "$d" ]] || die "빌드 원본 폴더가 없습니다 ($v): $d — 다른 PC라면 이미지를 docker load 후 --no-build로 실행하세요."
  done
  DOCKER_BUILDKIT=1 DC build gpu-worker
fi
docker image inspect kaist-gpu:latest >/dev/null 2>&1 || die "kaist-gpu:latest 이미지가 없습니다."
ok "kaist-gpu:latest $(docker image inspect kaist-gpu:latest --format '{{.Size}}' | awk '{printf "%.1f GB", $1/1e9}')"

if [[ $MODE == switch ]]; then
  echo "[4/5] 호스트 Worker 중지 → 컨테이너가 8090 사용"
  if host_worker_active; then sudo systemctl disable --now kaist-worker; ok "호스트 kaist-worker 중지·자동시작 해제"; fi
  set_env GPU_WORKER_PORT 8090
  set_env GPU_LOCK_FILE "$STATE/worker/gpu_4090_a.lock"
else
  echo "[4/5] 자체 점검 (GPU·모델·NAS, 몇 분 걸릴 수 있음)"
  DC run --rm --no-deps gpu-worker check || die "자체 점검 실패. 위 ✘ 항목을 확인하세요."
fi

echo "[5/5] 실행"
DC up -d --force-recreate gpu-worker
PORT=$(get_env GPU_WORKER_PORT)
wait_worker "$PORT" "$STATE/worker/secret" || { docker compose --profile gpu logs --tail 50 gpu-worker; die "컨테이너 Worker가 응답하지 않습니다."; }
ok "컨테이너 Worker 응답: http://127.0.0.1:$PORT"
if [[ $MODE == switch ]]; then
  set_env WORKER_URL "http://127.0.0.1:$PORT"
  set_env WORKER_SECRET_FILE "$(cd "$STATE/worker" && pwd)/secret"
  restart_stack
  ok "UI·Airflow가 이제 컨테이너 Worker를 사용합니다. 되돌리기: gpu/setup_gpu.sh --rollback"
elif [[ "$PORT" != 8090 ]]; then
  warn "호스트 Worker가 8090을 쓰고 있어 시험 포트 $PORT로 실행했습니다. UI·Airflow는 아직 호스트 Worker를 사용합니다."
  warn "전환: gpu/setup_gpu.sh --switch"
fi
cat <<EOF

한 번에 실행:  gpu/kaist-run <RAW 에피소드> --prompt "물체 이름"
이어서 실행:   gpu/kaist-run <RAW 에피소드> --resume
로그:          docker compose --profile gpu logs -f gpu-worker
EOF
