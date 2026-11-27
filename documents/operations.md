# 운영: 업데이트 · PC별 설정 · 점검

## 1. GitHub 업데이트 (PC 일괄 반영)

`./start.sh`가 끝나면 업데이트 타이머(`systemd --user kaist-update.timer`, 3분 간격)가 설치됩니다. 수동 설치·제거는 `repro/install_updater.sh` / `repro/install_updater.sh --remove`, 요청 없이 새 커밋을 바로 반영하려면 `repro/install_updater.sh --auto`입니다.
로그아웃 뒤에도 타이머가 돌려면 `sudo loginctl enable-linger $USER`가 한 번 필요합니다.

### 1.1 순서

1. 개발 PC에서 `git push origin main`.
2. 관리자 화면 **통합 관리 · PC1~6 → PC별 현황**의 `버전 · 업데이트` 열에서 PC별 **업데이트** 또는 상단 **⟳ 전체 업데이트**를 누릅니다.
3. 각 PC는 GitHub 코드를 받아(fast-forward) 새 이미지를 빌드·점검합니다. 이 동안 기존 서비스는 계속 돕니다. 작업이 쉬면 재시작하고 UI 연결을 검사합니다.
4. 빌드나 재시작에 실패하면 이전 커밋으로 되돌려 다시 띄우고 대시보드에 실패를 표시합니다.

### 1.2 바뀐 파일에 따라 하는 일

| 바뀐 파일 | 빌드 | 기다리는 것 | 재시작 |
|---|---|---|---|
| 문서(`.md`), `repro/`, `start.sh` 등 이미지에 안 들어가는 파일만 | 없음 | 없음 | 없음 |
| `code/platform/`만 | UI 이미지 | 없음 | UI만 |
| 그 밖의 `code/`·`deploy/` (DAG 등) | UI 이미지 | Airflow 실행 중 태스크·3D 복원 일괄 실행 | Airflow·UI |
| `worker/`·`pipeline/`·`gpu/`·`Dockerfile`·`compose.yaml` 등 | 전체 | GPU 작업·Airflow 태스크·일괄 실행 | 전체(GPU 점검 포함) |

`pipeline/` 코드는 실행마다 해시로 고정되므로 다음을 지킵니다.

- 검수·설정 입력 대기만 남아 있으면 업데이트합니다. 검수를 이어갈 때 이전 코드 기록을 보관하고 새 코드로 진행합니다. 입력·설정 변경은 자동으로 허용하지 않습니다.
- 파이프라인 설정·데이터 파일(`.py`가 아닌 것)이 바뀌면 진행 중인 실행이 끝날 때까지 기다립니다.
- 과거 3D 결과는 자동으로 다시 돌리지 않습니다. **3D 복원 일괄 실행**은 업데이트 전 코드로 고정된 에피소드를 항상 3D 복원부터 다시 하고(이전 결과는 `99_history/reset`에 보관) 이어서 MANO를 진행합니다. 코드(`.py`)만 바뀐 경우이며, 승인한 마스크·추적은 그대로 씁니다. 설정 파일(`config.env`·`runtime.local.json`)이 바뀐 에피소드는 건너뜁니다.

### 1.3 요청 중 · 취소 · 강제 업데이트

- 요청이 걸려 있는 동안 그 PC에서는 새 RAW 실행과 3D 복원 일괄 실행을 시작할 수 없습니다(진행 중인 작업은 계속). **요청 취소**를 누르면 풀리고, 재시작 전이었다면 업데이트도 보류됩니다.
- **⚡ 강제 업데이트**(PC별, 상단 **⚡ 전체 강제 업데이트**, 걸린 요청은 **강제로 전환**)는 작업을 기다리지 않습니다. 빌드가 끝나면 그 PC의 UI가 파이프라인 실행·일괄 실행·그 밖의 GPU 작업을 모두 강제 종료하고 바로 GPU 점검·재시작합니다.
  - 검수·설정 입력 대기 중인 실행은 그대로 둡니다.
  - 종료된 실행은 업데이트 뒤 **실패 단계 다시 실행**으로 이어 하고, 끊긴 NAS 업로드는 다시 올립니다.
  - UI가 3분 안에 응답하지 않으면(꺼짐·이전 버전) 재시작으로 작업을 끝냅니다. 종료한 작업 목록은 결과 메시지에 남습니다.

### 1.4 PC에서 직접 고친 파일

직접 고친 파일이나 GitHub와 갈라진 커밋이 있어도 멈추지 않습니다. 고친 내용을 `state/updater/discarded/<시각>/local.patch`와 브랜치 `updater-backup/<시각>`에 보관한 뒤 GitHub 최신 코드로 덮어씁니다. GitHub보다 앞선 로컬 커밋만 있으면 그대로 둡니다.

### 1.5 일회성 정비 작업

NAS `control/update/tick_hooks.json`에 적어 둔 명령을 타이머가 돌 때마다 업데이트 앞뒤로 실행합니다. 각 항목은 명령(`{nas}`·`{work}`·`{pc}` 치환)과 선택 항목 `until`(만료 시각)입니다. 실패해도 업데이트를 막지 않으며, 끄려면 NAS 파일만 고치면 됩니다(푸시 불필요).

현재 등록된 작업은 보정값 교체(calfix)입니다. 보정 회차가 잘못 찍힌 날의 RAW에 대해 `03_prepared/<ep>/calibrate.pkl`·`world_reorient_M.npy`를 교체본으로 바꿉니다. 마스크 승인 후·3D 시작 전 에피소드만 바꿉니다. 3D를 이미 시작한 에피소드는 전처리부터 다시 실행해야 하고, 다시 실행하면 마스크 승인 뒤 다음 타이머에서 자동으로 교체됩니다. 도구와 회차 기록은 [deploy/calfix/](../deploy/calfix/README.md)에 있습니다.

### 1.6 RAW 다시 검수

결과를 지우고 RAW를 처음부터 다시 검수해야 할 때(예: 보정값 오류) NAS `control/redo/<RAW>.json`에 표시를 씁니다. 각 PC UI가 20초마다 읽어, 표시 시각 전에 시작한 그 RAW의 실행을 '대체됨'으로 기록하고 담당·배정·NAS 선점을 풉니다. RAW는 RAW 목록에서 공용 대기열(미배정)로 돌아가 다시 자동 배정되며, 대체된 실행은 3D 대기 목록·RAW 진행 판단에서 빠지고 재시도·단계 초기화·일괄 실행이 막힙니다(작업 현황 기록은 남음). 진행 중인 실행이 있는 RAW는 그 실행이 끝난 뒤 반영합니다. 모든 PC가 이 기능이 든 코드로 업데이트돼야 다른 PC에도 풀립니다.

```bash
cd code/platform && python3 -I -c "import sys;sys.path.insert(0,'.');import fleet;[fleet.raw_redo_mark(e,'관리자','사유') for e in sys.argv[1:]]" ep_20261125_014211 …
```

### 1.7 진행 상황 · 로그

- 같은 탭 위쪽 **업데이트 진행** 패널: PC별로 코드 받기 → 이미지 빌드 → GPU 점검 → 작업 종료 대기 → 서비스 재시작 → UI 확인의 소요 시간, 빌드 층 진행도(완료/전체, 캐시 수), 현재 빌드 단계, 최근 로그. 진행 중에는 5초마다 갱신하고 끝난 결과는 6시간 남습니다.
- 요청과 상태: NAS `control/update/<pc>/{request,status}.json`.
- 로그: `journalctl --user -u kaist-update -e`, `state/updater/last_run.log`. 상태만 보려면 `python3 repro/update_stack.py --status`.
- Docker 없이 native로 돌리는 PC는 코드를 받은 뒤 UI만 재시작합니다(GPU Worker·파이프라인은 수동 배포).

## 2. PC별 설정

기본값으로 운영하고, 바꿀 때만 씁니다. 스택 `.env`의 값은 `compose.yaml`이 읽지만, 서비스 안에서만 쓰는 환경 변수는 `compose.yaml`이 넘기지 않으므로 저장소 폴더의 `compose.override.yaml`(Git에 넣지 않음)에 서비스별로 적습니다.

| 설정 | 위치 | 기본 동작 |
|---|---|---|
| `KAIST_SAM3_SLOTS` | Worker 환경 | SAM3 동시 실행 수. RTX 4090은 1개, RTX 5090은 2개(두 번째 자리를 마법봉과 공유). `0`이면 제한 없음 |
| `KAIST_WAND_MIN_FREE_MB` | Worker 환경 | 마법봉을 띄울 최소 여유 GPU 메모리(기본 8192) |
| `KAIST_HAND_ROTATE_CAMS` | Worker 환경 | 손 포인트 추적을 180° 돌려서 할 카메라 번호(0부터, 기본 `1` = 카메라 2). 빈 값이면 끔 |
| `KAIST_MANO_CUDA_GRAPH` | Worker 환경 | `0`이면 MANO 피팅 CUDA graph를 끄고 예전 루프로 실행 |
| `nas_upload_limit.json` | Worker 설정 폴더(컨테이너 안 `…/airflow/config/`, override로 마운트) | 있을 때만 정한 시간대에 NAS 업로드 속도 제한. 예: `{"mb_per_s": 8, "from": "09:30", "to": "18:30", "weekdays_only": true}` |
| `"hand_profiles": true` | Worker 설정 json | 사람별 손 형상 프로필 적용. 없으면 에피소드마다 손 형상(β) 보정 |
| `KAIST_HAND_PROFILE_UI=1` | UI 환경 | MANO beta 화면·RAW 손 형상 열·비교 화면 표시(기본 숨김) |
| `KAIST_EXTRA_LOGIN_NAMES` | UI 환경 + 스택 `.env` | PC1~PC6 외에 허용할 로그인·PC 이름(예: 시험 스택 `PCT`) |
| `KAIST_SESSION_COOKIE` | UI 환경 | 세션 쿠키 이름(기본 `kaist_session`). 한 PC에 UI가 둘이면 서로 다르게 |
| `KAIST_FLEET=off`, `KAIST_CONTROL_DIR` | UI 환경 | 시험 스택처럼 NAS PC 연동을 끄고 관제 기록을 따로 둘 때 |
| `KAIST_GPU_IMAGE_TAG`, `KAIST_AIRFLOW_IMAGE_TAG` | 스택 `.env` | 이미지 태그(기본 `latest`, `3.3.2`). 한 PC에 스택이 둘이면 스택마다 다르게 |

PC마다 기본 SAM3 분할 방식(`물체 포인트 + 손 텍스트`(기본) / `물체 포인트 + 손 포인트` / `텍스트만`)은 UI DB에 저장됩니다. 작업 실행 화면 **실행 설정 → 물체 분할 방식**에서 고른 뒤 기본값 저장 버튼으로 바꿉니다.

NAS 회선이 느린 PC는 낮 시간 업로드 제한을 둡니다. 제한 없이 작업 폴더(에피소드당 약 12 GB)를 올리면 NAS 쓰기 한계(약 11 MB/s)에 걸려 다른 PC의 화면 조회까지 한 건에 0.1~0.5초씩 밀립니다.

## 3. 점검과 로그

```bash
./start.sh --check                    # 파일·서비스를 바꾸지 않고 사전 검사
./start.sh --configure-only           # 설정 파일만 준비
python3 repro/verify_source.py        # 포함된 소스 체크섬 검사
python3 repro/check_ui.py             # 로그인·Airflow·Worker·화면 API 연결 검사 (--env-file로 스택 지정)

docker compose ps
docker compose logs -f ui airflow gpu-worker
docker compose run --rm --no-deps gpu-worker check   # GPU·모델·렌더링·저장소 자체 점검(실제 GPU 사용)
```

- 스택을 따로 띄웠다면 모든 `docker compose` 명령에 그 스택의 `--env-file`을 붙입니다.
- `check`는 환경·모델·GPU·렌더링·저장소 검사이고, 에피소드 하나를 끝까지 추론했다는 뜻은 아닙니다. 실제 결과의 정확도는 UI 검수로 확인합니다.
- `./start.sh --gpu-check-only`는 빌드한 이미지가 이미 실행 중인 이미지와 같으면 점검을 건너뜁니다. 꼭 다시 하려면 위 `check` 명령을 씁니다.
- `docker compose down -v`는 DB 볼륨까지 지웁니다. 운영 이력을 지키려면 쓰지 않습니다.
- 5초 넘는 UI 요청은 UI 로그에 `slow request`로 남습니다. 화면이 느리다는 신고는 이 로그와 NAS 업로드부터 확인합니다.

## 4. 회귀 테스트 (GPU 불필요)

UI·DAG 테스트는 `fastapi`·`httpx`가 든 UI 이미지 안에서, 호스트 경로·PIL이 필요한 테스트는 호스트에서 돌립니다.

```bash
docker run --rm --network none -u $(id -u):0 -e HOME=/tmp -v $PWD:/src:ro -w /src \
  --entrypoint python kaist-airflow:3.3.2 -m unittest discover -s deploy
python3 -m unittest deploy.test_publish_background deploy.test_sam3_single deploy.test_track_replace deploy.test_updater
```

UI 이미지 안에서는 `test_publish_background`·`test_sam3_single`·`test_track_replace`가 환경 문제로 오류가 나므로 두 번째 줄로 확인합니다. `test_stage_reset`에는 `KAIST_WORKERS_FILE`(스택의 `config/workers.json`)이 필요합니다.
