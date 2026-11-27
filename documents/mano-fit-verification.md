# MANO 피팅 버전별 결과 비교

MANO 피팅(19단계, `data_process_hands.py`)이 버전마다 무엇이 바뀌었고 결과가 얼마나 달라지는지 정리했습니다.
피팅 본체는 `pipeline/current/workspace/kaist-full/PhysTwin/Mono/data_process/data_process_hands.py`, 이를 부르는 스크립트는 `pipeline/current/project/00_scripts/18_fit_hands.py`입니다.

## 1. 요약

| 버전 | 커밋 | 알고리즘 | 초기 버전 대비 결과 |
|---|---|---|---|
| 초기 | `d7b808d` (11-13) | `mano-geometric-shared-shape-v6` | — |
| CUDA graph | `928da04` (11-17) | v6 그대로 | 계산 순서만 달라짐. 정점 차이 **최대 0.16 mm**, 누적 없음 (2절) |
| 보정 구제 | `cb2c5d2` (11-19) | v6 그대로 | 예전에 **실패하던** 에피소드에만 결과가 생김. 보정되던 에피소드는 그대로 |
| **v7 (현재 운영)** | `c527fb1` (11-26) | `mano-geometric-shared-shape-v7` | 설정이 바뀌어 **모든 에피소드 결과가 달라짐**. 손가락이 영상을 따라감 (3절) |
| v8 | `2604542` → `2198802` (11-26) | 되돌림 | 운영 결과에 영향 없음 |

손 형상 프로필(사람별 β 고정, `ce6f756`)은 기본 꺼짐이며, 켜면 알고리즘 이름이 `mano-geometric-profile-shape-v*`가 됩니다.

## 2. CUDA graph (`928da04`): 알고리즘은 같고 계산 순서만 다름

### 2.1 결론

- 반복 횟수, 옵티마이저, 학습률 스케줄, 단계별 피팅 순서, 손실 항과 가중치, 실행 인자가 모두 그대로입니다.
- 결과는 비트 단위로 같지 않습니다. 실측 차이는 정점 기준 최대 0.16 mm이고 프레임이 지나도 누적되지 않았습니다. 같은 수식을 더하는 순서가 달라져 생긴 부동소수점 반올림 차이입니다.
- 실측 범위: 에피소드 1개(`ep_20261113_061926_1`), 40프레임, 한 손, PC1(RTX 4090), 2026-11-17. 속도는 프레임당 12.8–13.2 s → 1.9 s.

### 2.2 바뀐 파일이 하나뿐이라는 근거

`d7b808d`와 `928da04` 사이에 MANO 피팅 경로에서 바뀐 파일은 `data_process_hands.py` 하나입니다(204줄 추가, 54줄 삭제). 아래 파일은 그 사이 이력이 없습니다.

- `00_scripts/18_fit_hands.py`(실행 인자·설정), `00_scripts/hand_stage_worker.py`
- `Mono/process_hand_data_ours.py`, `Mono/data_process/Hamba/script_hand_recon.py`, `Mono/data_process/models/mano_wrapper.py`

```bash
git log --oneline --follow -- pipeline/current/workspace/kaist-full/PhysTwin/Mono/data_process/data_process_hands.py
git diff d7b808d 928da04 --stat -- pipeline/current/workspace/kaist-full/PhysTwin/Mono pipeline/current/project/00_scripts
```

두 번째 명령에 나오는 `10_reconstruct.py`는 3D 복원 재실행 판단(`--auto-new-attempt`) 변경이라 MANO 계산과 무관합니다.

### 2.3 그대로인 것

| 항목 | 값 |
|---|---|
| 프레임당 반복 / seed / 카메라 수 | 1500 / 1027 / 4 |
| temp_weight / pose_temp_weight | 500000 / 500000 (v7에서 pose_temp_weight만 500) |
| hamba_weight / wrist_weight / w_pose_prior | 0.01 / 0.1 / 25.0 |
| w_mask / w_mask_cover / cover_max_px | 1.0 / 1.0 / 40.0 |
| robust_joint_delta | 5.0 |
| staged_fit / camera_init_iters | 켬 / 300 |
| shared_shape | 켬 (frame 12개, 3000회, lr 1e-3) |
| fit_shape / w_silhouette | 끔 / 0.0 |

최적화 절차도 그대로입니다: AdamW lr 2e-3, StepLR(40회마다 0.98배, 2.5e-4 아래로 안 내림), 1500회 고정(조기 종료 없음), 처음 10%는 전역 자세만 → 25%까지 일부 관절 → 그 뒤 전체, 공유 shape 보정·카메라 초기화·프레임 간 초기값 전달.

### 2.4 바뀐 것

| 변경 | 내용 | 결과 영향 |
|---|---|---|
| 관절 변환 | smplx `batch_rigid_transform`이 부모 관절 인덱스를 매번 GPU에서 읽던 것을 한 번만 읽음 | 비트 단위로 같음 |
| 마스크 손실·카메라 투영 | 카메라별 루프를 전 카메라 일괄 계산으로, 프레임마다 같은 값은 프레임당 한 번만 준비 | 합산 순서만 달라 반올림 수준 차이 |
| 순전파+역전파 | 프레임마다 2번째 반복부터 CUDA graph로 캡처해 재생. 옵티마이저 스텝·LR 스케줄·gradient 마스크는 그대로 | 같은 연산을 재생 |

- 재생은 관통(penetration) 손실·실루엣 손실·ball-anchor를 쓰지 않을 때만 켜집니다. 운영 설정(`w_silhouette` 0.0, `w_pen`·ball-anchor 없음)은 재생 경로로 실행됩니다. `KAIST_MANO_CUDA_GRAPH=0`이면 예전 루프로 돌립니다.
- 재생 구간에서는 2D 유효성 필터 값이 갱신되지 않지만, 이 필터는 코드의 `filter_list` 4개 케이스(`double_stretch_zebra` 등)에만 적용되고 운영 에피소드(`ep_…`)는 해당하지 않습니다.

## 3. v7 (`c527fb1`): 설정이 바뀐 현재 운영 버전

| 항목 | v6 | v7 |
|---|---|---|
| 알고리즘 이름 | `mano-geometric-shared-shape-v6` | `mano-geometric-shared-shape-v7` |
| pose_temp_weight | 500000 | 500 |
| mp_crop (손 마스크 주변 2배 크롭에서 MediaPipe 재검출, 관절 80% 이상이 마스크 위일 때 채택) | 없음 | 켬 |
| mp_smooth (검출 관절 ±N프레임 평균) | 없음 | 4 |

- 나머지 설정·최적화 절차는 2.3과 같습니다. `mp_crop`·`mp_smooth`는 피팅 전 MediaPipe 2D 관절을 준비하는 단계이며 한 손 에피소드에만 적용됩니다. CUDA graph 재생은 v7에서도 그대로 쓰입니다.
- 시험 결과(TEST/Set, 2026-11-26): `ep_20261126_005323` 손끝 오차 6.2 → 5.0 px, SAM3 손 마스크 IoU 0.559 → 0.589, 떨림 0.88 → 0.46 mm. PC5 `ep_20261126_005859_1` 관통 68/133프레임 → 0, IoU 0.495 → 0.567. 실험 코드와 운영 코드의 정점 차이 0.0 mm. 재검출로 에피소드당 약 10초 증가.
- v6로 고정된 실행은 설정이 달라 MANO 단계에서 새 시도로 다시 피팅합니다.

## 4. 결과에 영향을 주는 MANO 밖 요인

- **처리 fps**(`c5153df`, 기본 15fps): MANO 코드는 같지만 15fps면 프레임 수가 절반, 프레임 간격이 2배가 됩니다. 시간축 정규화 가중치는 프레임 단위로 걸리므로 30fps 결과와 값이 다를 수 있습니다. 버전을 비교할 때는 같은 fps 결과끼리 비교합니다.

## 5. 확인하지 못한 것

- **양손 경로**: 한 손(오른손) 데이터로만 쟀습니다.
- **표본 크기**: CUDA graph 비교는 에피소드 1개·40프레임, v7은 TEST/Set 몇 개입니다.
- **v7에서의 CUDA graph 오차**: 0.16 mm는 v6 설정에서 잰 값입니다. v7 설정으로는 다시 재지 않았습니다.
- **다른 PC**: CUDA graph 측정은 PC1에서만 했습니다.

## 6. 직접 검증하는 방법

같은 에피소드를 재생을 끈 상태와 켠 상태로 각각 피팅해 프레임별 정점 좌표 차이를 비교합니다.

```bash
KAIST_MANO_CUDA_GRAPH=0 <MANO 피팅 실행>   # 예전 루프
KAIST_MANO_CUDA_GRAPH=1 <MANO 피팅 실행>   # 현재 기본
```

최댓값이 0.16 mm 안팎이고 프레임이 지나도 커지지 않으면 2절과 일치합니다. 관절 변환·일괄 계산 변경까지 포함하려면 `d7b808d`의 `data_process_hands.py`로 돌린 결과와 비교합니다.
