#!/usr/bin/env bash
# 이 PC의 스택을 한 번에 설정·빌드·점검·실행합니다. 옵션은 repro/start_stack.py --help.
#   ./start.sh --install-docker   Ubuntu에 Docker·Compose·NVIDIA Container Toolkit 설치(처음 한 번, sudo)
set -euo pipefail
cd "$(dirname "$0")"
if [[ "${1:-}" == --install-docker ]]; then
  shift
  exec bash repro/install_docker.sh "$@"
fi
exec python3 repro/start_stack.py "$@"
