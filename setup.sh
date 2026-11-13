#!/usr/bin/env bash
# KAIST 단일 PC 스택 설치·갱신 스크립트. 여러 번 실행해도 안전합니다.
#   ./setup.sh           설정 확인 → 이미지 빌드 → 실행
#   ./setup.sh --check   설정만 확인하고 컨테이너는 건드리지 않음
#   ./setup.sh --native  Docker 없이 실행 (.venv + systemd 사용자 서비스, SQLite DB)
#                        sudo가 없어 Docker를 설치할 수 없을 때 쓰는 임시 방식입니다.
set -euo pipefail
cd "$(dirname "$0")"
CHECK_ONLY=0; NATIVE=0
for a in "$@"; do case "$a" in --check) CHECK_ONLY=1;; --native) NATIVE=1;; *) echo "알 수 없는 옵션: $a" >&2; exit 2;; esac; done

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

echo "[1/6] 필수 프로그램"
if [[ $NATIVE == 1 ]]; then
  PY310=${PY310:-$(readlink -f /home/cw-wfm/Desktop/worker1/airflow/venv/bin/python3.10 2>/dev/null || command -v python3.10 || true)}
  if [[ ! -x .venv/bin/airflow ]]; then
    [[ -x "$PY310" ]] || die "Python 3.10이 필요합니다. PY310=/경로/python3.10 ./setup.sh --native 로 지정하세요."
    "$PY310" -m venv .venv
    .venv/bin/pip install -q --upgrade pip
    .venv/bin/pip install -q "apache-airflow==3.3.2" \
      --constraint https://raw.githubusercontent.com/apache/airflow/constraints-3.3.2/constraints-3.10.txt
  fi
  command -v openssl >/dev/null || die "openssl이 필요합니다."
  command -v ffmpeg >/dev/null || warn "ffmpeg가 없습니다. 영상 미리보기가 동작하지 않을 수 있습니다."
  ok "Airflow $(.venv/bin/airflow version 2>/dev/null | tail -1) (.venv, Docker 없음)"
else
command -v docker >/dev/null || die "docker가 없습니다. README_KO.md의 'Docker 설치'를 먼저 진행하세요."
docker compose version >/dev/null 2>&1 || die "docker compose(v2)가 없습니다."
docker info >/dev/null 2>&1 || die "docker 권한이 없습니다. 'sudo usermod -aG docker $USER' 후 다시 로그인하세요."
command -v openssl >/dev/null || die "openssl이 필요합니다."
ok "docker $(docker version --format '{{.Server.Version}}'), compose $(docker compose version --short)"
fi

echo "[2/6] .env"
if [[ ! -f .env ]]; then cp .env.example .env; ok ".env 생성"; fi
chmod 600 .env
NAS_MOUNT=$(get_env NAS_MOUNT)
ip_guess=$(ip -4 route get "$(findmnt -no SOURCE "$NAS_MOUNT" 2>/dev/null | cut -d: -f1 || echo 1.1.1.1)" 2>/dev/null | grep -oP 'src \K[\d.]+' || hostname -I | awk '{print $1}')
gpu_name=$(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | head -1 | sed 's/NVIDIA GeForce //')
fill HOST_UID "$(id -u)"
fill HOST_IP "$ip_guess"
fill PC_LABEL "$(hostname) · ${gpu_name:-GPU}"
fill POSTGRES_PASSWORD "$(openssl rand -hex 32)"
fill AIRFLOW_JWT_SECRET "$(openssl rand -hex 32)"
HOST_IP=$(get_env HOST_IP); UI_PORT=$(get_env UI_PORT); AIRFLOW_PORT=$(get_env AIRFLOW_PORT)
fill KAIST_UI_ALLOWED_HOSTS "$HOST_IP:$UI_PORT,127.0.0.1:$UI_PORT,localhost:$UI_PORT,$(hostname):$UI_PORT"
fill KAIST_AIRFLOW_PUBLIC_URL "http://$HOST_IP:$AIRFLOW_PORT"
ok "이 PC 주소 $HOST_IP · 이름 '$(get_env PC_LABEL)'"

echo "[3/6] NAS 저장소"
mountpoint -q "$NAS_MOUNT" || die "$NAS_MOUNT 가 마운트되어 있지 않습니다. README_KO.md의 'NAS 마운트'를 확인하세요."
RAW=$(get_env NAS_RAW_HOST_PATH)
[[ -d "$RAW" && -r "$RAW" ]] || die "RAW 폴더를 읽을 수 없습니다: $RAW"
ok "$NAS_MOUNT 마운트됨 ($(findmnt -no SOURCE "$NAS_MOUNT")), RAW $(find "$RAW" -maxdepth 1 -name 'ep_*' | wc -l)개"

echo "[4/6] GPU Worker"
SECRET=$(get_env WORKER_SECRET_FILE); WURL=$(get_env WORKER_URL)
[[ -r "$SECRET" ]] || die "Worker 토큰 파일을 읽을 수 없습니다: $SECRET (worker/install_worker.sh로 Worker를 먼저 설치하세요)"
info=$(curl -s -m 10 -H "Authorization: Bearer $(cat "$SECRET")" "$WURL/worker/info" || true)
[[ -n "$info" ]] || die "Worker($WURL)에 연결할 수 없습니다. 'systemctl status kaist-worker'를 확인하세요."
python3 - "$info" "$NAS_MOUNT" <<'PY' || die "Worker 응답을 해석하지 못했습니다: $info"
import json,sys
d=json.loads(sys.argv[1]);nas=sys.argv[2]
print(f"  \033[32m✔\033[0m Worker 응답: 작업폴더 {d['project_dir']} · RAW {d['raw_root']} · NAS 연결 {d['nas_ready']}")
if d['project_dir'].startswith(nas.rstrip('/')+'/'):
    print("  \033[33m!\033[0m 작업 폴더가 NAS 안에 있습니다. 로컬 SSD로 옮기면 네트워크 I/O가 줄어듭니다 (README 참고).")
if not d['nas_ready']:
    print("  \033[33m!\033[0m Worker가 NAS를 마운트로 인식하지 않습니다. 결과가 로컬에만 저장됩니다.")
PY

echo "[5/6] 로컬 상태 폴더"
mkdir -p state intake_state secrets config
[[ -s secrets/ui_password ]] || { openssl rand -hex 12 > secrets/ui_password; ok "UI 접속 비밀번호 생성"; }
chmod 700 secrets; chmod 600 secrets/ui_password
python3 - "$(get_env PC_LABEL)" "$WURL" <<'PY'
import json,sys,pathlib
label,url=sys.argv[1],sys.argv[2]
# 이 PC 하나만 등록합니다. id/dag_id/pool은 코드 호환을 위해 PC1 이름을 유지합니다.
reg={'workers':[{'id':'gpu_4090_a','label':label,'enabled':True,'dag_id':'kaist_direct_pipeline_4090',
     'pool':'gpu_4090_a','env_prefix':'KAIST_PC1','url':url,'review_dag':'kaist_revising_gpu_4090_a'}]}
pathlib.Path('config/workers.json').write_text(json.dumps(reg,ensure_ascii=False,indent=2)+'\n')
PY
ok "config/workers.json (이 PC 1대 등록)"
[[ $NATIVE == 1 ]] || { docker compose config --quiet && ok "compose 설정 검사 통과"; }
[[ $CHECK_ONLY == 1 ]] && { echo "검사만 수행했습니다."; exit 0; }

if [[ $NATIVE == 1 ]]; then
  echo "[6/6] 실행 (systemd 사용자 서비스)"
  if docker compose ps -q 2>/dev/null | grep -q .; then die "Docker 스택이 실행 중입니다. 'docker compose down' 후 다시 실행하세요."; fi
  chmod +x native/run.sh
  unit_dir="$HOME/.config/systemd/user"; mkdir -p "$unit_dir"
  for svc in airflow ui; do
    after=""; [[ $svc == ui ]] && after=$'After=kaist-airflow.service\nWants=kaist-airflow.service'
    cat > "$unit_dir/kaist-$svc.service" <<UNIT
[Unit]
Description=KAIST $svc (native, $(pwd))
$after

[Service]
ExecStart=$(pwd)/native/run.sh $svc
Restart=always
RestartSec=10
TimeoutStopSec=60

[Install]
WantedBy=default.target
UNIT
  done
  systemctl --user daemon-reload
  systemctl --user enable kaist-airflow.service kaist-ui.service >/dev/null
  systemctl --user restart kaist-airflow.service kaist-ui.service
  ok "kaist-airflow, kaist-ui 서비스 등록·시작"
  if [[ "$(loginctl show-user "$USER" -p Linger --value 2>/dev/null)" != yes ]]; then
    warn "로그아웃하거나 재부팅 후 로그인 전에는 서비스가 멈춥니다. 부팅 시 자동 시작: sudo loginctl enable-linger $USER"
  fi
else
echo "[6/6] 빌드·실행"
if systemctl --user is-active -q kaist-airflow.service 2>/dev/null; then
  die "Docker 없이 실행한 스택이 동작 중입니다. 'systemctl --user disable --now kaist-airflow kaist-ui' 후 다시 실행하세요."
fi
docker compose build
docker compose up -d
fi
printf '  Airflow 기동 대기'
for _ in $(seq 1 60); do
  curl -s -o /dev/null -m 3 "http://127.0.0.1:$AIRFLOW_PORT/api/v2/version" && break
  printf '.'; sleep 5
done; echo
for _ in $(seq 1 24); do [[ -s state/simple_auth_manager_passwords.json.generated ]] && break; sleep 5; done
if [[ $NATIVE == 1 ]]; then systemctl --user --no-pager --lines=0 status kaist-airflow kaist-ui | grep -E '●|Active'; LOGS="journalctl --user -fu kaist-airflow -u kaist-ui"
else docker compose ps; LOGS="docker compose logs -f airflow ui"; fi
cat <<EOF

완료되었습니다.
  작업자 UI     http://$HOST_IP:$UI_PORT
                1단계 로그인: operator / $(cat secrets/ui_password)
  Airflow 관리  http://$HOST_IP:$AIRFLOW_PORT
                admin 비밀번호: $(pwd)/state/simple_auth_manager_passwords.json.generated
  로그          $LOGS
EOF
