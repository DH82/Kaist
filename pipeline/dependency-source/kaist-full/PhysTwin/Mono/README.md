# Hand Data Processing

HaMBA recon → MANO temporal fit → 렌더.
**모든 명령은 repo root에서 실행** (스크립트 안에 상대경로가 하드코딩되어 있음).

## 전체 파이프라인

stage 1(HaMBA, `hamba` env) + stage 2(MANO fit, `phystwin` env)를 자동으로 dispatch.

```bash
python process_hand_data_ours.py \
  --base_path data_base_path \
  --case_name ep20260721_072818 \
  --num_hands -1 --num_cams 4 --log hand_v1
```

- `--num_hands -1` = auto (case명에 `single` 포함 → 1, 아니면 2)
- stage on/off는 파일 상단 `PROCESS_HAMBA` / `PROCESS_FIT` 플래그

## stage 1 — HaMBA만 (`hamba` env)

```bash
cd data_process/Hamba
/root/anaconda3/envs/hamba/bin/python script_hand_recon.py \
  --img_folder <case들의 부모 폴더> \
  --out_folder ../hand_recon_out
```

`--img_folder` 는 부모 폴더를 `os.listdir` 로 전부 돈다.
`results.pt` 가 이미 있으면 그 cam은 skip → 재실행하려면 먼저 지울 것:

```bash
rm data_process/hand_recon_out/<case>/*/results.pt
```

## stage 2 — MANO fit만 (`phystwin` env)

```bash
python data_process/data_process_hands.py \
  --hamba_base_path ./data_process/hand_recon_out \
  --phystwin_base_path data_base_path \
  --case_name ep20260721_072818 --log hand_v1 \
  --num_hands -1 --num_cams 4 \
  --temp_weight 5e5 --hamba_weight 0.01 --num_iters 1500 --seed 1027
```

구간만 다시 돌릴 때:

```bash
  --start_frame 30 --end_frame 90 --init_from_log hand_v1
```

`--case_name all` 로 주면 `hamba_base_path` 안의 모든 case를 순회한다.

## 렌더

```bash
python render_hand.py \
  --base_path data_base_path \
  --scene_name ep20260721_072818 --cam_id 0 --log hand_v1

python data_process/render_mediapipe_video.py \
  --base_path data_base_path \
  --case_name ep20260721_072818
```
