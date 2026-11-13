# NAS 중심 스택 → PC별 스택 전환 정리

작성일: 2026-11-13
비교 대상

- 이전: `~/Downloads/kaist` — Synology NAS에서 UI·Airflow·DB를 실행하던 스택 사본 (`INSTALL_KO.md` 기준)
- 현재: `~/kaist-pc-stack` — 각 GPU PC가 자기 UI·Airflow·Worker를 실행하는 스택 (`README_KO.md` 기준)

---

## 1. 한눈에 보기

| 항목 | 이전 (`Downloads/kaist`) | 현재 (`kaist-pc-stack`) |
|---|---|---|
| UI·Airflow·DB 위치 | NAS 1대 (Container Manager) | **각 GPU PC** |
| NAS 역할 | 지휘(UI·Airflow) + 저장 | **저장소만** (`/volume1/kaist-data`, NFS) |
| Worker 연결 | NAS → PC: SSH 터널 컨테이너(`tunnel`, `tunnel_pc2~6`) | PC 안에서 `http://127.0.0.1:8090` 직접 호출, **터널 없음** |
| 등록 GPU PC | `workers.json`에 PC1~PC6 (포트 18084~18089, PC3~6 미연결) | `config/workers.json`에 **이 PC 1대만** (setup.sh가 생성) |
| Docker 네트워크 | bridge + 포트 매핑(`127.0.0.1:18082/18083`) | **host 네트워크** (`network_mode: host`) |
| DB | PostgreSQL, `./postgres` 바인드 폴더 | PostgreSQL, 이름 있는 볼륨 `pgdata` (127.0.0.1:15432) / native 모드는 **SQLite** |
| 설치 방식 | `.env` 손으로 작성 → `docker compose up` | `./setup.sh` (자동 설정·검사·실행), `./setup.sh --native` (Docker 없이) |
| RAW 경로 | NAS `/volume1/kaist/raw` | PC의 NFS 마운트 `/mnt/kaist-data/rgbd_raw` |
| 작업 폴더 | Worker 설정의 기존 `project_dir` | 로컬 SSD `~/kaist_work/pcN` |
| 결과 저장 | `results/pc1` | NAS `results/<PC_ID>/` (PC마다 분리) |
| GPU 파이프라인 | PC 호스트의 Python 환경 | 호스트 Worker 유지 + **GPU 컨테이너(`gpu/`) 신규** |

```
[이전]                                   [현재]
NAS: UI ─ Airflow ─ Postgres             GPU PC (각자)
      └ tunnel ──SSH──▶ PC1 Worker:8090    ├ UI:18083 ─ Airflow:18082 ─ Postgres:15432
      └ tunnel_pc2..6 ─▶ PC2..6            ├ Worker 127.0.0.1:8090 (systemd 또는 컨테이너)
                                           └ /mnt/kaist-data ◀─NFS─ NAS (저장만)
```

---

## 2. 바뀌지 않은 것 (코드 동일)

아래 파일은 두 폴더에서 **바이트 단위로 같습니다**.

- `Dockerfile` (Airflow 3.3.2 이미지)
- `code/dags/` : `kaist_direct.py`, `workers.py`, `direct_worker.py`
- `code/platform/` : `platform_app.py`, `access.py`, `finder_raw.py`, `intake.py`, `index.html`, `reviews.js`, `intake.js`, `outputs.js`, `session.js`, `mask-editor.js`
- `intake/upload_episode.py`, `intake/README_KO.md`
- Worker 쪽 `runner.py`, `kaist_runtime.py`

즉 UI·DAG 동작은 그대로이고, **배치(어디서 실행하는가)와 연결 방식만** 바뀌었습니다.
`workers.py`가 원래 `KAIST_WORKERS_FILE` 환경변수를 지원하므로, 레지스트리 파일 위치만 바꿔 코드 수정 없이 전환했습니다.

---

## 3. 파일별 변경 내용

### 3.1 `compose.yaml` (전면 재작성)

**삭제**
- `tunnel` 서비스 (NAS → PC1 SSH `-L 0.0.0.0:18084:127.0.0.1:8090`)
- `tunnel_pc2` ~ `tunnel_pc6` 서비스 및 프로필 `pc2`~`pc6`
- `KAIST_PC2_URL`~`KAIST_PC6_URL`, `KAIST_PC*_TOKEN_FILE` 환경변수
- `secrets/worker_key`, `known_hosts`, `pc2_worker_key`, `worker_tokens/` 마운트
- `ports:` 매핑, `./postgres` 바인드 폴더, `ui`의 `group_add: ["65536"]`

**추가·변경**
- `name: kaist-pc`, 모든 서비스 `network_mode: host`
- Airflow·UI를 `user: "${HOST_UID}:0"`로 실행 → `state/`, `intake_state/`, Worker secret을 sudo 없이 읽고 씀
- Postgres: `port=${POSTGRES_PORT}`, `listen_addresses=127.0.0.1`, 볼륨 `pgdata`
- Airflow: `AIRFLOW__API__HOST=${AIRFLOW_BIND}`, 포트 변수화, DB 주소 `127.0.0.1:15432`, 실행 API `http://127.0.0.1:18082/execution/`
- host 네트워크 충돌 방지용 로그 포트 `18793`/`18794`
- `KAIST_WORKERS_FILE=/opt/kaist-config/workers.json` + `./config` 마운트
- `KAIST_WORKER_URL=${WORKER_URL}` (기본 `http://127.0.0.1:8090`), 토큰은 `${WORKER_SECRET_FILE}`를 직접 마운트
- UI: `--host $UI_BIND --port $UI_PORT`, `depends_on: airflow`, `HOME=/tmp`
- **신규 `gpu-worker` 서비스** (프로필 `gpu`) — 4절 참고

### 3.2 `.env.example`

| 이전 | 현재 |
|---|---|
| `POSTGRES_PASSWORD`, `AIRFLOW_JWT_SECRET` 직접 입력 | `setup.sh`가 `openssl rand -hex 32`로 자동 생성 |
| `WORKER_SSH_TARGET=cw-wfm@192.168.200.17` | 삭제 → `WORKER_URL`, `WORKER_SECRET_FILE` |
| `PC2_SSH_TARGET` ~ `PC6_SSH_TARGET` | 삭제 |
| `KAIST_UI_ALLOWED_HOSTS`, `KAIST_AIRFLOW_PUBLIC_URL` 예시 도메인 | `HOST_IP`·포트로 자동 생성 |
| `NAS_RAW_HOST_PATH=/volume1/kaist/raw` | `NAS_MOUNT=/mnt/kaist-data`, `NAS_RAW_HOST_PATH=/mnt/kaist-data/rgbd_raw` |
| — | `PC_LABEL`, `UI_PORT`, `AIRFLOW_PORT`, `POSTGRES_PORT`, `UI_BIND`, `AIRFLOW_BIND`, `HOST_UID`, `HOST_IP` |

GPU 컨테이너용으로 `.env`에 추가로 쓰는 값: `PC_ID`, `GPU_PROJECT_DIR`, `GPU_STATE_DIR`, `GPU_LOCK_FILE`, `GPU_WORKER_PORT`, `KAIST_DEP_ROOT`, `KAIST_REV_ROOT`, `NAS_GID`.

### 3.3 Worker 레지스트리

- 이전: `code/dags/workers.json` — PC1(18084, 터널) + PC2(18085) + PC3~6(비활성)
- 현재: `config/workers.json` — 이 PC 1대만, `url: http://127.0.0.1:8090`
  - `setup.sh`가 매번 생성합니다. 라벨은 `PC_LABEL`(기본 `호스트이름 · GPU이름`).
  - `id/dag_id/pool/env_prefix`는 코드 호환을 위해 PC1 이름(`gpu_4090_a`, `kaist_direct_pipeline_4090`, `KAIST_PC1`)을 **모든 PC에서 그대로** 씁니다.

### 3.4 Worker 코드

- 이전: `worker_update/` — NAS에서 GPU PC로 옮겨 적용할 파일 3개 (`worker_service.py`, `runner.py`, `kaist_runtime.py`)
- 현재: `worker/app/` — 실제 PC1 호스트 Worker(`~/Desktop/worker1/airflow/worker`)와 동일한 전체 세트
  - `worker/`: `worker_service.py`, `runner.py`, `finder_raw.py`, `output_browser.py`, `output_roots.py`, `mask_editor.py`, `start_worker.sh`
  - `dags/kaist_runtime.py`, `review/` (`index.html`, `queue.html`, `tracking.html`, `review_backend.py`)
- `worker_service.py` 차이: `GET /worker/intake/fingerprint` 엔드포인트 추가 (RAW 에피소드 manifest의 SHA-256 반환)
- 새 PC용 설치 스크립트 `worker/install_worker.sh` 추가 → `~/kaist-worker`에 설치, systemd `kaist-worker` 등록, NAS 마운트 확인(`RequiresMountsFor`), 작업 폴더 `~/kaist_work/<PC_ID>`, 결과 `results/<PC_ID>`

### 3.5 새로 생긴 스크립트

| 파일 | 역할 |
|---|---|
| `setup.sh` | [1] 필수 프로그램 → [2] `.env` 자동 채움 → [3] NAS 마운트·RAW 확인 → [4] Worker `/worker/info` 응답 확인 → [5] `ui_password`·`workers.json` 생성 → [6] 빌드·실행. `--check`, `--native` 옵션 |
| `native/run.sh` | Docker 없이 `airflow standalone` / `uvicorn` 실행 (compose와 같은 설정을 호스트 경로로, DB는 `state/airflow.db` SQLite) |
| `install_docker.sh` | Docker·Compose v2·NVIDIA Container Toolkit 설치, docker 그룹 추가 |
| `pack.sh` | 다른 PC로 옮길 `kaist-pc-stack-YYYYMMDD.tar.gz` 생성 (`.env`, `secrets/`, `state/`, `intake_state/`, `config/workers.json`, `.venv`, `gpu_state/` 제외) |
| `gpu/` | GPU 파이프라인 컨테이너 (4절) |
| `.gitignore`, `.dockerignore` | 비밀값·상태·venv 제외 |

### 3.6 문서

- `INSTALL_KO.md` (NAS 설치, Mac → NAS 전환 절차) → `README_KO.md` (PC별 설치, native 실행, GPU 컨테이너, 새 PC 추가, 운영)

### 3.7 정리된 파일·폴더 (현재 스택에 없음)

- `secrets/worker_key*`, `known_hosts`, `pc2_*` (SSH 터널용 키)
- `worker_tokens/` (PC별 Worker 토큰)
- `postgres/` 바인드 폴더 (→ 볼륨 `pgdata` 또는 SQLite)
- `.jobs-perf/` (성능 실험 파일), 각종 `*.bak`, `compose.yaml.before-*`, `.DS_Store`

---

## 4. GPU 파이프라인 컨테이너 (`gpu/`, 신규)

이전 구성에는 없던 부분입니다. 파이프라인 전체(SAM3 → CoTracker 추적 → TRELLIS 3D 복원 → HaMBA·MANO → NAS 저장)와 Worker를 이미지 `kaist-gpu:latest` 하나에 담았습니다.

- **환경을 다시 빌드하지 않고 원래 절대경로 그대로 복사** (`KAIST_DEP_ROOT`, `KAIST_REV_ROOT`). venv·editable 설치·`config.env`가 절대경로를 쓰기 때문에 의도적으로 경로를 유지했습니다.
- 파일: `Dockerfile`, `entrypoint.sh` (`worker`/`run`/`check`/`shell`), `kaist_run.py` (`kaist-run` 한 번에 실행 CLI, 검수 지점에서 멈춤·`--resume`), `selfcheck.py` (단계별 환경 점검), `setup_gpu.sh` (`--switch`, `--rollback`, `--no-build`)
- 상태는 `gpu_state/worker`, 작업 파일은 `GPU_PROJECT_DIR`(로컬 SSD), 결과는 NAS
- 해결한 문제
  - Synology NFS가 PC별 그룹(`kaist-pc1`, gid 65536)으로 권한을 줌 → `group_add: ${NAS_GID}`
  - NVIDIA toolkit이 EGL vendor json을 넣지 않음 → 이미지에서 `10_nvidia.json` 생성
  - 호스트 Worker와 같은 GPU lock 파일 공유 (`GPU_LOCK_FILE`)

---

## 5. PC1 현재 상태 (2026-11-13 확인)

| 항목 | 상태 |
|---|---|
| PC1 | RTX 4090, `10.20.32.21` |
| UI·Airflow | `./setup.sh --native` 방식 (systemd 사용자 서비스 `kaist-airflow`, `kaist-ui`, SQLite) — active |
| linger | 활성화됨 (로그인 없이 부팅 후 자동 시작) |
| 호스트 Worker | systemd `kaist-worker`, `127.0.0.1:8090` — active, UI·Airflow가 이것을 사용 |
| GPU 컨테이너 | `kaist-pc-gpu-worker-1` 시험 포트 **8091**에서 실행 중 (healthy), 자체 점검 10/10. 아직 `--switch` 안 함 |
| NAS → PC1 SSH 키 | 제거됨 (NAS의 이전 스택이 이 PC Worker를 더 이상 지휘하지 않음) |
| 미완료 | GPU 컨테이너로 에피소드 전체(end-to-end) 실행 시험 |

---

## 6. 운영상 달라진 점

- **PC끼리, NAS와 서로 지휘하지 않습니다.** 각 PC의 UI에서는 그 PC의 작업만 보입니다. (이전: NAS UI 하나에서 PC1~6 선택)
- 접속 주소: `http://<각 PC IP>:18083` (UI), `:18082` (Airflow)
- `PC_ID`는 PC마다 반드시 다르게 지정 — 같으면 NAS `results/` 폴더가 섞입니다.
- 이전 Airflow 실행 이력(NAS Postgres)은 옮기지 않았습니다. Docker ↔ native 전환 시에도 Airflow 이력은 이어지지 않습니다 (`intake_state/`와 비밀번호는 유지).
- 새 PC 추가 절차: NAS NFS 권한 추가·마운트 → `pack.sh`로 배포 → `install_worker.sh` (또는 `docker load` 후 `gpu/setup_gpu.sh --no-build`) → `setup.sh`
