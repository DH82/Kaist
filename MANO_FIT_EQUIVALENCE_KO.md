# MANO 피팅: 초기 버전과 현재 버전의 계산 결과 비교

작성일: 2026-11-18, 갱신: 2026-11-27
대상 저장소: `DH82/Kaist` (main)

> **11-27 갱신 요약.** 이 문서의 비교(1~7절)는 v6 알고리즘에서 CUDA graph 전후를 비교한 것이고, 그 결론은 지금도 맞습니다.
> 다만 그 뒤 운영 MANO가 **v7로 바뀌어 알고리즘 설정 자체가 달라졌습니다**(11-26 `c527fb1`). 따라서 "초기 버전과 현재 버전의 수렴 방식이 같다"는
> 결론은 v7 이후에는 해당하지 않습니다. 바뀐 내용은 [8. 11-19 이후 바뀐 것](#8-11-19-이후-바뀐-것)에 적었습니다.

## 결론

- **수렴 방식(알고리즘)은 초기 버전과 현재 버전이 같습니다.** 반복 횟수, 옵티마이저, 학습률 스케줄, 단계별 피팅 순서, 손실 항과 가중치, 실행 인자가 모두 그대로입니다.
- **계산 결과는 비트 단위로 같지는 않습니다.** 실측한 차이는 손 메시 정점 기준 **최대 0.16mm**이고, 프레임이 진행되어도 누적되지 않았습니다. 원인은 같은 수식을 더하는 순서가 달라진 데서 오는 부동소수점 반올림 차이입니다.
- 이 실측은 에피소드 1개, 40프레임, 한 손 경로에 대한 것입니다. 범위 밖 항목은 [5. 확인하지 못한 것](#5-확인하지-못한-것)에 적었습니다.

## 1. 비교 대상

| 구분 | 커밋 | 시각 |
|---|---|---|
| 초기 버전 | `d7b808d` (Build GPU environments in Docker from pinned versions) | 2026-11-13 15:32 |
| 현재 버전 | `928da04` (MANO fit: replay forward/backward as a CUDA graph) 이후 `HEAD`(작성 당시 11-18) | 2026-11-17 14:07 |

MANO 피팅 본체는 `pipeline/current/workspace/kaist-full/PhysTwin/Mono/data_process/data_process_hands.py`이고, 이를 호출하는 스크립트는 `pipeline/current/project/00_scripts/18_fit_hands.py`입니다.

## 2. 바뀐 파일이 하나뿐이라는 근거

두 커밋 사이에 MANO 피팅 경로에서 바뀐 파일은 `data_process_hands.py` 하나입니다(204줄 추가, 54줄 삭제, 커밋 `928da04` 한 건).

아래 파일은 초기 import(`d7b808d`) 이후 커밋 이력이 없습니다.

- `00_scripts/18_fit_hands.py` (실행 인자와 설정)
- `00_scripts/hand_stage_worker.py`
- `Mono/process_hand_data_ours.py`
- `Mono/data_process/Hamba/script_hand_recon.py`
- `Mono/data_process/models/mano_wrapper.py`

작업 트리의 미커밋 변경에도 위 파일과 `data_process_hands.py`는 포함되어 있지 않습니다.

확인 명령:

```bash
git log --oneline --follow -- pipeline/current/workspace/kaist-full/PhysTwin/Mono/data_process/data_process_hands.py
git diff d7b808d HEAD --stat -- pipeline/current/workspace/kaist-full/PhysTwin/Mono pipeline/current/project/00_scripts
```

두 번째 명령에는 `10_reconstruct.py`도 나오지만, 이는 3D 복원 단계의 재실행 판단(`--auto-new-attempt`) 변경이며 MANO 계산과 무관합니다.

## 3. 그대로인 것

### 3.1 실행 인자

`18_fit_hands.py`가 넘기는 설정은 두 버전에서 같습니다.

| 항목 | 값 |
|---|---|
| 알고리즘 이름 | `mano-geometric-shared-shape-v6` |
| 프레임당 반복 횟수 | 1500 |
| seed | 1027 |
| 카메라 수 | 4 |
| temp_weight / pose_temp_weight | 500000 / 500000 |
| hamba_weight | 0.01 |
| w_mask / w_mask_cover / cover_max_px | 1.0 / 1.0 / 40.0 |
| wrist_weight | 0.1 |
| w_pose_prior | 25.0 |
| robust_joint_delta | 5.0 |
| staged_fit | 켬 |
| shared_shape | 켬 (frame 12개, 3000회, lr 1e-3) |
| camera_init_iters | 300 |
| fit_shape | 끔 |
| w_silhouette | 0.0 |

### 3.2 최적화 절차

`data_process_hands.py`의 아래 부분은 diff에 나타나지 않습니다.

- 옵티마이저: AdamW, lr 2e-3
- 학습률 스케줄: StepLR(40회마다 0.98배), 2.5e-4 아래로는 내리지 않음
- 반복 종료 조건: 1500회 고정, 조기 종료 없음
- 단계별 피팅: 처음 10%는 전역 자세만, 25%까지는 일부 관절, 그 뒤 전체
- 손실 항의 종류와 가중치
- 공유 shape 보정, 카메라 초기화, 프레임 간 초기값 전달

## 4. 바뀐 것과 결과에 미치는 영향

커밋 `928da04`의 변경은 세 가지이며, 모두 같은 수식을 더 적은 GPU 호출로 계산하기 위한 것입니다.

| 변경 | 내용 | 결과 영향 |
|---|---|---|
| 관절 변환 | smplx `batch_rigid_transform`이 부모 관절 인덱스를 매번 GPU에서 읽던 것을 한 번만 읽도록 함 | 비트 단위로 같음 |
| 마스크 손실·카메라 투영 | 카메라별 for 루프를 전 카메라 일괄 계산으로 바꾸고, 프레임마다 변하지 않는 값은 프레임당 한 번만 준비 | 수식은 같고 합산 순서만 달라 반올림 수준의 차이 |
| 순전파+역전파 | 프레임마다 2번째 반복부터 CUDA Graph로 캡처해 재생. 옵티마이저 스텝·학습률 스케줄·단계별 gradient 마스크는 기존 방식대로 실행 | 같은 연산을 재생하므로 연산 자체는 같음 |

### 실측

- 조건: 에피소드 `ep_20261113_061926_1`, PC1(RTX 4090), 40프레임, 2026-11-17 측정
- 결과 차이: 초기 버전 대비 정점 위치 **최대 0.16mm**, 프레임이 지나도 누적되지 않음
- 속도: 프레임당 12.8~13.2초 → 1.9초

1500회 반복 최적화에서는 반올림 차이가 반복을 거치며 조금씩 벌어지기 때문에, 수식이 같아도 결과가 완전히 일치하지는 않습니다. 0.16mm는 그 범위의 차이입니다.

### 그래프 재생이 적용되는 조건

재생은 관통(penetration) 손실, 실루엣 손실, ball-anchor를 쓰지 않을 때만 켜집니다. 현재 운영 설정은 `w_silhouette` 0.0이고 `w_pen`과 ball-anchor 인자를 넘기지 않으므로 재생 경로로 실행됩니다. 환경 변수 `KAIST_MANO_CUDA_GRAPH=0`을 주면 재생을 끄고 초기 버전과 같은 루프로 실행합니다.

재생 구간에서는 2D 유효성 필터 값이 갱신되지 않습니다. 다만 이 필터는 코드의 `filter_list`에 적힌 4개 케이스(`double_stretch_zebra`, `single_lift_cloth_1`, `single_lift_cloth_3`, `single_push_rope_4`)에만 적용되고, 운영 에피소드(`ep_...`)는 해당하지 않아 초기 버전에서도 쓰이지 않던 경로입니다.

## 5. 확인하지 못한 것

- **양손 경로**: 실측은 한 손(오른손) 데이터로만 했습니다. 양손 데이터가 없어 양손 경로의 결과 차이는 재지 못했습니다.
- **표본 크기**: 에피소드 1개, 40프레임입니다. 다른 에피소드나 전체 길이에서 재지는 않았습니다.
- **재측정**: 0.16mm는 2026-11-17 측정값이며, 이 문서를 쓰면서 다시 측정하지 않았습니다.
- **다른 PC**: 측정은 PC1에서만 했습니다.

## 6. MANO 코드 밖에서 결과가 달라질 수 있는 요인

"처리 fps 선택(기본 15fps)" 변경이 있습니다(작성 당시 미커밋, 11-18 `c5153df`로 반영). MANO 코드는 바꾸지 않지만, 15fps로 처리하면 프레임 수가 절반이 되고 프레임 간격이 2배가 됩니다. 시간축 정규화 가중치는 프레임 단위로 걸리므로, 30fps로 처리한 결과와는 값이 달라질 수 있습니다. 초기 버전과 현재 버전을 비교할 때는 같은 fps로 처리한 결과끼리 비교해야 합니다.

## 7. 직접 검증하는 방법

같은 에피소드를 재생을 끈 상태와 켠 상태로 각각 피팅해 정점 차이를 비교하면 됩니다.

```bash
# 초기 버전과 같은 루프
KAIST_MANO_CUDA_GRAPH=0 <MANO 피팅 실행>
# 현재 기본 동작
KAIST_MANO_CUDA_GRAPH=1 <MANO 피팅 실행>
```

두 결과의 프레임별 정점 좌표 차이의 최댓값이 0.16mm 안팎이고 프레임이 지나도 커지지 않으면 위 실측과 일치합니다. 관절 변환과 일괄 계산 변경까지 포함해 비교하려면 `d7b808d`의 `data_process_hands.py`로 한 번 실행한 결과와 비교합니다.

## 8. 11-19 이후 바뀐 것

아래 변경은 CUDA graph와 달리 **계산 내용이 바뀌는** 변경입니다. 같은 에피소드라도 v6 결과와 v7 결과는 다릅니다.

| 커밋 | 날짜 | 바뀐 것 | 결과 영향 |
|---|---|---|---|
| `cb2c5d2` | 11-19 | 공유 shape 보정 프레임이 모자라 멈추던 경우에만 다른 기준 카메라 시도 → MediaPipe 크롭 재검출로 구제(`hand_v1/shape_rescue.pkl`) | 예전에 **실패하던** 에피소드만 결과가 생김. 보정되던 에피소드는 그대로 |
| `c527fb1` | 11-26 | 알고리즘 `mano-geometric-shared-shape-v7`: `pose_temp_weight` 500000 → **500**, `mp_crop` 켬, `mp_smooth` 4 | 손가락이 영상을 따라감. 모든 에피소드의 결과가 바뀜 |
| `ce6f756` | 11-26 | 사람별 손 형상 프로필(β 고정) 기능은 기본 꺼짐 | 켜지 않으면 영향 없음. 켜면 알고리즘 이름이 `mano-geometric-profile-shape-v*` |
| `2604542` → `2198802` | 11-26 | v8(삼각측량 초기 위치, 깊이 항)을 넣었다가 되돌림 | 현재 운영 결과에는 영향 없음 |

v7에서 3.1절 표 중 바뀐 값은 다음과 같고, 나머지(반복 1500, seed 1027, `temp_weight` 500000, 손실 가중치, shared shape, 카메라 초기화 등)는 그대로입니다.

| 항목 | v6 | v7 |
|---|---|---|
| 알고리즘 이름 | `mano-geometric-shared-shape-v6` | `mano-geometric-shared-shape-v7` |
| pose_temp_weight | 500000 | 500 |
| mp_crop (손 마스크 주변 2배 크롭에서 MediaPipe 재검출, 관절 80% 이상이 마스크 위일 때 채택) | 없음 | 켬 |
| mp_smooth (검출 관절 ±N프레임 평균) | 없음 | 4 |

v7 시험 결과(TEST/Set, 2026-11-26): `ep_20261126_005323` 손끝 오차 6.2 → 5.0 px, SAM3 손 마스크 IoU 0.559 → 0.589, 떨림 0.88 → 0.46 mm.
PC5 `ep_20261126_005859_1` 관통 68/133프레임 → 0, IoU 0.495 → 0.567. 실험 코드와 운영 코드의 정점 차이는 0.0 mm였습니다. 재검출로 에피소드당 약 10초 늘어납니다.
CUDA graph 재생은 v7에서도 그대로 쓰입니다(`mp_crop`·`mp_smooth`는 피팅 전 MediaPipe 2D 관절 준비 단계이며, 한 손 에피소드에만 적용).

v6로 고정된 실행은 설정이 달라 MANO 단계에서 새 시도로 다시 피팅합니다. 0.16 mm 비교를 v7에서 다시 하려면 v7 설정으로 `KAIST_MANO_CUDA_GRAPH=0`/`1`을 각각 실행해 비교합니다(아직 측정하지 않았습니다).
