# 설치와 실행

GPU PC 한 대에 UI·Airflow·PostgreSQL·GPU Worker 네 서비스를 Docker로 띄우는 방법입니다. 모든 명령은 저장소 폴더에서 실행합니다.

## 1. 준비

| 항목 | 조건 |
|---|---|
| OS | Linux x86_64 (Ubuntu 기준) |
| GPU | `nvidia-smi`가 동작하는 NVIDIA GPU, CUDA 12.8 지원 드라이버. 기준: RTX 4090 (RTX 5090도 지원) |
| Docker | Docker Engine, Compose v2(`up --wait` 지원), NVIDIA Container Toolkit. 없으면 아래 2절 |
| 저장소(NAS) | `/mnt/kaist-data`에 NFS로 마운트되어 있으면 자동 사용. 없으면 로컬 `data/rgbd_raw`, `data/results`를 만듦. 다른 위치는 `--nas` |
| 모델 weight | 이미 받아 둔 weight 폴더(3절). 이미지에 넣지 않고 읽기 전용으로 마운트 |
| 계정 | `python3`와 Docker 권한이 있는 일반 사용자. root로 실행하지 않음 |

Ubuntu·CUDA·Airflow·PostgreSQL 이미지는 레지스트리에서, Python 환경 3개(sam-env, phystwin-env, hamba-env)는 빌드 때 PyPI·PyTorch 저장소에서 `gpu/env/*.lock`의 고정 버전으로 받아 만듭니다.
GitHub에는 코드·Dockerfile·버전 목록만 있고 가상환경·weight·실행 데이터는 올리지 않습니다.
첫 빌드는 다운로드와 CUDA 확장 컴파일로 오래 걸리고(GPU 이미지 약 45 GB), 이후에는 빌드 캐시를 다시 씁니다.

## 2. Docker 설치 (처음 한 번)

```bash
./start.sh --install-docker
```

sudo로 Docker·Compose·NVIDIA Container Toolkit을 설치하고 현재 계정을 `docker` 그룹에 넣습니다(`repro/install_docker.sh`). 끝나면 로그아웃 후 다시 로그인하거나 `newgrp docker`를 실행합니다.

## 3. 처음 실행

weight 폴더만 지정하면 GPU 세대, 사용자 UID, PC 주소, 작업 폴더, 저장소, 비밀번호를 자동으로 정합니다.

```bash
./start.sh --weights /path/to/weights
```

`./start.sh`가 하는 일: 소스 체크섬·GPU·NAS·weight·포트 확인 → `.env`와 인증 파일 생성 → 이미지 빌드 → GPU·모델·렌더링·저장소 자체 점검 → 네 서비스 기동 → 로그인한 상태로 UI/API 연결 검사.
하나라도 실패하면 성공으로 표시하지 않습니다. 설정은 `.env`에 저장되므로 그 뒤의 재실행·갱신은 `./start.sh` 한 줄입니다.

NAS의 PC 번호·그룹을 직접 정해야 하면 `--pc-id pc2 --nas-gid 65537`을 붙입니다. PC 번호는 PC마다 달라야 합니다(결과가 NAS `results/<PC 번호>/`에 저장됨).

### 3.1 weight 경로

`--weights`에 준 폴더 아래의 상대 경로에서 모델을 찾습니다(목록: `gpu/env/weights.txt`). 모델이 서로 다른 곳에 있으면 `--weight 이름=경로`를 반복합니다.
이미 저장된 경로는 유지되고 지정한 항목만 바뀌며, `--weights`를 새로 주면 공통 폴더 기준으로 다시 정합니다. 터미널에서 빠진 경로는 물어봅니다.

```bash
./start.sh --weights /path/to/weights \
  --weight hf-hub=/data/models/huggingface/hub \
  --weight mano=/data/models/mano \
  --weight hamba=/data/models/hamba/checkpoints
```

| 이름 | 내용 | 공통 폴더 기준 경로 |
|---|---|---|
| `hf-hub` | SAM3.1 / TRELLIS / DINOv2 Hugging Face hub 캐시 | `hf-cache/hub` |
| `torch-hub` | CoTracker 체크포인트, DINOv2 torch hub 캐시 | `runtime-cache/torch/hub` |
| `vitdet` | ViTDet iopath 캐시 | `runtime-cache/iopath` |
| `mano` | MANO_LEFT.pkl, MANO_RIGHT.pkl이 있는 폴더 | `kaist-full/PhysTwin/Mono/data_process/Hamba/_DATA/data/mano` |
| `mano-mean` | mano_mean_params.npz 파일 | `kaist-full/PhysTwin/Mono/data_process/Hamba/_DATA/data/mano_mean_params.npz` |
| `vitpose` | ViTPose 체크포인트 폴더 | `kaist-full/PhysTwin/Mono/data_process/Hamba/_DATA/vitpose_ckpts` |
| `hamba` | hamba.ckpt가 있는 폴더 | `kaist-full/PhysTwin/Mono/data_process/Hamba/ckpts/hamba/checkpoints` |
| `superglue` | SuperPoint / SuperGlue weight 폴더 | `kaist-full/PhysTwin/data_process/models/weights` |
| `mano-faces` | mano_faces.pkl 파일 | `kaist-full/PhysTwin/data/mano_faces.pkl` |
| `mano-faces-mono` | Mono 단계용 mano_faces.pkl(보통 `mano-faces`와 같음) | `kaist-full/PhysTwin/Mono/data/mano_faces.pkl` |

- `hf-hub`에는 hub **캐시 폴더**를 줍니다. SAM3.1뿐 아니라 TRELLIS·DINOv2 캐시도 들어 있어야 하며, `.pt` 파일 하나를 지정할 수는 없습니다.
- `mano-faces`만 지정하면 Mono 단계에도 같은 파일을 씁니다.
- MANO v7이 쓰는 MediaPipe 손 검출 모델은 `mediapipe` 패키지(`gpu/env/hamba.lock`) 안에 있어 따로 지정하지 않습니다.
- SAM3.1(Hugging Face 접근 승인)과 MANO(라이선스 등록)는 다시 받으려면 승인 절차가 필요합니다. weight 폴더는 지우지 않도록 주의합니다.

## 4. 접속 정보

| 대상 | 주소 | 계정 |
|---|---|---|
| 작업자 UI | `http://<PC-IP>:18083/` | 사이트 인증 `operator` / `secrets/ui_password` → 화면에서 PC 이름(PC1~PC6) 선택 |
| UI 관리자 | 같은 주소, 접속 구분 `관리자` | `state/simple_auth_manager_passwords.json.generated`의 admin 비밀번호 |
| Airflow | `http://<PC-IP>:18082/` | `admin` / 위와 같은 파일 |

`.env`, `secrets/`, `state/` 같은 PC 전용 파일과 weight·실행 데이터는 Git에 넣지 않습니다. UI·Worker 코드는 이미지에 들어가므로, 코드를 바꾼 뒤에는 `./start.sh`로 다시 빌드합니다.

## 5. 한 PC에 스택을 둘 띄울 때 (정식 + 시험)

기존 스택은 그대로 두고, 이름·포트·상태 폴더·이미지 태그가 다른 스택을 하나 더 띄울 수 있습니다. `--stack-name`을 주면 상태가 `.runtime/<스택 이름>/`에 따로 생깁니다.

```bash
./start.sh --env-file .runtime/kaist-test/test.env --stack-name kaist-test \
  --ui-port 28083 --airflow-port 28082 --worker-port 28091 --postgres-port 25435
```

- `.env` 파일(위 예: `test.env`)에 `KAIST_GPU_IMAGE_TAG`·`KAIST_AIRFLOW_IMAGE_TAG`를 스택마다 다르게 적습니다. 같으면 한쪽 빌드가 다른 쪽 이미지를 덮어씁니다.
- 두 스택의 `GPU_LOCK_FILE`은 같은 파일이어야 GPU 작업이 겹치지 않습니다.
- PC1~PC6 밖의 PC 이름(예: 시험 `pct`)은 `KAIST_EXTRA_LOGIN_NAMES`(UI)에 같은 이름이 있을 때만 받습니다. UI 쿠키가 섞이지 않게 `KAIST_SESSION_COOKIE`도 다르게 줍니다.
- UI 전용 환경 변수는 `compose.yaml`이 넘기지 않으므로, 저장소 폴더의 `compose.override.yaml`(Git에 넣지 않음)에 적습니다.
- 이후 모든 명령에 같은 `--env-file`을 붙입니다. 이미 쓰는 포트는 자동으로 빼앗지 않고 시작 전에 멈춥니다.
- GPU 점검은 실제 GPU를 쓰므로 다른 스택에 작업이 없을 때 돌립니다.
