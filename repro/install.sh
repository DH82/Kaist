#!/usr/bin/env bash
# 새 PC 설치: clone → .env 생성 → GPU 이미지 빌드(환경 자동 다운로드) → 점검 → Worker → UI·Airflow
# 기존 배포(.env 또는 8090 포트 Worker)가 있으면 덮어쓰지 않고 중단합니다.
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/.." && pwd)
cd "$ROOT"
usage='Usage: repro/install.sh PC_ID NAS_GID LOCAL_PROJECT [WEIGHTS_ROOT] [NAS_MOUNT]'
[[ $# -ge 3 ]] || { echo "$usage" >&2; exit 2; }
[[ ! -e .env ]] || { echo '기존 .env가 있습니다. 새 clone에서 설치하세요.' >&2; exit 1; }
PC=$1; NAS_GID=$2; PROJECT=$3
WEIGHTS=${4:-/home/cw-wfm/Documents/Codex/2026-10-20/https-drive-google-com-drive-folders/outputs/WFM_KAIST/workspace}
NAS=${5:-/mnt/kaist-data}
for cmd in docker python3 nvidia-smi ss; do command -v "$cmd" >/dev/null || { echo "$cmd 가 필요합니다." >&2; exit 1; }; done
docker info >/dev/null
docker compose version >/dev/null
if ss -ltn | grep -qE ':8090[[:space:]]'; then echo '8090 포트에 기존 Worker가 있습니다. 설치 중단.' >&2; exit 1; fi
python3 repro/verify_source.py
python3 repro/prepare_pc.py --pc-id "$PC" --nas "$NAS" --nas-gid "$NAS_GID" --project "$PROJECT" --weights "$WEIGHTS"
# Python 환경 3개를 인터넷에서 받아 빌드합니다 (처음에는 CUDA 확장 컴파일로 오래 걸립니다).
docker compose --profile gpu build gpu-worker
docker compose --profile gpu run --rm --no-deps gpu-worker check
docker compose --profile gpu up -d gpu-worker
for _ in $(seq 1 30); do
  if [[ -s gpu_state/worker/secret ]] && python3 - <<'PY'
from pathlib import Path
import urllib.request
req=urllib.request.Request('http://127.0.0.1:8090/worker/info',headers={'Authorization':'Bearer '+Path('gpu_state/worker/secret').read_text().strip()})
with urllib.request.urlopen(req,timeout=3) as r:assert r.status==200
PY
  then ./setup.sh; exit 0; fi
  sleep 2
done
echo 'Worker 시작 실패. docker compose --profile gpu logs gpu-worker 확인' >&2
exit 1
