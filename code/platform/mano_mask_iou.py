#!/usr/bin/env python3
"""MANO 손 메시 실루엣 ↔ SAM3 손 마스크 일치도 (카메라 4대 × 전 프레임). hamba-env 파이썬.

  <hamba-env>/bin/python mano_mask_iou.py <결과 폴더>... --case <inputs/<ep>>
메시 삼각형 1538개를 각 카메라에 투영해 채운 실루엣과 승인된 SAM3 손 마스크를 비교합니다.
- IoU = 겹친 면적 / 합친 면적, 정밀도 = 실루엣 중 마스크 안 비율, 재현율 = 마스크 중 실루엣이 덮은 비율
- 마스크가 비었거나(200px 미만) 실루엣이 화면 밖인 카메라·프레임은 제외
결과: 폴더마다 mask_iou.json, 표준출력에 요약
"""
import argparse, io, json, os, pickle, sys
from pathlib import Path
import cv2, numpy as np, torch

WORKER_HOME = Path(os.environ.get('KAIST_HAND_SHAPE_WORKER', str(Path.home() / 'Desktop/worker1/airflow')))
MONO = Path(json.loads((WORKER_HOME / 'config/gpu_4090_a.json').read_text())['pipeline_root']) / 'kaist-full/PhysTwin/Mono'


class U(pickle.Unpickler):
    def find_class(self, m, n):
        if m == 'torch.storage' and n == '_load_from_bytes': return lambda b: torch.load(io.BytesIO(b), map_location='cpu', weights_only=False)
        return super().find_class(m, n)


def main():
    p = argparse.ArgumentParser(); p.add_argument('outs', nargs='+', type=Path); p.add_argument('--case', type=Path, required=True); a = p.parse_args()
    if True:  # 오른손 MANO 면(모델 파일에서)
        os.chdir(MONO); sys.path.insert(0, 'data_process'); from models.mano_wrapper import MANO
        faces = np.asarray(MANO(data_dir='data_process/Hamba/_DATA/data/', model_path='data_process/Hamba/_DATA/data/mano', gender='neutral', num_hand_joints=15,
                                mean_params='data_process/Hamba/_DATA/data/mano_mean_params.npz', create_body_pose=False).faces, np.int64)
    meta = json.loads((a.case / 'metadata.json').read_text()); Ks = np.asarray(meta['intrinsics'], float); W, H = meta['WH']
    w2c = np.linalg.inv(np.asarray(pickle.load(open(a.case / 'calibrate.pkl', 'rb')), float))
    masks = {}
    for c in range(4):
        info = json.loads((a.case / 'mask' / f'mask_info_{c}.json').read_text()); hid = next(k for k, v in info.items() if isinstance(v, str) and 'hand' in v.lower())
        masks[c] = hid
    cache = {}
    for out in a.outs:
        reg = U(open(out / 'hand_v1/hand_reg.pkl', 'rb')).load(); T = len(reg)
        I = P = R = 0.0; n = 0; per_cam = {c: [] for c in range(4)}
        for f in range(T):
            V = np.asarray(reg[f]['right'], float)
            for c in range(4):
                key = (c, f)
                if key not in cache:
                    mp = a.case / 'mask' / str(c) / masks[c] / f'{f}.png'
                    m = cv2.imread(str(mp), 0) if mp.exists() else None
                    cache[key] = (m > 127) if m is not None and (m > 127).sum() >= 200 else None
                m = cache[key]
                if m is None: continue
                Xc = V @ w2c[c][:3, :3].T + w2c[c][:3, 3]; K = Ks[c]
                uv = np.stack([K[0, 0] * Xc[:, 0] / Xc[:, 2] + K[0, 2], K[1, 1] * Xc[:, 1] / Xc[:, 2] + K[1, 2]], 1)
                sil = np.zeros((H, W), np.uint8); cv2.fillPoly(sil, list(np.round(uv[faces] * 4).astype(np.int32)), 1, lineType=cv2.LINE_8, shift=2); s = sil > 0
                if s.sum() < 50: continue
                inter = float((s & m).sum()); iou = inter / float((s | m).sum())
                I += iou; P += inter / s.sum(); R += inter / m.sum(); n += 1; per_cam[c].append(iou)
        res = {'iou': round(I / n, 4), 'precision': round(P / n, 4), 'recall': round(R / n, 4), 'samples': n,
               'iou_per_camera': {str(c + 1): round(float(np.mean(v)), 4) if v else None for c, v in per_cam.items()}}
        (out / 'mask_iou.json').write_text(json.dumps(res, indent=1)); print(out.name, json.dumps(res))


if __name__ == '__main__':
    main()
