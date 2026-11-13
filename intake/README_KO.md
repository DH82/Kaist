# 촬영 노트북에서 NAS로 에피소드 전송

Python 3.9 이상과 노트북에 연결된 NAS 공유 폴더가 필요합니다. 추가 Python 패키지는 필요하지 않습니다.

```bash
python3 upload_episode.py /촬영폴더/ep_20260826_083803 \
  --nas-raw /NAS마운트/raw \
  --calibration-file extrinsics.json \
  --calibration-id cal_20261106_01 \
  --capture-date 2026-11-06 \
  --uploader 촬영자A
```

`calibration-file`은 에피소드 안의 실제 캘리브레이션 파일 상대 경로입니다. 전송 도구는 에피소드 전체와 파일별 크기·SHA256 명세를 복사하고, 마지막에 완료 표시를 생성합니다. 같은 에피소드가 있으면 중단하며 덮어쓰지 않습니다. 중단된 전송의 자동 재개는 아직 지원하지 않습니다.

NAS 관리자 UI의 데이터 입고에서 전송 완료와 파일 무결성을 확인합니다. 이는 캘리브레이션 수치의 정확성을 검증하는 과정은 아닙니다. 입고 검증 후 작업자는 작업 실행 화면에서 에피소드를 선택합니다. 실행한 로그인 이름이 담당자로 기록되며 별도 수동 배정은 필요하지 않습니다.

현재 GPU PC1만 연결합니다. Worker의 `intake_manifests`와 중앙 입고 기록이 일치해야 실행됩니다. Worker의 `raw_root`와 NAS UI의 `KAIST_NAS_RAW_ROOT`는 같은 RAW 공유 폴더를 바라봐야 합니다.

RAW는 `raw/ep_.../` 형태로 둡니다. `upload_manifest.json`과 `upload_complete.json`이 없으면 입고 검증을 통과하지 못합니다. 이미 전송한 RAW는 수정하지 않고 새 에피소드로 등록합니다.

전체 설치 방법은 패키지 상위 폴더의 INSTALL_KO.md를 참고하세요. 개인 이름 로그인은 시험용이며, GPU 6대 분산과 상시 업로드 감시는 별도 구현 대상입니다.
