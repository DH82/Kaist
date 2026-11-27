# KAIST 손·물체 3D 데이터 파이프라인 (PC 스택)

GPU PC 한 대에서 **작업자 UI → Airflow → GPU Worker**를 Docker로 돌려, NAS의 4카메라 RGB-D 촬영(RAW)에서 물체·손 분할(SAM3.1), 2D 추적(CoTracker3), 3D 복원(TRELLIS), 손 복원(HaMBA·MANO)까지 만들고 결과를 NAS에 저장합니다.
PC마다 이 스택을 따로 실행하고, NAS(`/mnt/kaist-data`)는 RAW·결과·PC 간 관제 기록을 두는 저장소로만 씁니다.

## 작업 흐름

1. 작업자가 낮에 RAW마다 카메라 4장에 물체(와 손) 포인트를 찍으면 `RAW 전처리 → SAM3 → 마스크 검수 → 추적 → 추적 검수`까지 진행하고 `3D 복원 대기`로 멈춥니다. 사람의 검수 승인이 있어야 다음 단계로 갑니다.
2. 퇴근 전 **3D 복원 일괄 실행**이 에피소드마다 `3D 복원 → HaMBA·MANO → NAS 저장`을 이어서 돌립니다. 평일 오전 9시가 되면 새 에피소드를 시작하지 않습니다.
3. 처리 fps는 기본 15fps(30fps 촬영에서 한 프레임씩 건너뜀)이며 실행마다 30fps를 고를 수 있습니다.

## 빠른 시작

```bash
./start.sh --install-docker          # Docker가 없는 Ubuntu PC에서 처음 한 번(sudo), 끝나면 다시 로그인
./start.sh --weights /path/to/weights # 설정·빌드·GPU 점검·실행·연결 검사
./start.sh                           # 이후 재실행·갱신
```

UI는 `http://<PC-IP>:18083/`(사이트 인증 `operator` / `secrets/ui_password`), Airflow는 `http://<PC-IP>:18082/`입니다. 자세한 내용은 [설치와 실행](documents/install.md)에 있습니다.

## 문서

| 문서 | 내용 |
|---|---|
| [작업자 가이드](code/platform/worker-guide/README.md) | 작업자가 하루 동안 하는 일 (UI 사이드바 **📖 작업 가이드**와 같은 문서) |
| [설치와 실행](documents/install.md) | 준비물, `./start.sh`, weight 경로, 접속 정보, 한 PC에 스택 둘 |
| [운영](documents/operations.md) | GitHub 업데이트, PC별 설정, 점검·로그, 회귀 테스트 |
| [SAM3 · MANO 변경 이력](documents/sam3-mano-changes.md) | 파이프라인이 바뀐 내용과 근거 |
| [MANO 피팅 버전별 결과 비교](documents/mano-fit-verification.md) | 버전마다 MANO 결과가 어떻게 달라지는지 |

## 저장소 구성

| 경로 | 역할 |
|---|---|
| `start.sh`, `repro/start_stack.py` | 한 번의 명령으로 설정·빌드·검증·실행 (`--install-docker`는 `repro/install_docker.sh`) |
| `repro/update_stack.py`, `repro/install_updater.sh` | GitHub 업데이트 타이머 |
| `Dockerfile`, `compose.yaml` | UI·Airflow 및 GPU 이미지 빌드, 네 서비스(PostgreSQL·Airflow·GPU Worker·UI) |
| `gpu/` | GPU 이미지 진입점·자체 점검, `gpu/env/*.lock` 고정 패키지와 CUDA 확장 빌드, `weights.txt` weight 목록 |
| `code/platform/` | 작업자 UI (작업자 가이드는 `code/platform/worker-guide/`) |
| `code/dags/` | Airflow DAG |
| `worker/app/` | GPU Worker, 파이프라인 어댑터(SAM3 고속 실행 등), 검수 백엔드 |
| `pipeline/` | 파이프라인 원본(`current/`)과 의존 소스(`dependency-source/`) |
| `deploy/` | 회귀 테스트, 컨테이너 상태 검사 |
| `documents/` | 설치·운영·변경 이력 문서 |

`.env`, `secrets/`, `state/`, `.runtime/`, `weights/`, `compose.override.yaml`은 PC 전용이라 Git에 넣지 않습니다.
서드파티 코드의 라이선스는 각 소스 폴더에 있으며, 모델 이용 조건은 각 공급자의 조건을 따릅니다.
