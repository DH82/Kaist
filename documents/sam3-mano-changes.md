# SAM3 · MANO 변경 이력

- 범위: GitHub 최초 커밋 `a094179`(2026-11-13, *Initial import of KAIST PC stack*) → 2026-11-27
- 최초 커밋에는 파이프라인 원본(`pipeline/current/…`, `00_scripts`, `kaist-full`)이 없고, 어댑터 `worker/app/dags/kaist_runtime.py`가 원본 스크립트를 그대로 호출하는 구조만 있었습니다. 원본은 같은 날 `d7b808d`에서 그대로 가져왔습니다. 아래 "최초"는 **원본 스크립트를 그대로 호출하던 방식**을 뜻합니다.
- MANO 피팅 결과가 버전 사이에 어떻게 달라졌는지는 [mano-fit-verification.md](mano-fit-verification.md)에 따로 정리했습니다.

## 1. 한눈에 보기

| 구분 | 최초 (`a094179`) | 현재 (2026-11-27) |
|---|---|---|
| SAM3 실행 | `05_sam3.py` CLI (프로젝트 전체 잠금, 카메라마다 모델 로드) | `sam3_episode.py` → `sam3_fast.py` (에피소드 단위 잠금, 단계별 모델 1회 로드) |
| SAM3 물체 지정 | 텍스트 프롬프트만 | `text` / `bbox_track` / `point_track` (첫 프레임 bbox·포인트 + 텍스트로 단일 물체 추적) |
| SAM3 손 지정 | 텍스트 | 텍스트(기본) 또는 첫 프레임 손 포인트 추적. PC별 기본 분할 방식 |
| SAM3 카메라 2 손 | — | 180° 돌린 사본으로 추적한 뒤 되돌림 |
| SAM3 소요 시간 | 98프레임 예시 437 s | 272 s (특징 재사용 시험 옵션 211 s) |
| SAM3 동시 실행 | 제한 없음 | RTX 4090 1개, RTX 5090 2개(한 자리를 마법봉과 공유) |
| 마스크 검수 도구 | 수동 편집 | SAM3.1 마법봉, 추적 일괄 교체(앞으로만/양방향), 작업자별 마스크 색 |
| 처리 fps | 30fps | 기본 15fps(30fps 선택 가능) |
| 실행 흐름 | 한 번에 끝까지 | 첫 실행은 추적 검수까지 → **3D 복원 일괄 실행**이 3D 복원 → MANO → 저장, 평일 09:00 이후 새 에피소드 시작 안 함 |
| 일괄 실행 실패 | 한 에피소드 실패 시 전체 중단 | 실패 에피소드만 건너뜀, 업데이트 전 결과는 항상 3D부터 다시, PC 간 이관 |
| MANO 실행 | `18_fit_hands.py` CLI (원본 그대로) | 같은 진입점, `data_process_hands.py` 최적화(CUDA graph) |
| MANO 피팅 속도 | 프레임당 12.8–13.2 s (에피소드당 약 34분) | 프레임당 약 1.9 s (약 6.8배) |
| MANO 알고리즘 | `mano-geometric-shared-shape-v6` | `…-v7` (손가락 시간 가중치 5e5→500, MediaPipe 크롭 재검출, 관절 ±4프레임 평활) |
| MANO 보정 실패 | 멈춤 | 다른 기준 카메라·MediaPipe 재검출로 구제(`shape_rescue`) |
| 손 형상 프로필 | — | 사람별 β 고정 기능 있음, 기본 꺼짐 |

## 2. SAM3

### 2.1 최초 상태

```python
elif stage == 'sam3':
    changed = sam_confirmed and sam_settings_changed(...)
    cli('05_sam3.py', prepared, '--project', project, *(['--new-attempt'] if changed else []))
```

- `05_sam3.py` → `pipeline.main('05')`가 `99_history/pipeline.lock`(**프로젝트 전체 잠금**)을 잡아, SAM3가 도는 동안 다른 에피소드의 SAM3·마스크 검수가 모두 실패.
- 기준선·손 ROI 단계 모두 카메라마다 SAM3 프로세스를 새로 띄워 모델을 에피소드당 8번(회당 약 8–17 s) 로드.
- 물체 분할은 텍스트 프롬프트만.

### 2.2 변경 이력

| 커밋 | 날짜 | 내용 |
|---|---|---|
| `d7b808d` | 11-13 | `sam3_episode.py`: 같은 `pipeline.sam3()`를 **에피소드 단위 잠금**(`99_history/locks/<ep>.lock`)으로 실행 → 에피소드 2개 동시 실행. `sam_object_track.py`(bbox 단일 물체 추적), `sam_wand_server.py`(마법봉). `gpu/env/sam.lock` 고정 |
| `cbbca23` | 11-13 | `sam_object_point_track.py`(포인트 포함/제외 + 텍스트 시드). DAG 파라미터 `sam_object_mode`(`text`/`bbox_track`/`point_track`), `sam_object_bboxes`, `sam_object_points` |
| `11e49bd` | 11-16 | 마스크 검수 **추적 일괄 교체**: 한 프레임 마법봉 결과를 시드로 정·역방향 전파해 대상 마스크 교체. 이전 마스크는 `edit_history`에 보관 |
| `9aaa39f` | 11-16 | 추적 일괄 교체 OOM 수정(역방향 전파 방향 인자, 151프레임 17 s·최대 7.1 GB). **SAM3 설정 대기**: 프롬프트 없이 전처리만 하고 `sam_settings_ready`에서 대기 |
| `426a190` | 11-17 | **SAM3 단계 속도 개선** (2.3) |
| `087a5d7` | 11-17 | 마스크 검수: Enter로 브러시/지우개 편집 저장 |
| `6339f05` | 11-17 | **이미지 특징 재사용** 시험 옵션 (2.4) |
| `c5153df` | 11-18 | **처리 fps 옵션**(기본 15 / 30). 15fps면 전처리에서 동기화된 프레임을 하나 걸러 하나만 남겨 이후 모든 단계·NAS 결과가 15fps. 값은 실행 identity와 전처리 계약에 고정. 이전 실행은 30fps 그대로 |
| `c3da5d7` | 11-18 | 마스크 검수: 현재 프레임부터 **앞으로만** 추적 교체 |
| `37b5b0a` | 11-18 | 고속 경로가 현재 `pipeline.py` 해시를 고정(`PINNED` 갱신) |
| `32cf50e` | 11-19 | **손 포인트 추적 모드**, PC별 기본 분할 방식 (2.5) |
| `0b7cc0d` | 11-19 | **RTX 4090은 SAM3 한 개씩**(다른 GPU 단계는 계속). `gpu_slots.acquire_sam` |
| `7a4ddef` | 11-19 | **RTX 5090**: SAM3 두 자리 중 하나를 마법봉과 공유(`acquire_wand`). 마법봉이 거절되면 10분간 그 자리를 비워 둠. `KAIST_SAM3_SLOTS`로 강제 지정 |
| `c2f9330` | 11-19 | 마법봉 클릭 속도, 한 번 요청으로 저장, 빈 마스크 일괄 조회 |
| `d26f40e` | 11-25 | 연속 모드에서 물체/손 대상 버튼 상시 표시: 4캠 모두 손 포인트가 있으면 손 포인트 모드, 없으면 손 텍스트 |
| `11222cc` | 11-26 | **카메라 2 손 180° 회전 추적** (2.6) |
| `4d68703`, `f04e398` | 11-26 | 작업자가 고르는 손/물체 마스크 색, 마법봉 뒤 Enter 저장 수정, 브러시 반경 기억 |
| `b308e83` | 11-27 | 브라우저 대화상자 대신 화면 안 확인 창(크롬이 대화상자를 막으면 버튼이 무반응이던 문제) |

### 2.3 `426a190` — 모델 1회 로드, bbox/point 모드는 기준선 손만

`sam3_episode.py`가 `pipeline.sam3()` 대신 어댑터 쪽 동등 구현 `dags/sam3_fast.py`를 실행합니다.

- 원본 `00_scripts`는 고치지 않음: 모든 `.py`가 실행 `identity.json`에 해시로 고정되어, 고치면 진행 중·MANO 대기 실행이 모두 실패하기 때문.
- 기준선·ROI 단계를 **단계별 SAM3 프로세스 1개**에서 모든 카메라 처리 → 모델 로드 8회 → 2회. 카메라별 세션은 그대로라 마스크 동일, 재개 기록 형식 유지.
- ROI 단계(`sam_roi_fast.py`)는 SAM이 도는 동안 다음 카메라 영상을 미리 준비(무손실 `libx264rgb`, 디코드 프레임 동일). 기준선 미리보기를 ROI 단계와 동시에 생성.
- bbox/point 모드에서는 물체를 추적기가 따로 계산하므로 기준선은 **손 그룹만** 계산. 물체 추적도 `sam_object_multi.py`로 4캠을 predictor 1개로 처리.
- 안전장치: 미러링한 파이프라인 파일의 sha256(`PINNED`)이 다르면 원본 `pipeline.sam3()`로 자동 대체.
- 검증: `ep_20261116_065605`(98프레임) 손/물체 마스크 픽셀 단위 동일, 437 s → 272 s.

### 2.4 `6339f05` — 이미지 특징 재사용 (시험 옵션, 기본 꺼짐)

- SAM3는 텍스트 프롬프트마다 세션을 리셋해 손·팔·물체마다 모든 프레임을 다시 인코딩(한 프롬프트 시간의 약 70%).
- `sam_feature_reuse.py`: ViT trunk 출력을 GPU에 (영상, 프레임) 단위로 캐시하고 이후 프롬프트는 넥·검출기·트래커만 실행. 상한 `KAIST_SAM_FEATURE_CACHE_GB`(기본 4), 여유 GPU 메모리 2 GB 미만이면 캐시 중단.
- 켜기: Worker 설정 폴더의 `sam_feature_reuse.on` 또는 `KAIST_SAM_FEATURE_REUSE=1`(`=0`은 강제 끔). 완료 기록에 `feature_reuse_test`.
- 검증: 마스크 픽셀 동일, 272 s → 211 s.

### 2.5 `32cf50e` — 손 포인트 추적 모드

- `물체 포인트 + 손 포인트`: 텍스트 기준선 + 손 ROI 대신 첫 프레임 클릭으로 손을 추적(`sam_hand_point_track.py`). 텍스트 "hand" 검출 중 클릭한 손을 고르고, 남은 클릭은 포인트 시드(화면 15% 초과면 거부). 여러 손을 한 번에 추적해 `mask/<cam>/1`로 합침.
- 단계 이름·재개 기록·검수 화면은 그대로. 물체 bbox/포인트 추적과 함께만 허용. 이 모드의 키와 추적기 해시는 사용할 때만 identity에 들어감.
- PC별 기본 분할 방식은 UI DB(`/api/sam-default`)에 저장.
- 검증(PC1, 117프레임): 4캠 66 s, 텍스트 손 마스크 대비 IoU 0.87–0.94, 빈 프레임 없음.

### 2.6 `11222cc` — 카메라 2 손 180° 회전 추적

- 카메라 2는 팔이 아래에서 들어와 손가락이 위를 향합니다. 이 방향에서 SAM3.1 추적 점수가 0 아래로 떨어져 손을 다시 잡지 못했습니다(11-25 촬영 149개 중 25개, 11-26 5개 중 3개).
- `sam_hand_rotate.py`가 `sam_hand_point_track.py`(해시 고정이라 그대로)를 감싸, 돌린 사본으로 추적하고 마스크를 되돌립니다(픽셀 치환, 보간 없음). 대상 카메라는 `KAIST_HAND_ROTATE_CAMS`(기본 `1`, 빈 값 = 끔).
- 30개 시험: 손 프레임 1201 → 3317/3621, 정상 에피소드는 100% 유지, 운영 마스크 대비 IoU 0.91–0.97.

## 3. 실행 흐름 (3D 복원 일괄 실행)

| 커밋 | 날짜 | 내용 |
|---|---|---|
| `cbbca23` | 11-13 | 야간 일괄 실행에 평일 오전 컷오프(시작 뒤 처음 오는 평일 08:00 이후 새 에피소드 시작 안 함) |
| `00ce6fb` | 11-16 | **MANO 이관**(`adopt`): 다른 PC 작업 폴더를 NAS에서 가져와 코드 해시가 같은지 확인 후 이 PC의 새 실행으로 고정 |
| `5818013` | 11-16 | MANO·검사·저장이 "3D 입력/코드/설정 계약 변경"으로 실패하면 재시도를 3D 복원부터(`--auto-new-attempt`, 이전 결과는 `99_history`) |
| `9e60e97`, `a572827` | 11-17 | **단계 초기화**(끝난 단계부터 다시, 결과는 `99_history/reset/`로 이동). 기본값 "3D 복원까지만": MANO는 skipped 처리하고 대기 목록으로 |
| `3c518f8` | 11-17 | 일괄 실행: **실패 에피소드는 건너뛰고 다음 진행**. GPU PC 연결 오류는 다음 확인 때 재시도 |
| `d13b2b5` → `f6af772` | 11-17 | 업데이트 뒤 3D 자동 재복원을 넣었다가 수동 선택 방식으로 되돌림 |
| `2f2af93` | 11-17 | "업데이트 전 대기열은 3D 복원부터 다시": 실행별 identity 고정값과 현재 코드를 비교(`drift`)해 코드가 바뀐 실행만 `prepare_reconstruction`부터 초기화 |
| `05cb3fa` | 11-18 | **첫 실행은 추적 검수까지**(`tracking_approved`에서 끝나고 `3D 복원 대기`). MANO 일괄 실행이 **3D 복원 일괄 실행**이 됨: 에피소드마다 `prepare_reconstruction → reconstruct → hands → validate → publish`, 끝난 단계는 건너뜀. DAG/API 이름(`kaist_pc1_mano_batch`, `/api/pc1/mano`)은 호환을 위해 유지 |
| `f018d7e`, `f4dc44b` | 11-18 | 업데이트 전 결과는 **항상** 3D부터 다시(옵션 `redo_3d`를 꺼도 무시 — 그대로 MANO만 하면 "Code/config changed"로 실패) |
| `e0891a1` | 11-18 | 같은 데이터를 다시 읽고 검증하던 부분 제거(결과 바이트 동일). 고정 파일이 바뀌어 그 전 실행은 3D부터 다시 |
| `1d1fdd9` | 11-18 | 저장(publish)은 NAS 최종 결과 검증까지만 하고 작업 폴더 미러링·로컬 삭제는 백그라운드 업로드로 → 다음 에피소드가 약 22분 일찍 시작. 검수 대기·검사·저장·이관은 GPU 자리를 잡지 않음 |
| `016718b` | 11-19 | 컷오프 **평일 08:00 → 09:00**. 업로드 제한 시간대를 `HH:MM`으로 |
| `d26f40e` | 11-25 | 3D 대기 목록 **⊘ 제거**: 작업 데이터(이 PC 작업 폴더 + NAS 작업 사본) 바로 삭제, 그 RAW는 모든 PC에서 제외. RAW 원본·저장된 결과는 남김 |

## 4. MANO (손 복원·피팅)

### 4.1 최초 상태

```python
elif stage == 'hands':
    cli('18_fit_hands.py', recon, '--hand-side', 'right', '--no-view')
elif stage == 'validate':
    module(scripts / '18_fit_hands.py').verify(recon / 'hands')
```

18단계(HaMBA 손 복원) → 19단계(MANO 시간축 피팅, `data_process_hands.py`)를 원본 그대로 실행. 이 진입점은 지금도 같고, 바뀐 것은 실행 환경·피팅 내부·일괄 실행 흐름입니다.

### 4.2 변경 이력

| 커밋 | 날짜 | 내용 |
|---|---|---|
| `d7b808d` | 11-13 | Docker GPU 환경을 고정 버전으로 빌드(`gpu/env/hamba.lock`, `phystwin.lock`), weight는 마운트 |
| `85f2ec4` | 11-16 | 18단계(HaMBA) 실패 수정: VMamba Triton 커널이 C 컴파일러를 못 찾음 → 이미지에 gcc/g++/libc6-dev |
| `023c23f` | 11-17 | HaMBA를 자체 폴더에서만 빌드 → PhysTwin 스크립트 수정이 HaMBA 층·38 GB 환경 복사를 다시 만들지 않음 |
| `928da04` | 11-17 | **MANO 피팅 CUDA graph** (4.3) |
| `cb2c5d2` | 11-19 | **공유 shape 보정 구제**: 한 손 피팅이 "Too few geometrically consistent calibration frames"로 멈추던 경우에만 다른 기준 카메라를 시도하고, 그래도 모자라면 승인된 손 마스크 주변 크롭에서 MediaPipe로 다시 검출. 결정은 `hand_v1/shape_rescue.pkl`에 저장. 이미 보정되던 에피소드는 변화 없음 |
| `c368deb` ~ `4d7a20e` | 11-26 | **사람별 손 형상 프로필**(MANO beta): 측정 도구, RAW별 지정(NAS `control/hand_profiles/`), `--shape_profile`로 β 고정, 기본 β와 비교 화면 |
| `288f0d2` ~ `347c739` | 11-26 | 손가락 추적 평가 지표·자동 설정 탐색·실루엣 대 SAM3 손 마스크 IoU 평가(실험 전용, 운영 결과 영향 없음) |
| `c527fb1` | 11-26 | **MANO v7** (4.4) |
| `ce6f756` | 11-26 | 손 형상 프로필 **기본 꺼짐**: UI는 `KAIST_HAND_PROFILE_UI=1`, Worker는 설정 `"hand_profiles": true`일 때만 |
| `2604542` → `2198802` | 11-26 | MANO v8(다시점 삼각측량으로 첫 프레임 손 위치, 깊이 이미지 항)을 넣었다가 사용자 요청으로 **되돌림**. 현재 운영은 v7 |

### 4.3 `928da04` — MANO 피팅 CUDA graph (약 6.8배)

대상: `kaist-full/PhysTwin/Mono/data_process/data_process_hands.py`

- 문제: 19단계가 에피소드당 41분 중 약 34분. 프레임당 1500회 반복, 반복당 GPU 작업은 약 2 ms인데 작은 커널 약 1,490개 + 호스트 동기화 54회로 GPU 사용률 약 39%.
- 변경:
  - smplx `batch_rigid_transform`: 부모 관절 인덱스를 한 번만 읽음(동기화 15회 제거, 결과 비트 동일).
  - 마스크 정렬/커버 손실·투영을 전 카메라 일괄 계산, 불리언 인덱싱·`int(x.sum())` 동기화 제거, 프레임 상수는 프레임당 1회 준비.
  - forward+backward를 프레임당 한 번 CUDA graph로 캡처해 재생. 옵티마이저·LR 스케줄·단계별 gradient mask는 그대로 eager.
  - 관통·실루엣·ball-anchor·유효성 필터 경로는 eager 유지. `KAIST_MANO_CUDA_GRAPH=0`이면 끔.
- 결과(`ep_20261113_061926_1`, RTX 4090): 프레임당 12.8–13.2 s → 1.9 s. 40프레임에서 정점 차이 최대 0.16 mm, 누적 없음. 양손 경로는 데이터가 없어 미검증.

### 4.4 `c527fb1` — MANO v7 (손가락이 영상을 따라감)

- 알고리즘 `mano-geometric-shared-shape-v7`(프로필 사용 시 `mano-geometric-profile-shape-v2`).
- 손가락 자세 시간 가중치 `pose_temp_weight` 5e5 → **500**.
- `--mp_crop`: 전체 화면 MediaPipe가 놓친 오른손을 승인된 손 마스크 주변 2배 확대 크롭에서 다시 검출(관절 80% 이상이 마스크 위일 때만 채택).
- `--mp_smooth 4`: 검출 관절을 ±4프레임 평균. 기록은 `hand_v1/mediapipe_v7.json`.
- 시험(TEST/Set): `ep_20261126_005323` 손끝 오차 6.2 → 5.0 px, SAM3 손 마스크 IoU 0.559 → 0.589, 떨림 0.88 → 0.46 mm. PC5 `ep_20261126_005859_1` 관통 68/133프레임 → 0. 실험 코드와 운영 코드의 정점 차이 0.0 mm. 재검출로 약 10 s 증가.
- v6로 고정된 실행은 설정이 달라 MANO 단계에서 새 시도로 다시 피팅합니다.

### 4.5 알려진 약점 (11-26 결과 774개 분석)

- MANO 손이 카메라 쪽으로 평균 약 4.5 cm 떠 있는 경우가 많습니다(v8은 이것을 고치려던 시도였고 되돌림).
- 결과 QC가 이를 걸러내지 못합니다.

## 5. 관련 파일

| 파일 | 역할 |
|---|---|
| `worker/app/dags/sam3_episode.py` | 에피소드 단위 잠금으로 SAM3 실행, 고속/원본 선택 |
| `worker/app/dags/sam3_fast.py` | `pipeline.sam3()` 고속 동등 구현 |
| `worker/app/dags/sam_roi_fast.py` | 손 ROI 단계 일괄 실행 + 다음 카메라 영상 선준비 |
| `worker/app/dags/sam_object_track.py`, `sam_object_point_track.py`, `sam_object_multi.py` | bbox·포인트 + 텍스트 단일 물체 추적, 4캠을 predictor 1개로 |
| `worker/app/dags/sam_hand_point_track.py`, `sam_hand_rotate.py` | 첫 프레임 손 포인트 추적, 지정 카메라 180° 회전 |
| `worker/app/dags/sam_feature_reuse.py`, `sam_run_reuse.py` | 이미지 특징 재사용(시험 옵션) |
| `worker/app/worker/sam_wand_server.py`, `mask_editor.py` | 마법봉·추적 일괄 교체 상주 서버, 추적 결과 적용 |
| `worker/app/worker/gpu_slots.py` | SAM3·마법봉 자리(`acquire_sam`, `acquire_wand`, `KAIST_SAM3_SLOTS`) |
| `worker/app/dags/kaist_runtime.py` | `hands`/`validate` 단계(원본 `18_fit_hands.py` 호출), `adopt`(이관) |
| `worker/app/dags/stage_reset.py` | 단계 초기화·재고정 |
| `code/dags/direct_worker.py`, `kaist_direct.py` | 3D 복원 일괄 실행(`BATCH_STAGES`), 09:00 컷오프, 업데이트 전 결과 3D 재실행 |
| `pipeline/current/project/00_scripts/18_fit_hands.py`, `hand_profile.py` | MANO v7 설정, 손 형상 프로필 |
| `…/Mono/data_process/data_process_hands.py` | MANO 피팅(CUDA graph, `--mp_crop`, `--mp_smooth`, `--shape_profile`, shape 보정 구제) |
| `code/platform/hand_shape.py`, `mano_compare.py` | MANO beta 화면·비교(기본 숨김) |

## 6. 공통 설계 원칙

1. **원본 파이프라인 코드는 최소한으로 수정**: `00_scripts`·`data_process`는 실행 identity에 해시로 고정되어 있어 SAM3 최적화는 어댑터(`worker/app/dags/`)에 두었습니다. MANO 피팅처럼 원본을 고친 경우에는 업데이트 전 실행을 3D부터 다시 하거나 재고정합니다.
2. **결과 동일성 확인 후 반영**: SAM3 최적화는 마스크 픽셀 동일, CUDA graph는 정점 오차 0.16 mm 이내, v7은 실험 코드와 0.0 mm를 확인했습니다.
3. **자동 대체·끄는 스위치**: `sam3_fast`는 원본이 바뀌면 원본 경로로 대체, 특징 재사용은 플래그 파일, CUDA graph는 `KAIST_MANO_CUDA_GRAPH=0`, 카메라 회전은 `KAIST_HAND_ROTATE_CAMS=`(빈 값)로 끕니다.
4. **삭제 대신 보관**: 재시도·초기화·재복원 때 기존 결과는 `99_history/`로 옮겨 둡니다.
