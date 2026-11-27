# SAM3 · MANO 변경 정리 (최초 커밋 → 현재)

- 기준: GitHub `DH82/Kaist` 최초 커밋 `a094179` (2026-11-13, *Initial import of KAIST PC stack*)
- 1차 정리: `928da04` (2026-11-17, *MANO fit: replay forward/backward as a CUDA graph*), 0~3절
- 추가 정리: `b308e83` (2026-11-27)까지, [4절](#4-11-18--11-27-변경)
- 작성일: 2026-11-17, 갱신: 2026-11-27

> 참고: 최초 커밋에는 파이프라인 원본(`pipeline/current/...`, `00_scripts`, `kaist-full`)이 들어 있지 않고,
> 어댑터 `worker/app/dags/kaist_runtime.py`가 원본 스크립트를 그대로 호출하는 구조뿐이었습니다.
> 파이프라인 원본은 바로 다음 커밋 `d7b808d`(같은 날)에서 원본 그대로 가져왔습니다.
> 그래서 아래 "최초" 상태는 **원본 스크립트를 그대로 호출하던 방식**을 뜻합니다.

---

## 0. 한눈에 보기

| 구분 | 최초 (`a094179`) | 11-17 (`928da04`) |
|---|---|---|
| SAM3 실행 | `05_sam3.py` CLI 호출 (프로젝트 전체 잠금, 카메라마다 모델 로드) | `sam3_episode.py` → `sam3_fast.py` (에피소드 단위 잠금, 단계별 모델 1회 로드) |
| SAM3 물체 지정 | 텍스트 프롬프트만 | `text` / `bbox_track` / `point_track` (첫 프레임 bbox·포인트 + 텍스트로 단일 물체 추적) |
| SAM3 소요 시간 | 98프레임 예시 437 s | 272 s (특징 재사용 테스트 옵션 켜면 211 s) |
| 마스크 검수 도구 | 수동 편집 | SAM3.1 마법봉(클릭 분할), 추적 일괄 교체(정·역방향 전파) |
| SAM3 대상 입력 | 실행 시작 시 반드시 입력 | 비워서 시작 가능 → `SAM3 설정 대기`에서 나중에 입력 |
| MANO 실행 | `18_fit_hands.py` CLI 호출 (원본 그대로) | 동일 진입점, 내부 `data_process_hands.py` 최적화(CUDA graph) |
| MANO 피팅 속도 | 프레임당 12.8–13.2 s (에피소드당 약 34분) | 프레임당 약 1.9 s (약 6.8배) |
| MANO 일괄 실행 | 실패 시 배치 전체 중단, 순서대로만 진행 | 실패 에피소드 건너뛰기, 평일 08:00 컷오프, 업데이트 전 대기열 3D 재복원 옵션, PC 간 이관 |
| MANO 실패 복구 | 수동 | 3D 계약 변경 감지 시 3D 복원부터 재시도, 단계 초기화(기본 "3D 복원까지만") |

11-27 현재(`b308e83`)는 위 표에서 다음이 더 바뀌었습니다(자세한 내용 4절).

| 구분 | 11-27 현재 |
|---|---|
| SAM3 손 지정 | 텍스트(기본) 또는 **첫 프레임 손 포인트 추적**, PC별 기본 분할 방식 |
| SAM3 동시 실행 | RTX 4090은 1개, RTX 5090은 2개(한 자리를 마법봉과 공유) |
| SAM3 카메라 2 손 | 180° 돌린 사본으로 추적 후 되돌림 |
| 처리 fps | 기본 15fps(30fps 선택 가능) |
| 실행 흐름 | 첫 실행은 추적 검수까지, **3D 복원 일괄 실행**이 3D 복원 → MANO → 저장, 평일 09:00 컷오프 |
| MANO 알고리즘 | `mano-geometric-shared-shape-v7` (손가락 시간 가중치 5e5→500, MediaPipe 크롭 재검출, 관절 ±4프레임 평활) |
| MANO 보정 실패 | 공유 shape 보정 프레임이 모자라면 다른 기준 카메라·MediaPipe 재검출로 구제(`shape_rescue`) |
| 손 형상 프로필 | 사람별 β 고정 기능 있음, **기본 꺼짐** |

---

## 1. SAM3

### 1.1 최초 상태

`kaist_runtime.py`의 `sam3` 단계:

```python
elif stage == 'sam3':
    changed = sam_confirmed and sam_settings_changed(...)
    cli('05_sam3.py', prepared, '--project', project, *(['--new-attempt'] if changed else []))
```

- 원본 `05_sam3.py` → `pipeline.main('05')`가 `99_history/pipeline.lock`(**프로젝트 전체 잠금**)을 잡음
  → 한 에피소드의 SAM3가 도는 동안 다른 에피소드의 SAM3·마스크 검수가 모두 실패.
- 기준선(baseline)과 손 ROI 단계 모두 **카메라마다 SAM3 프로세스를 새로 띄워** 모델을 에피소드당 8번(회당 약 8–17 s) 로드.
- 물체 분할은 텍스트 프롬프트 방식만 지원.

### 1.2 변경 이력

| 커밋 | 날짜 | 내용 |
|---|---|---|
| `d7b808d` | 11-13 | `sam3_episode.py` 추가: 같은 `pipeline.sam3()`를 **에피소드 단위 잠금**(`99_history/locks/<ep>.lock`)으로 실행 → 에피소드 2개 동시 실행 가능. `sam_object_track.py`(bbox 단일 물체 추적), `sam_wand_server.py`(마법봉) 추가. Docker용 `gpu/env/sam.lock` 고정 |
| `cbbca23` | 11-13 | `sam_object_point_track.py` 추가(포인트 포함/제외 + 텍스트 시드). DAG 파라미터 `sam_object_mode`(`text`/`bbox_track`/`point_track`), `sam_object_bboxes`, `sam_object_points` |
| `11e49bd` | 11-16 | 마스크 검수 **추적 일괄 교체**: 한 프레임 마법봉 결과를 시드로 SAM3 트래커를 정방향·역방향 전파해 대상(손=분홍, 물체=초록) 마스크를 교체. 이전 마스크는 `edit_history`에 한 이벤트로 보관. 백그라운드 작업 + 진행률 |
| `9aaa39f` | 11-16 | 추적 일괄 교체 **OOM 수정**: 역방향 전파에 `reverse=False`를 넘겨 특징이 해제되지 않던 문제(19.6 GB → OOM). 방향을 올바로 넘기고 방향마다 특징 캐시 정리 → 151프레임 17 s, 최대 7.1 GB. **SAM3 설정 대기**: 프롬프트 없이 RAW 전처리만 하고 `sam_settings_ready`에서 대기, UI에서 대상 입력 시 이어서 실행 |
| `426a190` | 11-17 | **SAM3 단계 속도 개선** (아래 1.3) |
| `087a5d7` | 11-17 | 마스크 검수: Enter로 브러시/지우개 편집 저장 |
| `6339f05` | 11-17 | **테스트 옵션: 이미지 인코더 출력 재사용** (아래 1.4) |

### 1.3 `426a190` — 모델 1회 로드 + bbox/point 모드는 기준선 손만

`sam3_episode.py`가 `pipeline.sam3()` 대신 어댑터 쪽 동등 구현 `dags/sam3_fast.py`를 실행합니다.

- **원본 `00_scripts`는 손대지 않음**: `00_scripts`의 모든 `.py`는 각 실행의 `identity.json`에 해시로 고정되어
  있어, 수정하면 진행 중이거나 MANO 대기 중인 실행이 모두 실패하기 때문.
- 기준선·ROI 단계를 **단계별로 SAM3 프로세스 1개**에서 모든 카메라 처리 → 모델 로드 8회 → 2회.
  카메라별 SAM 세션은 그대로이므로 마스크 동일. 카메라별 재개 기록(`baseline_camera_N` / `roi_camera_N`) 형식 유지.
- ROI 단계(`sam_roi_fast.py`): SAM이 도는 동안 다음 카메라 ROI 영상을 미리 준비(raw rgb24 파이프 → 무손실 `libx264rgb ultrafast`, 디코드 프레임 동일).
- 기준선 미리보기를 ROI 단계와 동시에 생성.
- `--object-tracked`(bbox/point 모드): 물체는 추적기가 따로 계산하므로 기준선은 **손 그룹만**(`--groups controller`) 계산.
  텍스트 모드 실행이 손만 계산된 기준선을 만나면 다시 계산.
- 물체 추적(`object_bbox_track`)도 `sam_object_multi.py`로 4개 카메라를 **predictor 1개**로 처리(고정된 추적기 파일은 그대로).
- 안전장치: 미러링한 파이프라인 파일의 sha256(`PINNED`)이 다르면 `compatible()`이 False → 원본 `pipeline.sam3()`로 자동 대체.
- 검증: `ep_20261116_065605`(98프레임, point_track) 사본에서 roi_candidate 손/물체 마스크 **픽셀 단위 동일**, 437 s → 272 s.

### 1.4 `6339f05` — 이미지 특징 재사용 (테스트 옵션, 기본 꺼짐)

- SAM3는 텍스트 프롬프트마다 세션 전체를 리셋해 손·팔·물체마다 모든 프레임을 다시 인코딩(한 프롬프트 시간의 약 70%).
- `sam_feature_reuse.py`: ViT trunk 출력을 GPU에 (영상, 프레임) 단위로 캐시하고, 이후 프롬프트에서는 넥·검출기·트래커만 다시 실행.
  - 상한 `KAIST_SAM_FEATURE_CACHE_GB`(기본 4), 남은 GPU 메모리 2 GB 미만이면 캐시 중단.
- 켜기: `<airflow>/config/sam_feature_reuse.on` 파일 존재 또는 `KAIST_SAM_FEATURE_REUSE=1` (`=0`이면 강제 끔).
- 기준선은 `sam_run_reuse.py`, ROI는 `sam_roi_fast.py --feature-reuse`로 실행. 완료 기록에 `feature_reuse_test`, 검수 페이지에 안내 표시.
- 검증: 마스크 픽셀 동일, 272 s → 211 s.

### 1.5 SAM3 관련 현재 파일

| 파일 | 역할 |
|---|---|
| `worker/app/dags/sam3_episode.py` | 에피소드 단위 잠금으로 SAM3 실행, fast/원본 선택 |
| `worker/app/dags/sam3_fast.py` | `pipeline.sam3()` 고속 동등 구현 |
| `worker/app/dags/sam_roi_fast.py` | 손 ROI 단계 일괄 실행 + 다음 카메라 영상 선준비 |
| `worker/app/dags/sam_object_track.py` | bbox + 텍스트 단일 물체 추적 |
| `worker/app/dags/sam_object_point_track.py` | 포인트 + 텍스트 단일 물체 추적 |
| `worker/app/dags/sam_object_multi.py` | 물체 추적기를 한 프로세스·predictor 1개로 카메라 순차 실행 |
| `worker/app/dags/sam_feature_reuse.py`, `sam_run_reuse.py` | 이미지 특징 재사용(테스트 옵션) |
| `worker/app/worker/sam_wand_server.py` | 마법봉 클릭 분할 / 추적 일괄 교체 상주 서버 |
| `worker/app/worker/mask_editor.py` | 추적 결과 적용(`track_source`/`apply_track`) |

---

## 2. MANO (손 복원·피팅)

### 2.1 최초 상태

```python
elif stage == 'hands':
    cli('18_fit_hands.py', recon, '--hand-side', 'right', '--no-view')
elif stage == 'validate':
    module(scripts / '18_fit_hands.py').verify(recon / 'hands')
```

- 18단계(HaMBA 손 복원) → 19단계(MANO 시간축 피팅, `data_process_hands.py`)를 원본 그대로 실행.
- 야간 일괄 실행(`poll_mano_batch`)은 `hands → validate → publish`를 선택 순서대로 진행하고,
  **한 에피소드라도 실패하면 배치 전체가 중단**.
- 실행 진입점(`18_fit_hands.py` 호출)은 지금도 그대로입니다. 바뀐 것은 실행 환경, 내부 피팅 속도, 일괄 실행·복구 흐름입니다.

### 2.2 변경 이력

| 커밋 | 날짜 | 내용 |
|---|---|---|
| `d7b808d` | 11-13 | Docker GPU 환경을 고정 버전으로 빌드(`gpu/env/hamba.lock`, `phystwin.lock`), 기존 가중치 마운트 |
| `cbbca23` | 11-13 | MANO 일괄 실행에 **평일 오전 08:00 컷오프**(시작 후 처음 오는 평일 08시 이후에는 새 에피소드 시작 안 함) |
| `00ce6fb` | 11-16 | **MANO 이관**(`adopt` 단계): 다른 PC의 작업 폴더를 NAS에서 가져와, 파이프라인 코드 해시가 같은지 확인 후 이 PC의 새 실행으로 고정 |
| `85f2ec4` | 11-16 | **18단계(HaMBA) 실패 수정**: VMamba CrossScan/CrossMerge Triton 커널이 C 컴파일러를 찾지 못함 → GPU 이미지에 gcc/g++/libc6-dev 설치 및 `config.env` 경로로 심볼릭 링크 |
| `5818013` | 11-16 | MANO/validate/publish가 "3D 입력/코드/설정 계약 변경"으로 실패하면 UI 재시도가 **3D 복원부터** 다시 시작(`--auto-new-attempt`, 이전 결과는 `99_history`에 보관) |
| `9e60e97` | 11-17 | **단계 초기화**(끝난 단계부터 다시 실행, 결과는 `99_history/reset/`로 이동·삭제 없음) + **MANO 대기 전체 실행** 버튼 |
| `a572827` | 11-17 | 단계 초기화 기본값 "3D 복원까지만": MANO 단계는 skipped 처리, `mano_deferred`에 기록되어 MANO 대기 목록으로 |
| `3c518f8` | 11-17 | MANO 일괄 실행: **실패 에피소드는 건너뛰고 다음 진행**, 끝에 목록 표시. GPU PC 연결 오류는 다음 확인 때 재시도 |
| `023c23f` | 11-17 | GPU 이미지: HaMBA를 자체 폴더에서만 빌드 → PhysTwin 스크립트 수정이 HaMBA 레이어·38 GB 환경 복사를 무효화하지 않음 |
| `d13b2b5` → `f6af772` | 11-17 | 업데이트 후 3D 자동 재복원을 넣었다가, 자동 재실행은 중단(수동 선택 방식으로 전환) |
| `2f2af93` | 11-17 | MANO 일괄 실행 옵션 **"업데이트 전 대기열은 3D 복원부터 다시"**(기본 켬, conf `redo_3d`) (아래 2.4) |
| `928da04` | 11-17 | **MANO 피팅 CUDA graph** (아래 2.3) |

### 2.3 `928da04` — MANO 피팅 CUDA graph (약 6.8배)

대상: `kaist-full/PhysTwin/Mono/data_process/data_process_hands.py` (파이프라인 원본 중 MANO 쪽에서 유일하게 수정된 파일)

- 문제: 19단계가 에피소드당 41분 중 약 34분. 프레임당 1500회 반복, 반복당 GPU 작업은 약 2 ms지만
  작은 커널 약 1,490개 실행 + 호스트 동기화 54회 → GPU 사용률 약 39%.
- 변경:
  - smplx `batch_rigid_transform`: CUDA 텐서로 Python 리스트를 인덱싱하던 부분을 한 번만 읽도록 변경(MANO forward당 동기화 15회 제거, 결과 비트 동일).
  - 마스크 정렬/커버 손실·투영: 모든 카메라를 배치 처리, 불리언 인덱싱·`int(x.sum())` 동기화 제거, 프레임 상수는 프레임당 1회 패킹.
  - forward+backward를 **프레임당 한 번 CUDA graph로 캡처한 뒤 재생**. 옵티마이저·LR 스케줄·단계별 gradient mask는 기존처럼 eager 유지.
  - 관통(penetration)·실루엣·ball-anchor·유효성 필터 경로는 기존 eager 루프 유지.
  - `KAIST_MANO_CUDA_GRAPH=0`이면 끔.
- 벤치마크(`ep_20261113_061926_1`, PC1 RTX 4090): 프레임당 12.8–13.2 s → **1.9 s**.
  40프레임에서 정점 차이 최대 0.16 mm(합산 순서 차이, 누적 없음). **양손 경로는 데이터가 없어 미검증.**
- 주의: 이 파일은 각 실행의 `identity.json`에 고정되어 있으므로, 배포 전에 대기열에 있던 실행은
  "업데이트 전 대기열은 3D 복원부터 다시" 옵션으로 돌려야 합니다.

### 2.4 `2f2af93` — 업데이트 전 대기열 3D 재복원

- 파이프라인 코드가 바뀌는 업데이트 후에는, 이전 코드로 3D가 고정된 실행이 MANO에서 "Code/config changed during run"으로 실패.
- 일괄 실행 시 실행별로 identity 고정값과 현재 코드를 비교(`drift`, 읽기 전용) →
  **코드가 바뀐 실행만** `prepare_reconstruction`부터 초기화해 3D 복원 후 MANO 진행, 나머지는 MANO만 실행.
- 설정(config) 변경이나 이관된 실행은 건너뜀. 이미 재복원 중인 에피소드는 08:00 이후에도 마저 끝내고,
  중단된 재복원은 다음 배치에서 이어감.

### 2.5 MANO 관련 현재 파일

| 파일 | 역할 |
|---|---|
| `worker/app/dags/kaist_runtime.py` | `hands`/`validate` 단계(원본 `18_fit_hands.py` 호출), `adopt`(이관) |
| `code/dags/direct_worker.py` | `poll_mano_batch`: 08:00 컷오프, 실패 건너뛰기, `redo_3d` |
| `code/dags/kaist_direct.py` | MANO 일괄 실행 DAG (`sources`, `redo_3d` 파라미터) |
| `worker/app/dags/stage_reset.py` | 단계 초기화 / 재고정 |
| `.../Mono/data_process/data_process_hands.py` | MANO 시간축 피팅 (CUDA graph) |
| `Dockerfile` | HaMBA 빌드 분리, Triton용 C 컴파일러 |

---

## 3. 공통 설계 원칙 (이번 변경들에서 지킨 것)

1. **원본 파이프라인 코드는 최소한으로 수정**: `00_scripts`·`data_process` 파일은 실행 identity에 해시로 고정되어 있어,
   SAM3 최적화는 전부 어댑터(`worker/app/dags/`)에 두었습니다. MANO 피팅만 원본 파일을 직접 고쳤고, 그래서 재고정 절차가 필요합니다.
2. **결과 동일성 검증**: SAM3 최적화는 마스크 픽셀 동일, MANO는 정점 오차 0.16 mm 이내를 확인한 뒤 반영했습니다.
3. **자동 대체 / 끄는 스위치**: `sam3_fast`는 원본이 바뀌면 원본 경로로 자동 대체되고, 특징 재사용은 플래그 파일로, CUDA graph는 `KAIST_MANO_CUDA_GRAPH=0`으로 끌 수 있습니다.
4. **삭제 대신 보관**: 재시도·초기화·재복원 때 기존 결과는 `99_history/`로 옮겨 둡니다.

---

## 4. 11-18 ~ 11-27 변경

### 4.1 SAM3

| 커밋 | 날짜 | 내용 |
|---|---|---|
| `c5153df` | 11-18 | **처리 fps 옵션**(기본 15fps / 30fps). 15fps면 RAW 전처리에서 동기화된 프레임을 하나 걸러 하나만 남겨 이후 모든 단계·NAS 결과가 15fps. 값은 실행 identity와 전처리 계약에 고정. 이전 실행은 키가 없어 30fps 그대로 |
| `37b5b0a` | 11-18 | SAM3 고속 경로가 현재 `pipeline.py` 해시를 고정(PINNED 갱신) |
| `32cf50e` | 11-19 | **손 포인트 추적 모드**(`물체 포인트 + 손 포인트`, `sam_hand_point_track.py`): 텍스트 기준선 + 손 ROI 대신 첫 프레임 클릭으로 손을 추적. 텍스트 "hand" 검출에서 클릭한 손을 고르고, 남은 클릭은 포인트 시드(화면 15% 초과면 거부). 여러 손을 한 번에 추적해 `mask/<cam>/1`로 합침. 단계 이름·재개 기록·검수 화면은 그대로. 물체 bbox/포인트 추적과 함께만 허용. **PC별 기본 분할 방식**(UI DB, `/api/sam-default`). PC1 검증: 117프레임 4캠 66 s, 텍스트 손 마스크 대비 IoU 0.87–0.94 |
| `0b7cc0d` | 11-19 | **RTX 4090은 SAM3 한 개씩**(다른 GPU 단계는 계속). `worker/gpu_slots.py` `acquire_sam` |
| `7a4ddef` | 11-19 | **RTX 5090**: SAM3 두 자리 중 하나를 마법봉과 공유(`acquire_wand`). 마법봉이 거절되면 10분간 그 자리를 비워 둠. `KAIST_SAM3_SLOTS`로 강제 지정(0=제한 없음) |
| `d26f40e` | 11-25 | 연속 모드에서 물체/손 대상 버튼 상시 표시: 4캠 모두 손 포인트가 있으면 손 포인트 모드, 없으면 손 텍스트. 기본값이 손 포인트인 PC는 필수 |
| `11222cc` | 11-26 | **카메라 2 손 180° 회전 추적**(`sam_hand_rotate.py`가 `sam_hand_point_track.py`를 감쌈, 원본은 해시 고정이라 그대로). 카메라 2는 팔이 아래에서 들어와 손가락이 위를 향하고, 이 방향에서 SAM3.1 추적 점수가 0 아래로 떨어져 손을 다시 못 잡음(11-25 촬영 149개 중 25개, 11-26 5개 중 3개). 돌린 사본으로 추적한 뒤 마스크를 되돌림(픽셀 치환, 보간 없음). `KAIST_HAND_ROTATE_CAMS`(기본 `1`, 빈 값=끔). 30개 시험: 손 프레임 1201 → 3317/3621, 정상 에피소드는 100% 유지 |

마스크 검수 화면(SAM3 결과를 사람이 고치는 곳)도 바뀌었습니다: 앞으로만 추적 교체(`c3da5d7`), 마법봉 클릭 속도·한 번에 저장(`c2f9330`),
작업자가 고르는 손/물체 마스크 색(`4d68703`), 마법봉 뒤 Enter 저장 수정·브러시 반경 기억(`f04e398`), 브라우저 대화상자 대신 화면 안 확인 창(`b308e83`).

### 4.2 실행 흐름 (3D 복원 일괄 실행)

| 커밋 | 날짜 | 내용 |
|---|---|---|
| `05cb3fa` | 11-18 | **첫 실행은 추적 검수까지**(`tracking_approved`에서 끝나고 `3D 복원 대기`). MANO 일괄 실행이 **3D 복원 일괄 실행**이 됨: 에피소드마다 `prepare_reconstruction → reconstruct → hands → validate → publish`, 끝난 단계는 건너뜀. DAG/API 이름(`kaist_pc1_mano_batch`, `/api/pc1/mano`)은 호환을 위해 유지 |
| `f018d7e`, `f4dc44b` | 11-18 | 업데이트 전 결과는 **항상** 3D부터 다시(옵션 `redo_3d`를 꺼도 무시 — 꺼 두면 MANO가 "Code/config changed"로 실패하기 때문) |
| `e0891a1` | 11-18 | 같은 데이터를 다시 읽고 검증하던 부분 제거(pcd 프레임 1회 로드, 18단계 HaMBA/MANO 재검증 생략). 결과 바이트 동일. 고정 파일이 바뀌어 그 전 실행은 3D부터 다시 |
| `1d1fdd9` | 11-18 | publish는 NAS 최종 결과 검증까지만 하고, 작업 폴더 미러링·로컬 삭제는 백그라운드 업로드(한 번에 하나, 자동 업로드 실패 시 3회 재시도)로 → 다음 에피소드가 약 22분 일찍 시작. 검수 대기·validate·publish·adopt는 GPU 자리를 잡지 않음 |
| `016718b` | 11-19 | 컷오프 **평일 08:00 → 09:00** |
| `d26f40e` | 11-25 | 3D 대기 목록 **⊘ 제거**: 작업 데이터(이 PC 작업 폴더 + NAS 작업 사본)를 바로 삭제하고 그 RAW를 모든 PC에서 제외. RAW 원본·저장된 결과는 남김 |

### 4.3 MANO

| 커밋 | 날짜 | 내용 |
|---|---|---|
| `cb2c5d2` | 11-19 | **공유 shape 보정 구제**: 한 손 피팅이 "Too few geometrically consistent calibration frames"로 멈추던 경우에만 다른 기준 카메라를 시도하고, 그래도 모자라면 승인된 손 마스크 주변 크롭에서 MediaPipe로 빠진 프레임을 다시 검출. 결정은 `hand_v1/shape_rescue.pkl`에 저장해 재개 때 그대로 적용. 이미 보정되던 에피소드는 변화 없음 |
| `c368deb` ~ `4d7a20e` | 11-26 | **사람별 손 형상 프로필**(MANO beta): 측정 도구, RAW별 지정(NAS `control/hand_profiles/`), `--shape_profile`로 β 고정 후 공유 shape 보정 생략(`mano-geometric-profile-shape-v*`), 기본 β vs 프로필 비교 화면 |
| `288f0d2` ~ `347c739` | 11-26 | 손가락 추적 평가 지표·자동 설정 탐색·실루엣 대 SAM3 손 마스크 IoU 평가(실험 전용, 운영 결과에 영향 없음) |
| `c527fb1` | 11-26 | **MANO v7**(`mano-geometric-shared-shape-v7`): 손가락 자세 시간 가중치 `pose_temp_weight` 5e5 → **500**, `--mp_crop`(전체 화면 MediaPipe가 놓친 오른손을 승인된 손 마스크 주변 2배 확대 크롭에서 다시 검출, 관절 80% 이상이 마스크 위일 때만 채택), `--mp_smooth 4`(검출 관절 ±4프레임 평균). `hand_v1/mediapipe_v7.json` 기록. 시험(TEST/Set): `ep_20261126_005323` 손끝 오차 6.2 → 5.0 px, SAM3 손 마스크 IoU 0.559 → 0.589, 떨림 0.88 → 0.46 mm; PC5 `ep_20261126_005859_1` 관통 68/133프레임 → 0. 실험 코드와 운영 코드 정점 차이 0.0 mm. 재검출로 약 10 s 증가 |
| `ce6f756` | 11-26 | 손 형상 프로필 **기본 꺼짐**: UI는 `KAIST_HAND_PROFILE_UI=1`일 때만, Worker는 설정에 `"hand_profiles": true`일 때만 적용. 없으면 에피소드마다 β 보정(v7 그대로) |
| `2604542` → `2198802` | 11-26 | MANO v8(다시점 삼각측량으로 첫 프레임 손 위치, 깊이 이미지 항)을 넣었다가 사용자 요청으로 **되돌림**. 현재 운영은 v7 |

v7은 알고리즘 설정이 바뀐 것이라(가중치·입력 2D 관절), v6 결과와는 결과가 다릅니다. v6로 고정된 실행은 3D 복원 일괄 실행에서 새 시도로 다시 피팅합니다.
[MANO_FIT_EQUIVALENCE_KO.md](MANO_FIT_EQUIVALENCE_KO.md)의 "알고리즘 동일" 결론은 v6(CUDA graph 전후) 비교에만 해당합니다.

알려진 약점(11-26 결과 774개 분석): MANO 손이 카메라 쪽으로 평균 약 4.5 cm 떠 있는 경우가 많고(v8은 이를 고치려던 시도), 결과 QC가 이를 걸러내지 못합니다.

### 4.4 관련 파일 (추가분)

| 파일 | 역할 |
|---|---|
| `worker/app/dags/sam_hand_point_track.py` | 첫 프레임 손 포인트 추적 |
| `worker/app/dags/sam_hand_rotate.py` | 지정 카메라를 180° 돌려 손 추적 후 되돌림 |
| `worker/app/worker/gpu_slots.py` | SAM3·마법봉 자리(`acquire_sam`, `acquire_wand`, `KAIST_SAM3_SLOTS`) |
| `code/dags/direct_worker.py` | 3D 복원 일괄 실행(`BATCH_STAGES`), 09:00 컷오프, 업데이트 전 결과 3D 재실행 |
| `pipeline/.../00_scripts/18_fit_hands.py` | MANO v7 설정, 프로필 옵션 |
| `pipeline/.../00_scripts/hand_profile.py` | 손 형상 프로필 만들기·검사 |
| `.../Mono/data_process/data_process_hands.py` | `--mp_crop`, `--mp_smooth`, `--shape_profile`, shape 보정 구제 |
| `code/platform/hand_shape.py`, `mano_compare.py` | MANO beta 화면·비교(기본 숨김) |
