# 보정값 교체(calfix) 도구 사본

실제로 도는 원본은 NAS `control/calfix_<날짜>/`에 있고, 각 PC 업데이터가 `control/update/tick_hooks.json` 항목으로 정기 실행합니다. 이 폴더는 기록용 사본입니다. 고칠 때는 NAS 원본을 고친 뒤 여기에 다시 복사합니다.

| 폴더 | 대상 | 내용 |
|---|---|---|
| `20261125/` | 11-25 10:41·14:41 보정으로 찍힌 RAW 211개 | 보드 크기 설정 오류로 카메라 간 거리가 0.8668·0.9321배로 짧게 잡힘. 교체본 = 그 RAW 자체 보정의 회전은 그대로, 이동만 1/배율. 10:41 회차는 cam_2가 실제로 다른 위치였으므로 16:29 보정값을 통째로 쓰면 안 됨. 회차 기록은 `sessions.json` |

- `gen_calfix.py`: 전처리(`preprocess_rgbd.py`)와 같은 함수로 `calibrate.pkl`·`world_reorient_M.npy` 교체본을 만듭니다. 동기화 기준별로 따로 만듭니다: `fixes/<RAW>/fps15`(host_time_s, e86b5ab 이전 전처리), `fixes/<RAW>/fps15_hw`(카메라 시계, e86b5ab 이후). 둘 다 실제 전처리 결과와 바이트 일치를 확인했습니다.
- `fix_raw.py`: NAS `rgbd_raw/<RAW>/extrinsics.json`의 이동 값을 같은 배율로 직접 고칩니다(11-27 18:04 적용, 원본은 NAS `control/calfix_20261125/raw_backup/`). 고친 뒤 새로 전처리하는 실행은 교체본과 같은 파일을 바로 만듭니다.
- `apply_calfix.py`: 마스크 승인 후·3D 시작(`_resume`) 전 에피소드만 교체합니다. 현재 파일과 해시가 맞는 쪽 교체본을 고르고, 백업은 `<작업 폴더>/99_history/calfix_<날짜>/`, 결과 기록은 NAS `applied/`. `--revert`로 되돌립니다.
