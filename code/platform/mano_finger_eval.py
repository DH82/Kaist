#!/usr/bin/env python3
"""MANO 피팅 결과의 손가락 추종 평가 (hamba-env 파이썬으로 실행).

  <hamba-env>/bin/python mano_finger_eval.py <결과 폴더(hand_v1 포함)> --case <런타임 inputs/<ep>> --hamba <hamba/<ep>> --obs <observations.pkl>

지표
- mp2d: MediaPipe 2D 관절과의 오차(px, 신뢰도 0.5 이상만). 전체·손가락 끝(4,8,12,16,20)·손가락별, 중앙값/평균
- mask: 손가락 메시 점(손가락 관절에 붙은 점)이 SAM3 손 마스크 안에 있는 비율(카메라·프레임 평균) - 피팅 목표와 독립
- hamba: 손가락 15관절 굽힘 각의 시간 흐름이 HaMBA(카메라 평균 자세)와 얼마나 같이 움직이는지(상관계수), 평균 각 차이(도)
- motion: 관절별 굽힘 각의 프레임간 표준편차(도, 클수록 손가락이 움직임)
- jitter: 손목 기준 손가락 끝 위치의 2차 차분 평균(mm, 클수록 떨림)
결과: <결과 폴더>/finger_metrics.json
"""
import argparse, io, json, os, pickle, sys
from pathlib import Path
import numpy as np, torch
from PIL import Image

WORKER_HOME = Path(os.environ.get('KAIST_HAND_SHAPE_WORKER', str(Path.home() / 'Desktop/worker1/airflow')))
MONO = Path(json.loads((WORKER_HOME / 'config/gpu_4090_a.json').read_text())['pipeline_root']) / 'kaist-full/PhysTwin/Mono'


class U(pickle.Unpickler):
    def find_class(self, m, n):
        if m == 'torch.storage' and n == '_load_from_bytes': return lambda b: torch.load(io.BytesIO(b), map_location='cpu', weights_only=False)
        return super().find_class(m, n)


def load(p):
    with open(p, 'rb') as f: return U(f).load()


A = lambda v: v.detach().cpu().numpy() if torch.is_tensor(v) else np.asarray(v)
TIPS = [4, 8, 12, 16, 20]
FINGERS = {'thumb': [1, 2, 3, 4], 'index': [5, 6, 7, 8], 'middle': [9, 10, 11, 12], 'ring': [13, 14, 15, 16], 'pinky': [17, 18, 19, 20]}


def angles(R):  # (..., 3, 3) → 회전 각(도)
    return np.degrees(np.arccos(np.clip((np.trace(R, axis1=-2, axis2=-1) - 1) / 2, -1, 1)))


def so3_mean(Rs):
    M = np.mean(Rs, 0); Uu, _, Vt = np.linalg.svd(M); R = Uu @ Vt
    if np.linalg.det(R) < 0: Uu[:, -1] *= -1; R = Uu @ Vt
    return R


def kabsch(P, Q):  # P→Q 강체 변환
    pc, qc = P.mean(0), Q.mean(0); H = (P - pc).T @ (Q - qc); Uu, _, Vt = np.linalg.svd(H)
    d = np.sign(np.linalg.det(Vt.T @ Uu.T)); D = np.diag([1, 1, d]); R = Vt.T @ D @ Uu.T
    return R, qc - R @ pc


def main():
    p = argparse.ArgumentParser(); p.add_argument('out', type=Path); p.add_argument('--case', type=Path, required=True)
    p.add_argument('--hamba', type=Path, required=True); p.add_argument('--obs', type=Path, required=True); a = p.parse_args()
    os.chdir(MONO); sys.path.insert(0, 'data_process')
    from models.mano_wrapper import MANO
    mano = MANO(data_dir='data_process/Hamba/_DATA/data/', model_path='data_process/Hamba/_DATA/data/mano', gender='neutral', num_hand_joints=15,
                mean_params='data_process/Hamba/_DATA/data/mano_mean_params.npz', create_body_pose=False)
    params = load(a.out / 'hand_v1/hand_reg_params.pkl'); T = len(params)
    meta = json.loads((a.case / 'metadata.json').read_text()); Ks = np.asarray(meta['intrinsics'], float); W, H = meta['WH']
    c2w = np.asarray(pickle.load(open(a.case / 'calibrate.pkl', 'rb')), float); w2c = np.linalg.inv(c2w)
    obs = load(a.obs)['observations']
    # 손가락 점: 스키닝 가중치가 가장 큰 관절이 손가락 관절(1~15)인 점
    lw = A(mano.lbs_weights); finger_v = np.where(lw.argmax(1) >= 1)[0]
    hand_id = {}
    for c in range(4):
        info = json.loads((a.case / 'mask' / f'mask_info_{c}.json').read_text()); hand_id[c] = next(k for k, v in info.items() if isinstance(v, str) and 'hand' in v.lower())
    HP, J, V = [], [], []
    for f in range(T):
        r = params[f]['right']
        go, hp, be = [torch.as_tensor(A(r[k])).float().reshape(s) for k, s in (('global_orient', (1, 1, 3, 3)), ('hand_pose', (1, 15, 3, 3)), ('betas', (1, 10)))]
        out = mano(global_orient=go, hand_pose=hp, betas=be, pose2rot=False)
        vc, jc = A(out.vertices[0]).astype(float), A(out.joints[0]).astype(float)
        vw = A(r['verts']).astype(float); R, t = kabsch(vc, vw)
        J.append(jc @ R.T + t); V.append(vw); HP.append(A(hp[0]))
    J, V, HP = np.stack(J), np.stack(V), np.stack(HP)
    def proj(X, c):
        Xc = X @ w2c[c][:3, :3].T + w2c[c][:3, 3]; K = Ks[c]
        return np.stack([K[0, 0] * Xc[..., 0] / Xc[..., 2] + K[0, 2], K[1, 1] * Xc[..., 1] / Xc[..., 2] + K[1, 2]], -1)
    # MediaPipe 2D 오차
    errs = {j: [] for j in range(21)}
    for c in range(4):
        for f in range(T):
            k = np.asarray(obs[c]['kpts']['Left'][f], float).reshape(21, 2); cf = np.asarray(obs[c]['conf']['Left'][f], float).reshape(21)
            if cf.mean() < 0.5: continue
            e = np.linalg.norm(proj(J[f], c) - k, axis=1)
            for j in range(21):
                if cf[j] >= 0.5: errs[j].append(e[j])
    allv = np.concatenate([errs[j] for j in range(1, 21)]); tips = np.concatenate([errs[j] for j in TIPS])
    mp2d = {'all_median': round(float(np.median(allv)), 2), 'all_mean': round(float(allv.mean()), 2), 'tips_median': round(float(np.median(tips)), 2),
            'tips_mean': round(float(tips.mean()), 2), 'tips_p90': round(float(np.percentile(tips, 90)), 2), 'n': int(len(tips)),
            'fingers_median': {n: (round(float(np.median(np.concatenate([errs[j] for j in js]))), 2) if sum(len(errs[j]) for j in js) else None) for n, js in FINGERS.items()}}
    # 마스크 안 비율(손가락 점)
    inside = []
    for c in range(4):
        for f in range(T):
            mp = a.case / 'mask' / str(c) / hand_id[c] / f'{f}.png'
            if not mp.exists(): continue
            m = np.asarray(Image.open(mp).convert('L')) > 127
            if m.sum() < 200: continue
            uv = np.round(proj(V[f][finger_v], c)).astype(int); ok = (uv[:, 0] >= 0) & (uv[:, 0] < W) & (uv[:, 1] >= 0) & (uv[:, 1] < H)
            inside.append(float(m[uv[ok, 1], uv[ok, 0]].mean()) if ok.any() else 0.0)
    # HaMBA 손가락 자세(카메라 평균)
    hb = {}
    for c in range(4):
        try: d = torch.load(a.hamba / str(c) / 'results.pt', map_location='cpu', weights_only=False)
        except FileNotFoundError: continue
        for f in range(T):
            x = d.get(f'{f}_1')
            if x is not None: hb.setdefault(f, []).append(A(x['pred_mano_params']['hand_pose']).reshape(15, 3, 3))
    fs = sorted(hb); Hm = np.stack([np.stack([so3_mean([r[j] for r in hb[f]]) for j in range(15)]) for f in fs])
    fa, ha = angles(HP[fs]), angles(Hm)
    corr = [float(np.corrcoef(fa[:, j], ha[:, j])[0, 1]) for j in range(15) if ha[:, j].std() > 2 and fa[:, j].std() > 1e-3]
    rel = np.einsum('fjab,fjcb->fjac', HP[fs], Hm); geo = angles(rel)
    tip_rel = (J[:, TIPS] - J[:, :1]) * 1000; jit = np.linalg.norm(tip_rel[2:] - 2 * tip_rel[1:-1] + tip_rel[:-2], axis=-1)
    allang = angles(HP)
    res = {'frames': T, 'mp2d': mp2d, 'mask_inside_finger': round(float(np.mean(inside)), 4) if inside else None,
           'hamba': {'corr_mean': round(float(np.mean(corr)), 3) if corr else 0.0, 'corr_joints': len(corr), 'angle_diff_deg': round(float(geo.mean()), 2), 'frames': len(fs)},
           'motion_deg': round(float(allang.std(0).mean()), 2), 'hamba_motion_deg': round(float(ha.std(0).mean()), 2),
           'jitter_tip_mm': round(float(jit.mean()), 3), 'jitter_tip_p95_mm': round(float(np.percentile(jit, 95)), 3)}
    (a.out / 'finger_metrics.json').write_text(json.dumps(res, indent=1))
    print(json.dumps(res))


if __name__ == '__main__':
    main()
