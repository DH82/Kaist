#!/usr/bin/env bash
# 다른 PC로 옮길 배포 파일을 만듭니다. 이 PC의 비밀값·DB·상태(.env, secrets/, state/, intake_state/)는 제외합니다.
set -euo pipefail
cd "$(dirname "$0")/.."
out="kaist-pc-stack-$(date +%Y%m%d).tar.gz"
tar czf "$out" --exclude='kaist-pc-stack/.env' --exclude='kaist-pc-stack/secrets' \
  --exclude='kaist-pc-stack/state' --exclude='kaist-pc-stack/intake_state' \
  --exclude='kaist-pc-stack/config/workers.json' --exclude='kaist-pc-stack/.venv' --exclude='kaist-pc-stack/gpu_state' --exclude='__pycache__' kaist-pc-stack
echo "생성: $(pwd)/$out"
