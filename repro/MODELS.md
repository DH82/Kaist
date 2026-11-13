# 모델 weight 배치

모델 weight는 Git과 Docker 이미지에 넣지 않습니다. 새 PC에 이미 있는 weight 폴더(원본 `WFM_KAIST/workspace` 구조)를 `.env`의 `KAIST_WEIGHTS_ROOT`로 지정하면 `gpu-worker` 컨테이너가 아래 경로를 **읽기 전용**으로 마운트합니다. 컨테이너 안에서는 원래 PC와 같은 절대경로에 보이고, 파이프라인 workspace의 심볼릭 링크도 원래 PC와 같습니다.

목록의 기준은 `gpu/env/weights.txt`입니다. 경로가 하나라도 없으면 `repro/prepare_pc.py`가 중단하고, 컨테이너도 시작하지 않습니다.

| 구성 요소 | `KAIST_WEIGHTS_ROOT` 아래 경로 |
|---|---|
| SAM3.1·TRELLIS·DINOv2 (Hugging Face 캐시) | `hf-cache/hub` |
| DINOv2 torch hub 코드·CoTracker 체크포인트 | `runtime-cache/torch/hub` |
| ViTDet (detectron2) | `runtime-cache/iopath` |
| MANO | `kaist-full/PhysTwin/Mono/data_process/Hamba/_DATA/data/mano`, `.../_DATA/data/mano_mean_params.npz`, `kaist-full/PhysTwin/data/mano_faces.pkl`, `kaist-full/PhysTwin/Mono/data/mano_faces.pkl` |
| ViTPose | `kaist-full/PhysTwin/Mono/data_process/Hamba/_DATA/vitpose_ckpts` |
| HaMBA | `kaist-full/PhysTwin/Mono/data_process/Hamba/ckpts/hamba/checkpoints` |
| SuperPoint·SuperGlue | `kaist-full/PhysTwin/data_process/models/weights` |

확인:

```bash
docker compose --profile gpu run --rm --no-deps gpu-worker check
```

`check`는 SAM3.1 캐시, 추적·3D 복원·HaMBA 단계의 자체 검사(`tracking_worker`·`reconstruction_worker`·`hand_stage_worker check`)를 실행합니다. 모델 없이 환경만 볼 때는 `check --runtime-only`를 쓰지만, 이것은 추론 준비 완료를 뜻하지 않습니다.
