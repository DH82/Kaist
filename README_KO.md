# KAIST 단일 PC 스택

GPU PC 한 대가 **작업 화면(UI)·작업 순서 관리(Airflow)·GPU 연산**을 모두 맡고, NAS는 **데이터 보관소**로만 씁니다.

```
[이 GPU PC]
  ├─ Docker:  UI :18083 ─ Airflow :18082 ─ PostgreSQL :15432(내부)
  │                 │
  │                 ▼ http://127.0.0.1:8090 (터널 없음)
  ├─ GPU Worker (systemd: kaist-worker)
  │     1) NAS RAW 읽기 → 로컬 01_raw로 복사
  │     2) 로컬 SSD에서 SAM3 → 추적 → 3D 복원 → MANO
  │     3) 최종 결과만 NAS results/<pc>/ 로 업로드
  └─ /mnt/kaist-data  ← NFS 마운트
           │
[NAS]  /volume1/kaist-data   (rgbd_raw/, results/pc1..pc6/)
```

PC마다 이 스택을 따로 실행합니다. PC끼리, 또는 NAS와 서로 지휘하지 않습니다.

## 폴더 구성

| 경로 | 내용 |
|---|---|
| `compose.yaml`, `Dockerfile` | UI·Airflow·PostgreSQL 컨테이너 |
| `setup.sh` | 설정 생성·검사·빌드·실행 (여러 번 실행해도 안전). `--native`는 Docker 없이 실행 |
| `native/run.sh` | Docker 없이 실행할 때 systemd 사용자 서비스가 호출하는 실행 스크립트 |
| `install_docker.sh` | Ubuntu에 Docker·NVIDIA Container Toolkit 설치 (한 번만, sudo) |
| `gpu/` | **GPU 파이프라인 컨테이너** (SAM3 → 추적 → 3D 복원 → HaMBA·MANO → NAS 저장). `setup_gpu.sh`, `kaist-run` |
| `code/` | UI(`platform/`)와 Airflow DAG(`dags/`) — NAS 버전과 동일한 코드 |
| `worker/install_worker.sh` | **새 PC용** GPU Worker 설치 (systemd 등록) |
| `intake/upload_episode.py` | 촬영 노트북 → NAS RAW 전송 도구 |
| `pack.sh` | 다른 PC로 옮길 tar.gz 생성 (비밀값·DB 제외) |
| `.env`, `secrets/`, `state/`, `intake_state/`, `config/workers.json`, `gpu_state/` | **이 PC 전용** — 자동 생성, 복사하지 않음 |

## 이 PC(이미 Worker가 있는 PC)에 설치

```bash
cd ~/kaist-pc-stack
./install_docker.sh          # 처음 한 번. 끝나면 로그아웃·로그인 (또는 newgrp docker)
./setup.sh --check           # 설정만 검사
./setup.sh                   # 빌드 + 실행
```

끝나면 화면에 접속 주소와 비밀번호가 나옵니다.

- 작업자 UI: `http://<이 PC IP>:18083` → 1단계 `operator` / `secrets/ui_password` 값 → 2단계 이름 입력
- Airflow 관리: `http://<이 PC IP>:18082` → `admin` / `state/simple_auth_manager_passwords.json.generated`

## Docker 없이 실행 (sudo가 없을 때, 현재 PC1 방식)

sudo 권한이 없어 Docker를 설치할 수 없으면 같은 코드를 `.venv`(Python 3.10 + Airflow 3.3.2)와 systemd **사용자** 서비스로 실행합니다. DB는 PostgreSQL 대신 `state/airflow.db`(SQLite)입니다.

```bash
./setup.sh --native          # .venv 생성(처음 한 번) → 설정 검사 → kaist-airflow, kaist-ui 서비스 시작
systemctl --user status kaist-airflow kaist-ui
journalctl --user -fu kaist-airflow -u kaist-ui
systemctl --user restart kaist-ui      # 코드 수정 후
```

- 부팅 후 로그인하지 않아도 자동 시작하려면 한 번 실행: `sudo loginctl enable-linger $USER`
- 나중에 Docker 방식으로 바꿀 때: `systemctl --user disable --now kaist-airflow kaist-ui` → `./install_docker.sh` → `./setup.sh`.
  UI 기록(`intake_state/`)과 비밀번호는 그대로 쓰지만, Airflow 실행 이력(SQLite)은 옮겨지지 않습니다.

## GPU 파이프라인 컨테이너 (gpu/)

파이프라인 전체를 이미지 `kaist-gpu:latest` 하나에 담습니다. 이미지는 GPU에서 검증된 기존 환경
(sam-env · phystwin-env · hamba-env, CUDA 12.8 툴체인, SAM3.1·TRELLIS·CoTracker·HaMBA·ViTPose·MANO 가중치)을
**다시 빌드하지 않고 원래 절대경로 그대로** 복사해서 만듭니다(약 45 GB). 같은 이미지에 GPU Worker도 들어 있어
UI·Airflow가 지금과 똑같이 호출합니다.

| 단계 (Airflow 작업 이름) | 하는 일 | 환경 |
|---|---|---|
| prepare_raw | NAS RAW → 로컬 `01_raw` 복사·검증, 촬영 QC, RGB-D 전처리(`03_prepared`) | phystwin-env |
| sam_settings_ready → sam3 | 프롬프트 확정 → SAM3.1 4캠 물체·손 분할(`04_masks/pending`) | sam-env |
| mask_approved | **사람 검수**: UI 검수 화면에서 마스크 승인 (자동 승인 없음) | — |
| preflight → track | CoTracker3 물체·손 4캠 2D 추적 + 자동 QC (`05_tracking`) | phystwin-env |
| tracking_approved | 자동 QC 통과 시 자동 승인, 예외 구간이 있으면 **사람 검수** | — |
| prepare_reconstruction → reconstruct | TRELLIS 형상 생성, 점군 융합·필터, 3D 추적, 메시 정합, PhysTwin 입력 (`06_reconstruction`) | phystwin-env + xvfb |
| hands → validate | HaMBA 4캠 손 추정 → 시간 연속 MANO 피팅 → 검수 영상, 결과 검사 | hamba-env + EGL |
| publish | 지정 결과 10개만 NAS `results/<pc>/<에피소드>/<실행키>/`에 해시 검증 후 저장 | — |

```bash
./install_docker.sh               # 처음 한 번 (sudo). 끝나면 다시 로그인
gpu/setup_gpu.sh                  # 빌드(처음 20~40분) → 자체 점검 → 실행. 기존 Worker가 있으면 시험 포트 8091
gpu/setup_gpu.sh --switch         # 호스트 Worker(systemd) 끄고 컨테이너로 전환 (sudo) — UI·Airflow도 자동 연결
gpu/setup_gpu.sh --rollback       # 호스트 Worker로 되돌리기 (sudo)

gpu/kaist-run ep_20261112_062904 --prompt "white towel"   # 한 번에 실행 (검수 대기에서 멈춤)
gpu/kaist-run ep_20261112_062904 --resume --wait          # 승인 후 이어서 / 승인될 때까지 대기
docker compose --profile gpu run --rm gpu-worker check    # 자체 점검만
```

- 상태(작업 기록·토큰·GPU lock)는 `gpu_state/`, 작업 파일은 `GPU_PROJECT_DIR`(로컬 SSD), 결과는 NAS에 남습니다. 컨테이너를 지워도 사라지지 않습니다.
- `kaist-run`으로 시작한 에피소드도 UI 검수 화면 목록에 나옵니다(작업 폴더 기준). 다만 Airflow 실행 목록에는 나오지 않습니다.
- 새 PC: 이 PC에서 `docker save kaist-gpu:latest | zstd -T0 > kaist-gpu.tar.zst` → 새 PC에서 `zstd -dc kaist-gpu.tar.zst | docker load`
  → `.env`에 `PC_ID=pc2` → `gpu/setup_gpu.sh --no-build`. 새 PC에는 파이프라인·모델 설치가 필요 없습니다.
- 파이프라인 코드(00_scripts)를 고친 뒤에는 `gpu/setup_gpu.sh`로 다시 빌드합니다(바뀐 층만 다시 만듭니다).

## 새 GPU PC에 설치

1. **파이프라인 준비.** KAIST 파이프라인(00_scripts, workspace, 모델, Python 환경)을 기존 PC와 같은 버전으로 설치합니다. 이 패키지에는 GPU 모델 환경이 들어 있지 않습니다.
2. **NAS 마운트.** DSM → 공유 폴더 `kaist-data` → NFS 권한에 새 PC IP를 추가한 뒤:
   ```bash
   sudo mkdir -p /mnt/kaist-data
   echo '10.20.32.24:/volume1/kaist-data /mnt/kaist-data nfs vers=3,_netdev,nofail,x-systemd.automount 0 0' | sudo tee -a /etc/fstab
   sudo systemctl daemon-reload && sudo mount /mnt/kaist-data
   ```
3. **배포 파일 복사.** 기존 PC에서 `./pack.sh` → 생성된 `kaist-pc-stack-YYYYMMDD.tar.gz`를 새 PC 홈에 풉니다.
4. **Worker 설치.**
   ```bash
   cd ~/kaist-pc-stack/worker
   PC_ID=pc2 \
   SCRIPTS_DIR="$HOME/Kaist pipeline revising/02_current/project/00_scripts" \
   PIPELINE_ROOT="$HOME/Kaist pipeline revising/02_current/workspace" \
   CONTROL_PYTHON=/usr/bin/python3 \
   ./install_worker.sh
   ```
   - 작업 폴더는 로컬 `~/kaist_work/pc2`, 결과는 NAS `results/pc2`로 자동 지정됩니다.
   - **`PC_ID`는 PC마다 반드시 다르게** 지정하세요. 같으면 결과 폴더가 섞입니다.
5. **스택 실행.** `.env.example`의 `WORKER_SECRET_FILE`을 4번 출력값(`~/kaist-worker/worker/secret`)으로 바꾼 뒤:
   ```bash
   cd ~/kaist-pc-stack
   ./install_docker.sh
   ./setup.sh
   ```

## 운영

```bash
docker compose ps                      # 상태
docker compose logs -f airflow ui      # 로그
docker compose restart ui              # UI만 재시작
docker compose down                    # 중지 (데이터 유지)
systemctl status kaist-worker          # GPU Worker
```

- 코드 수정 후: `docker compose up -d --build`
- 백업 대상: `.env`, `secrets/`, `state/`, `intake_state/catalog.sqlite`, Postgres(`docker compose exec postgres pg_dump -U airflow -p 15432 -h 127.0.0.1 airflow > airflow.sql`)
- `docker compose down -v`는 Airflow 실행 이력 DB를 지웁니다. 업데이트 때 쓰지 마세요.

## 주의

- **같은 Worker를 두 곳에서 지휘하지 마세요.** NAS의 기존 UI·Airflow가 아직 이 PC Worker에 SSH 터널로 연결되어 있다면, 이 PC 스택을 쓰는 동안 NAS 쪽 연결을 끊어야 합니다. NAS를 건드리지 않으려면 이 PC의 `~/.ssh/authorized_keys`에서 `admin123@SynologyNAS` 줄을 제거(백업 후)하면 됩니다.
- Worker의 `project_dir`은 로컬 SSD에 두세요. NAS 안에 있으면 모든 중간 파일이 네트워크를 거칩니다.
- SimpleAuthManager와 이름 로그인은 내부망 시험용 인증입니다. 외부에 포트를 열지 마세요.
- RAW 목록은 `NAS_RAW_HOST_PATH` 바로 아래의 `ep_*` 폴더입니다.
