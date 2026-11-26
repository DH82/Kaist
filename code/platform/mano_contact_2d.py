#!/usr/bin/env python3
"""손-물체 접점 2D 점검 (3D 물체 복원 전 대용). hamba-env 파이썬.

  <hamba-env>/bin/python mano_contact_2d.py <결과 폴더>... --case <inputs/<ep>> --recon <06_reconstruction/<ep>>
SAM3 마스크에서 손이 물체에 닿아 보이는 카메라·프레임(손 마스크를 3px 넓히면 물체 마스크와 겹침)을 '접촉'으로 보고,
그때 MANO 결과가
- 손 실루엣이 물체 마스크에 닿는지(3px 이내) → contact_hit
- 손가락 끝 5개 중 물체 마스크에 가장 가까운 것까지의 거리(px) → tip_gap (0 = 물체 위)
- 같은 접촉에서 SAM3 손 마스크 기준 손가락 끝 거리(참고 상한) → mask_tip_gap 은 계산 안 함(손 마스크에는 관절이 없음)
비접촉 프레임에서 MANO 손이 물체에 닿아 보이는 비율 → false_contact
결과: 폴더마다 contact_2d.json
"""
import argparse, io, json, os, pickle, sys
from pathlib import Path
import cv2, numpy as np, torch

WORKER_HOME = Path(os.environ.get('KAIST_HAND_SHAPE_WORKER', str(Path.home() / 'Desktop/worker1/airflow')))
MONO = Path(json.loads((WORKER_HOME / 'config/gpu_4090_a.json').read_text())['pipeline_root']) / 'kaist-full/PhysTwin/Mono'
TIPS = [745, 317, 444, 556, 673]  # MANO 손가락 끝 정점(엄지·검지·중지·약지·소지)


class U(pickle.Unpickler):
    def find_class(self, m, n):
        if m == 'torch.storage' and n == '_load_from_bytes': return lambda b: torch.load(io.BytesIO(b), map_location='cpu', weights_only=False)
        return super().find_class(m, n)


def main():
    p = argparse.ArgumentParser(); p.add_argument('outs', nargs='+', type=Path); p.add_argument('--case', type=Path, required=True); p.add_argument('--recon', type=Path, required=True)
    a = p.parse_args()
    os.chdir(MONO); sys.path.insert(0, 'data_process'); from models.mano_wrapper import MANO
    faces = np.asarray(MANO(data_dir='data_process/Hamba/_DATA/data/', model_path='data_process/Hamba/_DATA/data/mano', gender='neutral', num_hand_joints=15,
                            mean_params='data_process/Hamba/_DATA/data/mano_mean_params.npz', create_body_pose=False).faces, np.int64)
    meta = json.loads((a.case / 'metadata.json').read_text()); Ks = np.asarray(meta['intrinsics'], float); W, H = meta['WH']
    w2c = np.linalg.inv(np.asarray(pickle.load(open(a.case / 'calibrate.pkl', 'rb')), float))
    k3 = np.ones((7, 7), np.uint8)
    obj = {}; touch = {}
    T = None
    for out in a.outs:
        reg = U(open(out / 'hand_v1/hand_reg.pkl', 'rb')).load(); T = len(reg)
        hit = gap = fc = 0; n_touch = n_free = 0; gaps = []
        for f in range(T):
            V = np.asarray(reg[f]['right'], float)
            for c in range(4):
                if (c, f) not in obj:
                    om = cv2.imread(str(a.recon / 'mask' / str(c) / '0' / f'{f}.png'), 0); hm = cv2.imread(str(a.recon / 'mask' / str(c) / '1' / f'{f}.png'), 0)
                    if om is None or hm is None or (om > 127).sum() < 200 or (hm > 127).sum() < 200: obj[(c, f)] = None; continue
                    o = (om > 127).astype(np.uint8)
                    obj[(c, f)] = (o, cv2.distanceTransform(1 - o, cv2.DIST_L2, 3))
                    touch[(c, f)] = bool((cv2.dilate((hm > 127).astype(np.uint8), k3) & o).any())
                if obj[(c, f)] is None: continue
                o, dt = obj[(c, f)]
                Xc = V @ w2c[c][:3, :3].T + w2c[c][:3, 3]; K = Ks[c]
                uv = np.stack([K[0, 0] * Xc[:, 0] / Xc[:, 2] + K[0, 2], K[1, 1] * Xc[:, 1] / Xc[:, 2] + K[1, 2]], 1)
                sil = np.zeros((H, W), np.uint8); cv2.fillPoly(sil, list(np.round(uv[faces] * 4).astype(np.int32)), 1, shift=2)
                sil_touch = bool((cv2.dilate(sil, k3) & o).any())
                tip = np.clip(np.round(uv[TIPS]).astype(int), [0, 0], [W - 1, H - 1]); tg = float(dt[tip[:, 1], tip[:, 0]].min())
                if touch[(c, f)]:
                    n_touch += 1; hit += sil_touch; gaps.append(tg)
                else:
                    n_free += 1; fc += sil_touch
        g = np.asarray(gaps) if gaps else np.asarray([np.nan])
        res = {'touch_samples': n_touch, 'free_samples': n_free, 'contact_hit': round(hit / max(1, n_touch), 4),
               'tip_gap_median_px': round(float(np.nanmedian(g)), 2), 'tip_gap_mean_px': round(float(np.nanmean(g)), 2),
               'tip_on_object_ratio': round(float(np.mean(g <= 3)), 4), 'false_contact': round(fc / max(1, n_free), 4)}
        (out / 'contact_2d.json').write_text(json.dumps(res, indent=1)); print(out.name, json.dumps(res))


if __name__ == '__main__':
    main()
