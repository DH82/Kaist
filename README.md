# KAIST Docker 파이프라인

새 GPU PC에서 `git clone` 후 Docker로 빌드·실행하면 운영 PC(pc1)와 같은 환경으로 파이프라인이 동작하도록 만든 저장소입니다.
RAW → QC·전처리 → SAM3 → 마스크 검수 → CoTracker → 추적 검수 → 3D 복원 → HaMBA·MANO → NAS 저장 순으로 실행합니다.

| 구성 | 위치 |
|---|---|
| UI·Airflow 이미지 | `Dockerfile` |
| GPU 파이프라인 이미지 | `gpu/Dockerfile` |
| Python 환경 버전 고정 목록 | `gpu/env/{sam,phystwin,hamba}.lock` |
| 환경 빌드 스크립트 (CUDA 확장 포함) | `gpu/env/build_envs.sh` |
| 모델 weight 경로 목록 | `gpu/env/weights.txt` |
| 서비스 구성 (PostgreSQL·Airflow·UI·GPU Worker) | `compose.yaml` |
| UI·작업 제어·검수 코드 | `code/`, `worker/app/` |
| 파이프라인 코드와 라이브러리 소스 | `pipeline/` |
| 새 PC 설치·검증 | `repro/` |

## 환경과 weight를 다루는 방식

- **Python 환경은 저장소에 넣지 않습니다.** `docker compose build` 할 때 PyPI·PyTorch(cu128)·NVIDIA Kaolin·GitHub에서 받아 새로 만듭니다.
  - 버전은 pc1에서 검증된 환경을 freeze한 `gpu/env/*.lock`으로 고정합니다 (sam-env 72개, phystwin-env 191개, hamba-env 196개 패키지).
  - pytorch3d·nvdiffrast·detectron2·diffoctreerast·diff-gaussian-rasterization·selective_scan은 pc1과 같은 git 커밋·소스로 CUDA 12.8에서 컴파일합니다.
  - Python은 3.12.15(SAM3)와 3.10.22(PhysTwin·HaMBA)입니다.
- **모델 weight는 이미지에 넣지 않습니다.** 새 PC에 이미 있는 weight 폴더를 컨테이너 실행 시 **읽기 전용으로 마운트**합니다.
  - weight 폴더는 원본 `WFM_KAIST/workspace`와 같은 구조여야 합니다. 필요한 하위 경로는 `gpu/env/weights.txt`에 있습니다 (SAM3.1·TRELLIS·DINOv2 HF 캐시, torch hub, ViTDet, HaMBA, ViTPose, MANO, SuperGlue).
  - weight를 바꿔도 이미지를 다시 빌드할 필요가 없습니다. 컨테이너만 다시 시작합니다.

## 새 PC 설치

검증 기준: Linux x86_64, NVIDIA 드라이버(CUDA 12.8 지원), RTX 4090. GPU 세대는 설치 때 `nvidia-smi`로 감지해 그 세대용으로 컴파일합니다.

1. `nvidia-smi`가 동작해야 합니다. Docker Engine·Compose v2·NVIDIA Container Toolkit을 준비합니다. Ubuntu에서는 `./install_docker.sh`를 쓸 수 있습니다.
2. NAS를 `/mnt/kaist-data`에 NFS 마운트합니다. `rgbd_raw` 읽기와 `results` 쓰기 권한이 필요합니다. PC마다 PC ID와 NAS 그룹 ID가 다릅니다.
3. weight 폴더 위치를 확인합니다 (예: `~/Documents/Codex/.../WFM_KAIST/workspace`).
4. 아래 명령을 실행합니다. `pc2`, `65537`, 작업 폴더, weight 폴더는 해당 PC 값으로 바꿉니다.

```bash
git clone https://github.com/DH82/Kaist.git
cd Kaist
./repro/install.sh pc2 65537 "$HOME/kaist_work/pc2" /path/to/WFM_KAIST/workspace
```

weight 폴더 인자를 생략하면 원래 PC 경로(`/home/cw-wfm/Documents/Codex/2026-10-20/https-drive-google-com-drive-folders/outputs/WFM_KAIST/workspace`)를 사용합니다.

`install.sh`는 다음 순서로 실행합니다. 소스 체크섬 검증 → weight 경로 확인·GPU 세대 감지 → PC별 `.env` 생성 → GPU 이미지 빌드(환경 다운로드·컴파일) → 모델 포함 전체 점검(`check`) → Worker 시작 → UI·Airflow 시작.
처음 빌드는 패키지 다운로드(약 15GB)와 CUDA 확장 컴파일 때문에 오래 걸립니다. 이후 빌드는 Docker 캐시를 재사용합니다.
기존 `.env`나 8090 포트의 Worker가 있으면 덮어쓰지 않고 중단합니다.

설치 후 UI는 `http://<PC-IP>:18083`, Airflow는 `http://<PC-IP>:18082`입니다. UI 계정은 `operator`이고 비밀번호는 `secrets/ui_password`에 있습니다. 각 PC가 자체 UI·Airflow·DB를 가지며 NAS는 저장소로만 씁니다.

## Docker 명령으로 직접 실행

`repro/install.sh`의 핵심 명령은 아래와 같습니다.

```bash
python3 repro/prepare_pc.py --pc-id pc2 --nas-gid 65537 --project "$HOME/kaist_work/pc2" \
    --weights /path/to/WFM_KAIST/workspace          # .env 생성 (weight 경로·GPU 세대 포함)
docker compose --profile gpu build gpu-worker       # 환경 다운로드·컴파일
docker compose --profile gpu run --rm --no-deps gpu-worker check   # GPU·환경·모델 점검
docker compose --profile gpu up -d gpu-worker       # GPU Worker
./setup.sh                                          # PostgreSQL·Airflow·UI
```

weight 폴더나 GPU 세대를 바꾸려면 `.env`의 `KAIST_WEIGHTS_ROOT`, `TORCH_CUDA_ARCH_LIST`를 고칩니다. `TORCH_CUDA_ARCH_LIST`를 바꾼 경우에만 이미지를 다시 빌드합니다.

## 운영

```bash
docker compose --profile gpu ps
docker compose --profile gpu logs -f gpu-worker
docker compose logs -f ui airflow
```

UI에서 RAW를 선택해 실행합니다. 마스크·추적 검수는 사람이 승인해야 다음 단계로 진행합니다.

운영 데이터는 `gpu_state/`, `state/`, `intake_state/`, 로컬 작업 폴더, PostgreSQL 볼륨에 저장됩니다. `.env`, 토큰, 비밀번호, 실행 이력, 모델은 Git에 올리지 않습니다. **`docker compose down -v`는 DB 볼륨을 삭제하므로 주의하세요.**

## 검증 범위

- `python3 repro/verify_source.py`: 포함된 파이프라인 파일을 SHA-256으로 검증합니다.
- 환경 고정 목록은 2026-11-13 pc1 환경에서 생성했습니다. 원본 환경에서도 sam-env는 `decord` 휠 플랫폼 경고 1건이 있으며 동작에는 문제가 없습니다.
- 모델을 갖춘 새 PC에서 전체 에피소드를 다시 실행하는 검증은 아직 하지 않았습니다.
- 서드파티 소스의 라이선스는 각 소스 폴더에 있습니다. 모델 이용 조건은 각 모델 공급자의 조건을 따릅니다.

## GitHub 인증

비공개 저장소이므로 clone하려면 접근 권한이 있는 계정이 필요합니다(`gh auth login` 등). 저장소를 업데이트할 담당자는 `./repro/github_login.sh`로 PAT를 숨김 입력할 수 있습니다. 토큰을 `.env`, Git remote URL, 코드에 넣지 마세요.
