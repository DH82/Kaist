#!/usr/bin/env bash
# Docker 없이 UI·Airflow를 실행합니다 (systemd 사용자 서비스가 호출).
#   native/run.sh airflow | ui
# 설정은 compose.yaml과 같은 값을 쓰고, 컨테이너 경로 대신 호스트 경로를 넣습니다.
# DB는 PostgreSQL 대신 state/airflow.db(SQLite)를 씁니다.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
# .env는 Docker 형식(따옴표 없음, 값에 공백 가능)이라 source 대신 한 줄씩 읽습니다.
while IFS= read -r line; do
  [[ "$line" =~ ^[A-Za-z_][A-Za-z0-9_]*= ]] && export "${line%%=*}=${line#*=}"
done < .env
export PATH="$ROOT/.venv/bin:$PATH"

# 공통
export KAIST_WORKERS_FILE="$ROOT/config/workers.json"
export KAIST_WORKER_URL="${WORKER_URL:-http://127.0.0.1:8090}"
export KAIST_WORKER_TOKEN_FILE="$WORKER_SECRET_FILE"
AIRFLOW_PORT="${AIRFLOW_PORT:-18082}"

case "${1:-}" in
airflow)
  export AIRFLOW_HOME="$ROOT/state"
  export AIRFLOW__CORE__DAGS_FOLDER="$ROOT/code/dags"
  export AIRFLOW__CORE__LOAD_EXAMPLES=false
  export AIRFLOW__CORE__EXECUTOR=LocalExecutor
  export AIRFLOW__CORE__PARALLELISM=6
  export AIRFLOW__CORE__DEFAULT_TIMEZONE=Asia/Seoul
  export AIRFLOW__API__HOST="${AIRFLOW_BIND:-0.0.0.0}"
  export AIRFLOW__API__PORT="$AIRFLOW_PORT"
  export AIRFLOW__API__BASE_URL="$KAIST_AIRFLOW_PUBLIC_URL"
  export AIRFLOW__CORE__EXECUTION_API_SERVER_URL="http://127.0.0.1:$AIRFLOW_PORT/execution/"
  export AIRFLOW__DATABASE__SQL_ALCHEMY_CONN="sqlite:///$ROOT/state/airflow.db"
  export AIRFLOW__LOGGING__WORKER_LOG_SERVER_PORT=18793
  export AIRFLOW__LOGGING__TRIGGER_LOG_SERVER_PORT=18794
  export AIRFLOW__CORE__AUTH_MANAGER=airflow.api_fastapi.auth.managers.simple.simple_auth_manager.SimpleAuthManager
  export AIRFLOW__CORE__SIMPLE_AUTH_MANAGER_USERS=admin:admin
  export AIRFLOW__API_AUTH__JWT_SECRET="$AIRFLOW_JWT_SECRET"
  exec airflow standalone
  ;;
ui)
  export KAIST_UI_ALLOWED_HOSTS KAIST_AIRFLOW_PUBLIC_URL
  export KAIST_UI_USER=operator
  export KAIST_UI_PASSWORD_FILE="$ROOT/secrets/ui_password"
  export KAIST_AIRFLOW_API_URL="http://127.0.0.1:$AIRFLOW_PORT"
  export KAIST_AIRFLOW_USERS_FILE="$ROOT/state/simple_auth_manager_passwords.json.generated"
  export KAIST_GPU_TEST_ENABLED=false
  export KAIST_NAS_RAW_ROOT="$NAS_RAW_HOST_PATH"
  export KAIST_INTAKE_DB="$ROOT/intake_state/catalog.sqlite"
  exec python -m uvicorn platform_app:app --app-dir "$ROOT/code/platform" \
    --host "${UI_BIND:-0.0.0.0}" --port "${UI_PORT:-18083}"
  ;;
*) echo "사용법: $0 airflow|ui" >&2; exit 2 ;;
esac
