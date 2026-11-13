# kaist_pipeline — RGB-D 처리 + KAIST 납품 파이프라인

멀티카메라 RealSense RGB-D 수집본(에피소드 폴더)을 받아, PhysTwin 포맷으로 변환하고
세그멘테이션·추적·3D 재구성·MANO 손 피팅을 거쳐 **KAIST 납품 zip** 까지 만드는 파이프라인입니다.
사내 개발 PC 의 코드(`PhysTwin`, `PhysicalAI_data_code`)에서 필요한 부분만 추출했고,
이 PC 에 묶인 경로는 모두 `config.env` 한 곳으로 모았습니다.

범위: **납품 파이프라인까지**. 물리 파라미터 피팅(CMA/train/inference, 붕괴 분석)은 포함하지 않습니다.

```
[수집 PC: RealSense ×4]                         (이 패키지 범위 밖 — 하드웨어 필요, 아래 "수집 쪽" 참고)
   cam_N.mp4, cam_N_depth/*.npy, cam_N_intrinsics.json, cam_N_timestamps.csv, extrinsics.json
        │
        │  bin/kp ingest      원본 폴더 복사
        │  bin/kp qc          (선택) 수집 품질 검사
        ▼
 Phase 1  bin/kp phase1       전처리(동기화·캘리브레이션 환산·월드 z-up) → 노출 trim → SAM 3.1
        │
   ── 사람 검수 지점 ──      마스크 확인/수정 (bin/kp review, GUI 필요) — 여기서 멈춥니다
        │
 Phase 2  bin/kp phase2       CoTracker3 → TRELLIS 형상 → 3D 재구성 → 메시 정합 → final_data
                              → MANO 손 피팅 → 납품 레이아웃/메타데이터 → 검증 영상 → zip
```

---

## 1. 디렉터리 구성

| 경로 | 내용 |
|---|---|
| `bin/kp` | 실행 스크립트 (모든 단계의 진입점) |
| `config.env.example` | 설정 템플릿 → `config.env` 로 복사해서 사용 |
| `episodes.example.json` | 에피소드–물체 매핑 예시 |
| `samples/` | 테스트용 수집 원본 에피소드 3개(천·스펀지·마우스패드) + `episodes.json` |
| `capture/` | 수집 품질 검사 (`capture_qc.py`, `run_capture_qc.py`) |
| `PhysTwin/` | 처리 코드 (PhysTwin 기반 + 사내 추가 스크립트) |
| `PhysTwin/Mono/` | KAIST 제공 MANO 손 피팅 코드 (HaMBA → MANO temporal fit) |
| `env/` | 개발 PC 의 env 별 `pip freeze` 스냅샷과 PhysTwin 원본 설치 스크립트 (참고용) |

## 2. 필요 환경

### 2.1 시스템
- Linux x86_64, NVIDIA GPU + CUDA 12.8 드라이버
- `ffmpeg`, `ffprobe`, `zip`, `script`(util-linux)
- 디스플레이가 없는 서버/코랩: **`xvfb`** (`apt install xvfb`).
  open3d 오프스크린 렌더링은 X 디스플레이 없이 실행하면 코어 덤프로 죽습니다.
  `bin/kp phase2` 는 `DISPLAY` 가 없으면 자동으로 `xvfb-run` 으로 감싸서 실행합니다.
- 디스크: 에피소드당 수 GB (1분 클립은 중간 산출물 `pcd/` 가 수십~100GB 까지 커졌다가 자동 삭제됨)

### 2.2 Python 환경 3개
단계마다 요구 버전이 달라 **환경 3개**가 필요합니다. 경로는 `config.env` 에 지정합니다.

| 설정 키 | 용도 | 개발 PC 기준 |
|---|---|---|
| `KP_PY_PHYSTWIN` | 전처리, CoTracker, TRELLIS, 3D 재구성, 납품 | Python 3.10, torch 2.7.0+cu128 |
| `KP_PY_SAM3` | SAM 3.1 세그멘테이션 | Python 3.12, torch 2.10.0+cu128 |
| `KP_PY_HAMBA` | HaMBA 손 복원 + MANO 피팅 | Python 3.10, torch 2.7.0+cu128 |
| `KP_PY_XLSX` (선택) | 데이터셋 메타데이터 xlsx | `openpyxl` 이 있는 아무 python |

주요 패키지 (개발 PC 에서 실제 동작한 버전 — 전체 목록은 `env/pip_freeze_*.txt`):

- **phystwin**: torch 2.7.0+cu128, torchvision 0.22.0, numpy 2.2.6, open3d 0.19.0, opencv-python 4.11,
  trimesh 4.10, kornia 0.8.2, pytorch3d 0.7.9, **TRELLIS 의존성**(spconv-cu120 2.3.6, flash_attn 2.7.4.post1,
  nvdiffrast 0.4.0, kaolin 0.18.0, diff_gaussian_rasterization, diffoctreerast, utils3d, rembg, transformers 4.46.3),
  imageio-ffmpeg. PhysTwin 원본 설치 절차는 `env/env_install.sh`, RTX 50 계열용 수정본은 `env/5090_env_install.sh`.
- **sam3**: torch 2.10.0+cu128, `sam3` (https://github.com/facebookresearch/sam3 , **`sam3.1` 브랜치**,
  개발 PC 커밋 `20dba30`), numpy 1.26.4, decord, hydra-core, huggingface_hub.
  flash_attn_3 는 쓰지 않습니다 (Blackwell 에서 FA3 비활성, 코드 기본값도 off).
- **hamba**: torch 2.7.0+cu128, detectron2 0.6, mmcv 1.3.9, pytorch3d 0.7.9, smplx 0.1.28, chumpy 0.71,
  mediapipe 0.10.14, pytorch-lightning 2.6.5, selective_scan(`Mono/data_process/Hamba/VMamba/kernels/selective_scan`
  에서 빌드), open3d 0.19.0. MANO 피팅(stage 2)도 이 env 에서 돕니다.

> **GPU 아키텍처 주의.** 개발 PC(RTX 5090, sm_120)의 CUDA 확장들(pytorch3d, detectron2, selective_scan,
> nvdiffrast, diff_gaussian_rasterization, simple_knn, diffoctreerast, flash_attn 2)은 **sm_120 전용으로 빌드**되어
> 있습니다. 다른 GPU(예: 코랩 T4 sm_75 / L4 sm_89 / A100 sm_80 / H100 sm_90)에서는 해당 GPU 에 맞게
> **소스에서 다시 빌드**해야 합니다 (`TORCH_CUDA_ARCH_LIST` 지정). 빌드가 안 맞으면 실행 시
> `no kernel image is available for execution on the device` 오류가 납니다. `bin/kp check` 가 hamba env 에서
> pytorch3d CUDA 커널을 실제로 한 번 실행해 이 문제를 미리 잡습니다.
>
> **GPU 메모리.** 개발 PC 는 32GB 에서 돌렸습니다. TRELLIS 형상 생성과 HaMBA 는 메모리를 많이 쓰므로
> 16GB 급(T4)에서는 부족할 수 있습니다.

### 2.3 외부 저장소 / 가중치

| 항목 | 받는 곳 | 두는 곳 |
|---|---|---|
| CoTracker3 코드 | `git clone https://github.com/facebookresearch/co-tracker` (개발 PC 커밋 `82e02e8`) | `config.env` 의 `KP_COTRACKER_REPO` |
| CoTracker3 가중치 | 첫 실행 때 자동 다운로드 (`facebook/cotracker3` `scaled_online.pth`) | torch hub 캐시 |
| SAM 3.1 가중치 | HF `facebook/sam3.1` — **접근 승인 필요**. 승인된 계정으로 `huggingface-cli login` | `HF_HOME` |
| TRELLIS 가중치 | HF `JeffreyXiang/TRELLIS-image-large` (자동 다운로드) | `HF_HOME` |
| SuperGlue 가중치 | 패키지에 포함 | `PhysTwin/data_process/models/weights/` |
| HaMBA 체크포인트 (8.4GB) | 사내 공유 (별도 전달) | `PhysTwin/Mono/data_process/Hamba/ckpts/hamba/checkpoints/hamba.ckpt` |
| ViTPose+ huge wholebody (3.6GB) | 사내 공유 (별도 전달) | `PhysTwin/Mono/data_process/Hamba/_DATA/vitpose_ckpts/vitpose+_huge/wholebody.pth` |
| ViTDet (detectron2) | 첫 실행 때 자동 다운로드 (dl.fbaipublicfiles.com) | torch/fvcore 캐시 |
| **MANO** `MANO_LEFT.pkl`, `MANO_RIGHT.pkl` | **직접 등록 후 다운로드**: https://mano.is.tue.mpg.de (라이선스상 재배포 불가라 패키지에 없음) | `PhysTwin/Mono/data_process/Hamba/_DATA/data/mano/` |

MANO 파일을 넣은 뒤 한 번 `bin/kp setup-mano` 를 실행해 `mano_faces.pkl` 을 만듭니다.

## 3. 설정

```bash
cp config.env.example config.env     # 경로 채우기
bin/kp check                         # 환경·가중치·에피소드 점검 (FAIL 이 없어야 진행)
```

### 3.1 에피소드–물체 매핑: `episodes.json`
`$KP_DATA_BASE/episodes.json` (또는 `EPISODES_JSON` 으로 지정한 파일) 하나가 모든 단계의 기준입니다.
**새 에피소드는 이 파일에 항목을 추가하는 것만으로 처리됩니다.** 코드 수정은 필요 없습니다.

```json
{
  "ep_20260826_083803": {
    "prompt": "blue cloth",
    "label": "cloth",
    "scene": "2d",
    "name": "blue cloth",
    "interaction": "fold, roll and unfold a blue cloth on a table by hand"
  }
}
```

| 필드 | 의미 |
|---|---|
| 키 | 에피소드(케이스) 폴더 이름 |
| `prompt` | SAM 3.1 물체 텍스트 프롬프트 |
| `label` | 물체 마스크 라벨 (`mask_info` 에 기록) |
| `scene` | `1d`(로프) / `2d`(천, 테이블 위) / `3d`(들어 올리는 입체물) — 손 프롬프트 프리셋 선택 |
| `name` | 납품 `object_name` (metadata.json, xlsx) |
| `interaction` | 납품 상호작용 설명 |

다섯 필드 모두 필수입니다. 하나라도 비면 실행 시작 시점에 어떤 에피소드의 어떤 필드인지 출력하고 멈춥니다.

### 3.2 샘플로 바로 돌려보기
`samples/` 에 수집 원본 에피소드 3개(물체별 1개, 4캠, 각 약 5초)와 그에 맞는 `episodes.json` 이 들어 있습니다.
세 에피소드는 같은 캘리브레이션(`extrinsics.json` 동일)으로 찍은 2026-08-26 배치입니다.

| 에피소드 | 물체 | 개발 PC 검증 |
|---|---|---|
| `ep_20260826_083803` | blue cloth | qc PASS → phase1 → phase2 OK (납품 zip 까지) |
| `ep_20260826_084721` | yellow sponge | qc PASS → phase1 |
| `ep_20260826_085016` | mouse pad | qc PASS → phase1 |

```bash
mkdir -p /path/to/data                              # = config.env 의 KP_DATA_BASE
cp samples/episodes.json /path/to/data/
bin/kp check
bin/kp ingest samples/raw/ep_20260826_083803 samples/raw/ep_20260826_084721 samples/raw/ep_20260826_085016
bin/kp qc ep_20260826_083803 --scene 2D             # 기대 결과: OVERALL PASS (세 샘플 모두)
bin/kp phase1                                       # 세 샘플 모두: 전처리 → trim → SAM
bin/kp phase2 ep_20260826_083803                    # 기대 결과: [phase2 OK], /path/to/data/ep_20260826_083803.zip
bin/kp phase2 --all                                 # 세 샘플 전부 (MANO 때문에 에피소드당 약 1시간)
```

## 4. 실행 순서

```bash
bin/kp ingest /path/to/raw/ep_XXXX [...]   # 수집 원본을 $KP_DATA_BASE 로 복사 (필수 파일 확인)
bin/kp qc ep_XXXX --scene 2D                # (선택) 수집 품질 검사 — PASS/WARN/FAIL
bin/kp phase1                               # episodes.json 전체: 전처리 → trim → SAM (완료분은 건너뜀)
bin/kp review ep_XXXX                       # 마스크 검수/수정 (GUI 필요, 디스플레이 있는 PC 에서)
bin/kp phase2 ep_XXXX [...]  | --all        # 후단 전체 → $KP_DATA_BASE/ep_XXXX.zip
bin/kp status                               # 에피소드별 진행 상태
```

- **검수 지점**: Phase 1 은 SAM 까지만 하고 멈춥니다. SAM 3.1 이 어려운 변형에서 완벽하지 않아서,
  검증되지 않은 마스크 위에서 비싼 후단을 돌리지 않도록 끊어 둔 구조입니다. 마스크가 틀리면 그 아래
  모든 산출물이 틀립니다. 편집기(`sam_mask_refine.py`)는 matplotlib GUI 라서 디스플레이가 필요하고,
  `config.env` 에 `KP_SAM3_CKPT`(facebook/sam3 체크포인트 파일)를 지정해야 합니다.
- 모든 단계는 **산출물이 있으면 건너뜁니다.** 중단 후 같은 명령을 다시 실행하면 이어서 진행합니다.
- `phase2` 는 끝나면 핵심 산출물(`final_data.pkl`, `final_data_hand.pkl`, `hand_mano_trajectory.pkl`,
  `metadata.json`, zip)을 확인해 `[phase2 OK]` / `[phase2 FAIL] ...없는 파일` 로 판정합니다.
  로그: `$KP_LOG_DIR/phase2_<ep>.log`
- `phase2` 는 원본 `cam_*` 파일을 `$KP_DATA_BASE/raw_archive/<ep>/` 로 **옮깁니다**(삭제 아님).
  `bin/kp qc` 는 이 위치도 찾아봅니다.
- `--keep`: 나중 분석용 중간 산출물(정합 메시 등)을 디스크에 남깁니다. zip 내용은 동일합니다.

## 5. 단계별 처리 내용

| 단계 | 스크립트 (`PhysTwin/`) | 내용 |
|---|---|---|
| 전처리 | `data_process/preprocess_rgbd.py` | 카메라 간 동기화(호스트 타임스탬프 기준, ±25ms 최근접), extrinsics(w2c, cam_1=월드 원점) → `calibrate.pkl`, 테이블 평면을 z=0 으로 월드 재정렬. 카메라들이 테이블 평면을 5° 넘게 다르게 보면 **중단**(캘리브레이션 불량) |
| trim | `data_process/trim_exposure.py` | 앞/뒤 노출 불량 프레임을 모든 카메라에 공통으로 잘라냄 |
| SAM | `data_process/segment_object_sam3.py` | 물체(id 0) = JSON `prompt`, 손(id 1) = `scene` 프리셋 (`2d`: "hand" − "human forearm") |
| CoTracker | `data_process/track_object.py` | 물체/손 2D 조밀 추적. 추적점이 없는 물체 표면이 드러나면 그 부분만 새로 시딩 |
| 형상 | `make_shape_rgba.py`, `shape_prior.py` | TRELLIS 로 물체 3D 형상 사전 |
| 3D 재구성 | `data_process_pcd.py` → `data_process_mask.py` → `data_process_track.py` | 4캠 RGB-D 융합, 마스크 필터, 추적점 3D 리프트 |
| 손 3D | `export_hand_track_data.py` | CoTracker 손을 물체와 같은 방식으로 3D 화 |
| 정합 | `align.py` | TRELLIS 메시를 SuperGlue 로 관측에 정합. **실패하면 그 에피소드를 중단**(`ALIGN_FAILED.txt`) — 메시 없이 진행하면 속이 빈 물리 모델이 되기 때문 |
| 물리 입력 | `data_process_sample.py` | `final_data.pkl` |
| MANO | `Mono/process_hand_data_ours.py` | HaMBA 프레임별 손 복원 → MANO 시간축 피팅 → `hand_v1/hand_reg.pkl` |
| 납품 | `finalize_delivery.py`, `export_final_hand*.py`, `viz_delivery.sh`, `export_dataset_metadata_xlsx.py` | 레이아웃 정리, MANO 정점 궤적, 손 파티클, 검증 영상, xlsx, zip |

### 납품 zip 레이아웃
```
cotracker/{cam}.npz                    물체 2D 추적 (T,N,2)[y,x] + visibility
cotracker/hand/{cam}.npz               손 2D 추적
track_process_data.pkl                 물체 3D 포인트클라우드 (T,N,3) + colors + vis
shape/object.glb, ...                  물체 3D 재구성 (TRELLIS 메시)
shape/hand/hand_pointcloud_3d.pkl      손 3D 포인트클라우드 (CoTracker)
shape/hand/hand_mano_trajectory.pkl    손 MANO 정점 궤적 (T,H,778,3)
final_data.pkl                         PhysTwin 물리 피팅 입력
final_data_hand.pkl                    손 파티클 (CoTracker)
final_data_hand_mano.pkl               손 파티클 (MANO)
metadata.json                          PhysTwin 필드 + object_name + interaction
mask/  color/  depth/  calibrate.pkl
```
모든 mp4 는 mpeg4 코덱으로 재인코딩됩니다 (수신 측 환경과 무관하게 디코딩되도록).

## 6. 수집 쪽 (참고 — 하드웨어 필요, 이 패키지로는 실행하지 않음)
수집은 사내 PhysicalAI 플랫폼(`PhysicalAI_data_code/Exo_video_data`)에서 합니다.
- 녹화: `realsense.py` — RealSense D455 ×4, color+depth 848×480@30fps, depth 는 color 에 정렬(`rs.align`),
  카메라별 `cam_N.mp4`, `cam_N_depth/frame_XXXXXX.npy`(uint16, mm), `cam_N_intrinsics.json`, `cam_N_timestamps.csv`.
  카메라는 **하드웨어 동기화되어 있지 않고**, 전처리에서 호스트 타임스탬프로 맞춥니다.
- 캘리브레이션: `calibration.py` — ChArUco 보드, `stereoCalibrate` 로 cam_1 기준 R,T (파일 의미: `X_cam = R·X_world + T`).
  녹화를 멈출 때 최신 `extrinsics.json` 이 에피소드 폴더로 복사됩니다.
- 수집 품질 검사: `capture_qc.py` — 이 패키지의 `capture/` 에 같은 코드가 있고 `bin/kp qc` 로 실행합니다.
  검사 항목: 프레임 연속성, 색상 품질, depth 완전성, 카메라 간 포인트클라우드 잔차, 월드 좌표계(테이블 평면), 동기화.

## 7. 알려진 제약
- `bin/kp qc` 의 "프레임 연속성"과 "동기화" 항목은 구조상 거의 항상 PASS 가 나옵니다(프레임 드롭이
  파일 인덱스에 흔적을 남기지 않고, 동기화는 호스트 도착 시각의 최근접 비교라서). 이 두 항목은 참고용으로만 보세요.
- MANO 단계(HaMBA)는 중간 결과를 코드 폴더 안에 씁니다: `PhysTwin/Mono/data_process/hand_recon_out/<ep>/`,
  `PhysTwin/debug/` (에피소드당 약 400MB). 처리 후 필요 없으면 지워도 됩니다.
- 데이터셋 메타데이터 xlsx 는 `$KP_DATA_BASE/dataset_metadata.xlsx` 에 에피소드마다 한 줄씩 추가/갱신됩니다.

## 8. 검증 내역
개발 PC(RTX 5090)에서 이 패키지만으로, **디스플레이 없이**(`DISPLAY` 해제 → Xvfb) 에피소드 1개를
원본 수집 폴더부터 납품 zip 까지 돌렸습니다. 원본 PhysTwin 저장소 경로는 전혀 참조하지 않았습니다.

| 단계 | 결과 | 소요 |
|---|---|---|
| `check` | all OK | — |
| `ingest` → `qc --scene 2D` | OVERALL PASS | 약 1분 |
| `phase1` (전처리 → trim 146→137프레임 → SAM) | 4캠 × 137프레임 마스크 | 약 3.5분 |
| `phase2` (CoTracker → TRELLIS → recon → align → final → MANO → 납품) | `[phase2 OK]`, zip 256MB | 약 53분 (MANO 43분) |

에피소드: `ep_20260826_083803` (blue cloth, 4캠, 137프레임). 산출물 파일 구성과 zip 파일 목록은 기존 납품본과 같았습니다
(포인트 개수는 기존 납품본이 사람이 수정한 마스크를 썼기 때문에 조금 다릅니다).
다른 환경에서의 설치(특히 CUDA 확장 빌드)는 검증되지 않았습니다.
