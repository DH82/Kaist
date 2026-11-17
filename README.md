# KAIST Docker 파이프라인

`./start.sh` 한 번으로 **PostgreSQL → GPU Worker → Airflow → 작업자 UI**를 시작합니다.
루트 `Dockerfile` 하나에 `airflow`(UI 포함), `gpu` 빌드 타깃을 두고 Docker Compose가 서비스를 관리합니다.

UI는 기본 `http://<PC-IP>:18083/`입니다. 현재 UI 코드의 RAW 목록·배정, SAM3 설정과 실행,
마스크·추적 검수, 3D 복원, HaMBA·MANO, 작업 재시도, 결과 조회·NAS 저장 기능을 포함합니다.
사람의 검수 승인이 필요한 단계는 UI에서 승인해야 계속 진행됩니다.

## 준비

- Linux x86_64, Docker Engine·Compose v2(`up --wait` 지원), NVIDIA Container Toolkit.
- `nvidia-smi`가 정상 동작하는 GPU와 CUDA 12.8 지원 드라이버. 기준 GPU: RTX 4090.
- NAS가 `/mnt/kaist-data`에 마운트되어 있으면 자동으로 사용합니다. 없으면 로컬 `data/rgbd_raw`, `data/results`를 생성합니다. 별도 저장소는 `--nas`로 지정할 수 있습니다.
- 기존 모델 weight 폴더. 필요한 상대 경로는 `gpu/env/weights.txt`에 있습니다.
- `python3`와 Docker 접근 권한이 있는 일반 사용자 계정. root 계정으로 실행하지 않습니다.

Ubuntu·CUDA·Airflow·PostgreSQL은 컨테이너 레지스트리에서 자동으로 받고, Python 환경 3개는 빌드 시 Python 배포처·PyPI·PyTorch 저장소에서 고정 버전으로 다운로드·컴파일합니다. GitHub에는 실행 코드·Dockerfile·버전 목록만 저장하며 Ubuntu 이미지, Python/torch 설치 파일, 가상환경, weight, 실행 데이터는 올리지 않습니다. 모델 weight는 이미지에
포함하지 않으며 호스트에서 읽기 전용으로 마운트합니다. 첫 빌드는 대용량 다운로드와 CUDA
컴파일로 오래 걸립니다. 이후에는 Docker 빌드 캐시를 재사용합니다.

## 한 번에 실행

weight 폴더만 지정하면 GPU 세대, 사용자 UID, PC 주소, 작업 폴더, 저장소, 비밀번호를 자동 설정합니다.

```bash
./start.sh --weights /path/to/WFM_KAIST/workspace
```

터미널에서 `./start.sh`만 실행하면 필요한 weight 경로를 물어봅니다. weight들이 서로 다른 곳에 있으면
`--weight 이름=경로`를 반복해서 지정할 수 있습니다. 경로는 `.env`에 저장되어 다음 실행 때 다시 묻지 않습니다.

```bash
./start.sh --weights /path/to/workspace \
  --weight hf-hub=/data/models/huggingface/hub \
  --weight mano=/data/models/mano \
  --weight hamba=/data/models/hamba/checkpoints
```

모델별 경로의 이름과 형식은 [weight 설정 안내](repro/MODELS.md)에 있습니다.
공통 폴더가 없어도 모든 모델 경로를 개별 지정할 수 있습니다.
NAS PC 번호·그룹을 직접 지정해야 하면 `--pc-id pc2 --nas-gid 65537`을 추가합니다.

설정 후 재실행·갱신은 다음 한 줄입니다.

```bash
./start.sh
```

스크립트는 소스 체크섬·GPU·NAS·weight·포트 확인 → `.env`와 인증 파일 생성 → 두 이미지 빌드 →
GPU·모델·렌더링·저장소 자체 점검 → 네 서비스 기동 → 인증된 UI/API 연결 검사 순서로 실행합니다.
UI·Airflow의 Worker 주소와 토큰 경로는 함께 설정합니다. 일부 검사에 실패하면 성공으로 표시하지 않습니다.

- UI 로그인: `operator`, 비밀번호는 `secrets/ui_password`.
- UI 관리자 로그인 및 Airflow `admin` 비밀번호: `state/simple_auth_manager_passwords.json.generated`.
- Airflow 관리 화면: `http://<PC-IP>:18082/`.
- `.env`, 인증 파일, 모델, 실행 데이터는 Git에 넣지 않습니다.
- UI 및 Worker 코드는 이미지에 포함됩니다. 코드 변경 후 `./start.sh`로 다시 빌드합니다.

기존 `./setup.sh`와 `./repro/install.sh PC_ID NAS_GID LOCAL_PROJECT [WEIGHTS_ROOT] [NAS_MOUNT]`도
동일한 실행 흐름을 호출합니다.

## 기존 서비스를 운영 중인 PC

이미 사용 중인 포트를 자동으로 빼앗거나 기존 native 서비스를 종료하지 않습니다.
현재 `10.20.32.21:18083`을 계속 운영하면서 새 구성을 검증하려면 별도 설정·포트·상태 폴더를 사용하세요.
NAS 그룹과 weight 경로는 해당 PC 값으로 지정합니다.

```bash
./start.sh --env-file .env.validation --stack-name kaist-validation \
  --state-dir "$PWD/.runtime/validation" \
  --project "$PWD/.runtime/validation/project" \
  --ui-port 28083 --airflow-port 28082 --worker-port 28090 --postgres-port 25432 \
  --nas-gid 65536 --weights /path/to/WFM_KAIST/workspace
```

별도 설정을 사용할 때 이후 Compose 명령에도 `--env-file .env.validation`을 전달합니다.
기존 운영 작업 폴더·DB·검수 이력의 자동 이관은 하지 않습니다. 운영 전환 시 기존 서비스를 종료하고
기존 상태 경로 및 포트를 선택해야 합니다. GPU 모델 점검은 실제 GPU 연산을 수행하므로 여유 시간에 실행하세요.

## GitHub 업데이트 (PC 일괄 반영)

`./start.sh`가 끝나면 업데이트 타이머(`systemd --user kaist-update.timer`, 3분 간격)가 자동으로 설치됩니다.
수동 설치·제거는 `repro/install_updater.sh` / `repro/install_updater.sh --remove`입니다.

1. 개발 PC에서 `git push origin main`.
2. 관리자 화면 **통합 관리 · PC1~6 → PC별 현황**의 `버전 · 업데이트` 열에서 PC별 **업데이트** 또는 상단 **⟳ 전체 업데이트**를 누릅니다.
3. 각 PC는 GitHub 코드를 받아(fast-forward) 새 이미지를 빌드·점검합니다. 이 동안 기존 서비스는 계속 동작합니다.
   이후 GPU 작업·Airflow 태스크·MANO 일괄 실행이 없을 때 서비스를 재시작하고 UI 연결을 검사합니다.
   `pipeline/` 코드가 바뀌어도 검수·설정 입력 대기만 남아 있으면 업데이트합니다. 검수를 이어갈 때 이전 코드 기록을 보관하고 새 코드로 진행하며, 입력·설정 변경은 자동으로 허용하지 않습니다. 파이프라인 설정·데이터 파일 변경은 기존 실행이 끝날 때까지 기다립니다.
   업데이트는 과거 3D 결과를 자동 재실행하지 않습니다. 이전 버전이 남긴 자동 재복원 요청도 종료하며, 이미 시작한 계산은 유지합니다. 재복원이 필요하면 작업 목록에서 직접 선택합니다.
4. 요청이 걸려 있는 동안 그 PC에서는 새 RAW 실행과 MANO 일괄 실행을 시작할 수 없습니다. 진행 중인 작업은 계속됩니다.
   **요청 취소**를 누르면 차단이 풀립니다. 재시작 전이었다면 업데이트도 보류됩니다.
5. 빌드나 재시작에 실패하면 이전 커밋으로 되돌려 다시 기동하고, 실패 내용을 대시보드에 표시합니다.
   PC에서 직접 고친 파일이 있으면 업데이트하지 않습니다.

진행 상황은 같은 탭 위쪽 **업데이트 진행** 패널에 표시되며, 진행 중에는 5초마다 갱신됩니다.
PC별로 코드 받기 → 이미지 빌드 → GPU 점검 → 작업 종료 대기 → 서비스 재시작 → UI 확인 단계의 소요 시간을 보여줍니다.
빌드 중에는 층 진행도(완료/전체, 캐시 사용 수), 현재 빌드 단계와 출력, 최근 로그도 함께 표시합니다.
대기 중이면 대기 이유를 보여주고, 끝난 결과는 6시간 동안 남습니다.
요청과 상태는 NAS `control/update/<pc>/{request,status}.json`에 저장됩니다.
로그는 `journalctl --user -u kaist-update -e`와 `state/updater/last_run.log`에서 확인합니다.
상태만 확인하려면 `python3 repro/update_stack.py --status`를 실행합니다.
요청 없이 새 커밋을 자동 반영하려면 `repro/install_updater.sh --auto`로 설치합니다.
Docker 없이 native로 운영하는 PC는 코드를 받은 뒤 UI만 재시작합니다. GPU Worker와 파이프라인 사본은 수동으로 배포해야 합니다.
로그아웃 후에도 타이머가 동작하려면 `sudo loginctl enable-linger $USER`가 한 번 필요합니다.

## 점검과 운영

```bash
./start.sh --check                       # 파일·서비스를 바꾸지 않고 사전 검사
./start.sh --configure-only              # 설정 파일만 준비
python3 repro/verify_source.py           # 포함된 소스 체크섬 검사
python3 repro/check_ui.py                # 로그인·Airflow·Worker·화면 API 연결 검사
python3 -m unittest discover -s deploy   # GPU 없는 빌드 패치 회귀 검사

docker compose ps
docker compose logs -f ui airflow gpu-worker
docker compose run --rm --no-deps gpu-worker check
```

`check`는 환경·모델 파일·GPU·렌더링·저장소 검사이며, 전체 에피소드 추론 완료를 의미하지 않습니다.
실제 RAW 데이터의 검수와 최종 결과 정확도는 UI에서 확인해야 합니다.
`docker compose down -v`는 DB 볼륨까지 삭제하므로 운영 이력을 보존하려면 사용하지 마세요.

| 파일 | 역할 |
|---|---|
| `Dockerfile` | UI·Airflow 및 GPU 이미지의 단일 빌드 정의 |
| `compose.yaml` | 네 서비스, 준비 상태 검사, 영속 데이터·weight 마운트 |
| `start.sh`, `repro/start_stack.py` | 한 번의 명령으로 설정·빌드·검증·실행 |
| `gpu/env/*.lock`, `build_envs.sh` | 고정된 Python 패키지와 CUDA 확장 빌드 |
| `gpu/env/patch_selective_scan.py` | 이미지 안에서만 GPU 없는 CUDA 빌드 지원 |
| `code/`, `worker/app/`, `pipeline/` | UI·작업 제어·검수·파이프라인 소스 |

서드파티 코드의 라이선스는 각 소스 폴더에 있으며 모델 이용 조건은 각 공급자의 조건을 따릅니다.
