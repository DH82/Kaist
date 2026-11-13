# Weight 경로 입력

Ubuntu, CUDA, Python, PyTorch 및 패키지는 Docker 빌드가 자동 다운로드합니다.
모델 weight는 사용자가 이미 보유한 경로를 지정하며 GitHub나 이미지에 복사하지 않습니다.

## 공통 폴더 하나로 시작

```bash
./start.sh --weights /path/to/WFM_KAIST/workspace
```

## 서로 다른 위치의 모델 사용

`--weight 이름=/실제/경로`를 반복합니다. 기존 `.env` 경로는 유지되고 지정한 항목만 바뀝니다.
`--weights`를 새로 지정하면 공통 폴더 기준으로 다시 설정합니다.
터미널에서 경로가 빠져 있으면 해당 경로를 물어봅니다.

| 이름 | 입력할 경로 | 공통 폴더 기준 경로 |
|---|---|---|
| `hf-hub` | SAM3.1 / TRELLIS / DINOv2 Hugging Face hub cache | `hf-cache/hub` |
| `torch-hub` | CoTracker checkpoint and DINOv2 torch hub cache | `runtime-cache/torch/hub` |
| `vitdet` | ViTDet iopath cache | `runtime-cache/iopath` |
| `mano` | MANO_LEFT.pkl and MANO_RIGHT.pkl directory | `kaist-full/PhysTwin/Mono/data_process/Hamba/_DATA/data/mano` |
| `mano-mean` | mano_mean_params.npz file | `kaist-full/PhysTwin/Mono/data_process/Hamba/_DATA/data/mano_mean_params.npz` |
| `vitpose` | ViTPose checkpoint directory | `kaist-full/PhysTwin/Mono/data_process/Hamba/_DATA/vitpose_ckpts` |
| `hamba` | directory containing hamba.ckpt | `kaist-full/PhysTwin/Mono/data_process/Hamba/ckpts/hamba/checkpoints` |
| `superglue` | SuperPoint / SuperGlue weights directory | `kaist-full/PhysTwin/data_process/models/weights` |
| `mano-faces` | mano_faces.pkl file | `kaist-full/PhysTwin/data/mano_faces.pkl` |
| `mano-faces-mono` | Mono mano_faces.pkl file (normally same as mano-faces) | `kaist-full/PhysTwin/Mono/data/mano_faces.pkl` |

SAM3.1은 현재 파이프라인이 사용하는 Hugging Face **hub 캐시 폴더**를 입력합니다.
이 폴더에는 SAM3.1뿐 아니라 TRELLIS와 DINOv2 캐시도 필요합니다. 단독 `.pt` 파일을
hub 폴더로 지정할 수는 없습니다. `mano`는 MANO_LEFT.pkl과 MANO_RIGHT.pkl이 들어 있는 폴더입니다.
`mano-faces`를 개별 지정하면 Mono 단계에도 같은 파일을 기본으로 사용합니다.

실행 시 파일은 읽기 전용으로 마운트합니다. `.env`에 경로가 저장되므로 이후에는 `./start.sh`만 실행하면 됩니다.
모든 모델을 지정한 뒤 GPU·CUDA 확장·캐시·EGL·저장소 검사에 통과해야 UI 준비 완료로 표시합니다.
전체 에피소드 추론과 검수 승인은 UI에서 수행합니다.
