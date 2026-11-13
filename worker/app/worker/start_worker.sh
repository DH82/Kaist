#!/bin/bash
set -euo pipefail
WORKER_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(dirname "$WORKER_DIR")"
export KAIST_AIRFLOW_CONFIG_DIR="$ROOT_DIR/config"
cd "$WORKER_DIR"
exec "$ROOT_DIR/venv/bin/python" -m uvicorn worker_service:app --host 127.0.0.1 --port 8090
