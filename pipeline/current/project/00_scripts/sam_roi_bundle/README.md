# SAM3 손 ROI 확대 추론 — Ubuntu 이전 안내

작성일: 2026-10-22. 대상: 새 Ubuntu GPU PC의 담당자.

## 현재 상태

2026-10-15 이전 패키지의 실행 코드와 설정을 SAM 전용으로 분리했습니다. 별도로 전달받은 최신 확정 코드는 없습니다. **손 ROI 확대 추론은 GPU 검증 전 후보**이며, 개선 효과·처리시간·VRAM 절감이 확정된 버전이 아닙니다. 본 패키지는 기존 마스크를 자동 교체하지 않습니다.

## 개선 방식과 한계

1. 전체 화면에서 물체와 손을 SAM3.1로 추론합니다.
2. 카메라별 전체 구간 손 마스크의 합집합에서 고정 ROI를 잡습니다. 가로·세로 50% 여백(최소 16픽셀)을 추가합니다.
3. ROI를 4배 확대해 `hand`를 추론하고 `human forearm`을 차감합니다.
4. 원본 좌표로 최근접 축소한 뒤 ROI 내부만 교체합니다. 바깥쪽은 기존 마스크를 유지합니다.
5. ROI 결과가 비어 있으면 해당 프레임은 기존 마스크로 되돌립니다. 카메라 전체에서 손을 못 찾으면 그 카메라는 건너뜁니다.

확대는 원본에 없는 디테일을 만들지 않습니다. 기존 추론에서 완전히 놓친 손이나 ROI 밖의 손 복구를 보장하지 않습니다. 최대 300프레임의 짧은 파일럿용입니다. 긴 영상의 이동 ROI·구간 연결은 별도 개발 대상입니다.

**물체 마스크는 그대로입니다.** 물체 메시에서 손이 함께 생성되는 오류는 물체 마스크·TRELLIS 입력과 결과를 별도 확인해야 합니다. 이 후보를 실행했다고 해당 오류가 해결된 것으로 처리하지 않습니다.

기존 전체 화면 코드는 형태학적 보정·작은 영역 제거·면적 급증 제거가 포함된 기준선입니다. ROI 분기에는 같은 후처리가 없습니다. 따라서 현재 비교는 '확대만의 효과'가 아닌 두 처리 방식 전체의 비교입니다. 순수 확대 효과를 주장하려면 후처리 조건을 맞춘 추가 비교가 필요합니다.

## 포함 파일

- `source/segment_object_sam3.py`: 기존 소스 ZIP에서 그대로 추출한 전체 화면 실행기
- `tools/sam_roi_candidate.py`: 손 ROI 후보 실행기
- `configs/sam_roi_candidate_v1.json`: 모델·프롬프트·확대율·임계값
- `setup_sam.sh`: 기존 SAM 환경 설치 스크립트
- `tools/measure.py`: 시간·GPU 사용률·VRAM·전력 기록
- `run_baseline.sh`, `run_roi.sh`: 원본 보존 및 별도 결과 경로를 사용하는 실행 명령
- `SHA256SUMS`: 파일 무결성 확인

Raw 데이터, 모델 가중치, HF 토큰, MANO 파일은 포함하지 않았습니다. 기존 전처리 에피소드를 별도로 준비하십시오.

## Ubuntu 설치 순서

Ubuntu 24.04 기준입니다. NVIDIA 드라이버 설치 후 `nvidia-smi`가 정상이어야 합니다. Ubuntu/RTX에서 본 패키지의 실제 추론은 아직 재검증하지 않았습니다.

```bash
sudo apt update
sudo apt install -y git ffmpeg unzip python3-venv
unzip SAM3_Ubuntu_transfer_20261022.zip
cd SAM3_Ubuntu_transfer_20261022
sha256sum -c SHA256SUMS
python3 -m venv ~/.venvs/kaist-tools
~/.venvs/kaist-tools/bin/pip install uv
export PATH="$HOME/.venvs/kaist-tools/bin:$PATH"
mkdir -p "$HOME/kaist-sam"
bash setup_sam.sh "$HOME/kaist-sam"
export HF_HOME="$HOME/kaist-sam/hf-cache"
"$HOME/kaist-sam/sam-env/bin/hf" auth login
```

승인된 `facebook/sam3.1` 접근 계정이 필요합니다. 토큰은 로컬 로그인에서 입력하고 공유하지 마십시오. 모델은 추론 시 다운로드됩니다. 설치 기준은 Python 3.12, torch 2.10.0/cu128, torchvision 0.25.0, SAM 소스 commit `20dba30`입니다. 일부 의존성은 고정되지 않았으므로 생성되는 `sam-resolved-freeze.txt`도 보관합니다. 임의 업그레이드 후 결과를 기존 실험과 동일하다고 간주하지 마십시오. Flash Attention 3는 사용하지 않습니다.

## 입력과 실행

이미 전처리한 에피소드가 필요합니다. Raw 녹화 파일을 바로 넣는 도구가 아닙니다.

```text
episode/
  metadata.json              # WH=[width,height], frame_num, fps, num_cameras
  color/0.mp4 ... 3.mp4       # PNG와 같은 프레임 순서·해상도
  color/0/0.png ...           # 카메라 0~3, 프레임 0부터 연속 번호
  depth/0/ ... depth/3/       # 기존 전처리 폴더
```

```bash
# 새 출력 경로를 지정합니다. 이미 존재하면 중단합니다.
bash run_baseline.sh "$HOME/kaist-sam" /data/episode /data/sam_trial 'blue cloth' 2d
bash run_roi.sh "$HOME/kaist-sam" /data/sam_trial/case /data/sam_trial/roi_candidate
```

`blue cloth`는 천 예시입니다. 실제 물체 명사로 변경하십시오. scene은 기존 정의 1d(끈), 2d(천), 3d(입체 물체)를 사용합니다. 본 후보의 손 프롬프트는 hand/forearm으로 고정되어 있어 3d 기준선과는 프롬프트도 다릅니다. 우선 기존 천 샘플/2d로 확인하십시오.

기준선은 입력에서 color/depth/metadata만 별도 폴더로 복사합니다. 원본을 덮어쓰지 않지만 RGB-D를 복사할 디스크 여유가 필요합니다. 실행 중 실패한 출력은 자동 재개하지 않으며 새 경로에서 재실행합니다.

## 결과 확인과 다음 담당자에게 전달할 파일

- 기준선: `/data/sam_trial/case/mask/`
- 후보: `/data/sam_trial/roi_candidate/mask/` (0=물체, 1=손)
- 후보 기록: `candidate_record.json`, 카메라별 ROI 작업 파일
- 측정 기록: `~/kaist-sam/benchmarks/`의 output.log, gpu.csv, status.json
- 환경 기록: sam-resolved-freeze.txt 및 `nvidia-smi` 출력

`candidate_record.json`의 IoU는 기존 결과와의 일치도이며 정답 정확도가 아닙니다. difference_pixels는 바뀐 마스크 픽셀 개수이지 관절 재투영 오차가 아닙니다. progress.json은 카메라 처리 표시이며 완료 판정은 종료코드와 candidate_record.json, 출력 전체를 함께 확인합니다.

검수에서는 같은 RGB 프레임 위에 기준선/후보를 비교하여 손목 손실, 손가락 누락, 팔 혼입, 가림·재등장, 양손 구분, 물체와 손의 중첩을 확인합니다. 첫·중간·마지막뿐 아니라 실패 프레임을 포함합니다. 카메라/프레임별 승인·수정·반려와 사유를 남기고, 승인된 마스크만 별도 에피소드 사본의 후단에 적용합니다. 폴더가 생겼다는 이유만으로 검수 완료로 처리하지 않습니다.

30→15fps와 MANO 반복수 변경은 이번 SAM 전용 묶음에 포함하지 않았습니다. 먼저 같은 30fps 입력으로 SAM 변경만 비교해야 원인을 분리할 수 있습니다.

## Drive 업로드

이 ZIP과 SHA256 파일을 사내 승인된 비공개 Drive 폴더에 업로드한 뒤 Ubuntu에서 내려받습니다. 가중치·토큰은 ZIP에 추가하지 마십시오. Drive의 Ubuntu_RTX5090_20261001 폴더에 이 묶음을 보관합니다. ZIP을 다운로드한 뒤 위 설치 순서대로 진행하십시오.
