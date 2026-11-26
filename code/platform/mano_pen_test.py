#!/usr/bin/env python3
"""손-물체 관통 방지 시험 (3D 복원이 끝난 에피소드, 시험용 · 운영과 분리).

  python3 mano_pen_test.py --project /mnt/kaist-data/work/pc5 --episode ep_20261126_005859_1 --wpen 0 10 50 200

- 입력은 그 PC의 작업 폴더(NAS)를 읽기만 합니다: 03_prepared, 06_reconstruction/<ep>/mask·final_data.pkl, hands/hamba(재사용)
- 조건마다 운영 MANO와 같은 설정(기본 β 보정, 손가락 시간축 5e5)에 관통 방지 항 가중치(w_pen)만 바꿉니다.
  관통 계산은 시험용 코드 사본의 그래프 호환 버전(손 근처 15cm 물체 점만, CUDA Graph 유지).
- 평가: 3D 관통(물체 점이 손 표면 안쪽 1mm 넘게 들어간 프레임 수·깊이), 엄지 끝-물체 거리, 손-물체 최소 거리,
        SAM3 마스크 IoU, MediaPipe 관절 오차, 피팅 시간
결과: ~/kaist_work/mano_compare/pen/<ep>/<조건>/, pen_test.json
"""
import argparse, io, json, os, pickle, shutil, subprocess, sys, time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import mano_finger_sweep as sw  # noqa: E402
mc = sw.mc

CONTACT = r'''
import pickle, io, sys, os, json, numpy as np, torch
from scipy.spatial import cKDTree
class U(pickle.Unpickler):
    def find_class(self, m, n):
        if m == 'torch.storage' and n == '_load_from_bytes': return lambda b: torch.load(io.BytesIO(b), map_location='cpu', weights_only=False)
        return super().find_class(m, n)
sys.path.insert(0, 'data_process'); from models.mano_wrapper import MANO
faces = np.asarray(MANO(data_dir='data_process/Hamba/_DATA/data/', model_path='data_process/Hamba/_DATA/data/mano', gender='neutral', num_hand_joints=15,
                        mean_params='data_process/Hamba/_DATA/data/mano_mean_params.npz', create_body_pose=False).faces, np.int64)
TIPS = [745, 317, 444, 556, 673]
def vn(V):
    fn = np.cross(V[faces[:, 1]] - V[faces[:, 0]], V[faces[:, 2]] - V[faces[:, 0]]); n = np.zeros_like(V)
    for k in range(3): np.add.at(n, faces[:, k], fn)
    return n / np.maximum(np.linalg.norm(n, axis=1, keepdims=True), 1e-12)
a = json.loads(sys.argv[1]); d = pickle.load(open(a['final'], 'rb')); O = np.asarray(d['object_points']); vis = np.asarray(d['object_motions_valid']).astype(bool)
for out in a['outs']:
    reg = U(open(out + '/hand_v1/hand_reg.pkl', 'rb')).load(); T = min(len(reg), len(O)); thumb = []; hmin = []; depth = []; nfr = 0; npts = []
    for f in range(T):
        V = np.asarray(reg[f]['right'], float); P = O[f][vis[f]] if vis.shape == O.shape[:2] else O[f]
        if len(P) < 50: continue
        tr = cKDTree(P); thumb.append(tr.query(V[TIPS[0]])[0] * 1000); hmin.append(tr.query(V)[0].min() * 1000)
        dd, idx = cKDTree(V).query(P); m = dd < 0.02; best = 0.0; cnt = 0
        if m.any():
            s = np.einsum('ij,ij->i', P[m] - V[idx[m]], vn(V)[idx[m]]) * 1000; ins = s < -1.0; cnt = int(ins.sum())
            if cnt: best = float((-s[ins]).max())
        depth.append(best); npts.append(cnt); nfr += best > 0
    th, hm, dp = np.asarray(thumb), np.asarray(hmin), np.asarray(depth)
    res = {'frames': len(dp), 'pen_frames': int(nfr), 'pen_depth_max_mm': round(float(dp.max()), 2), 'pen_depth_mean_mm': round(float(dp[dp > 0].mean()) if nfr else 0.0, 2),
           'pen_points_mean': round(float(np.mean(npts)), 1), 'thumb_obj_median_mm': round(float(np.median(th)), 1), 'thumb_within5_pct': round(float(np.mean(th <= 5) * 100), 1),
           'thumb_within15_pct': round(float(np.mean(th <= 15) * 100), 1), 'hand_obj_min_median_mm': round(float(np.median(hm)), 1)}
    json.dump(res, open(out + '/contact_3d.json', 'w'), indent=1); print(os.path.basename(out), json.dumps(res))
'''


def main():
    p = argparse.ArgumentParser(); p.add_argument('--project', type=Path, required=True); p.add_argument('--episode', required=True)
    p.add_argument('--wpen', nargs='+', type=float, default=[0, 10, 50, 200]); p.add_argument('--published', type=Path, default=None)
    a = p.parse_args(); ep = a.episode
    case = a.project / '06_reconstruction' / ep; prepared = mc.fh.source(case); final = case / 'final_data.pkl'
    root, pt, env = mc.fh.pipeline.runtime(); mono = pt / 'Mono'; python = str(root / 'hamba-env/bin/python')
    env['PYTHONPATH'] = str(mono / 'data_process') + os.pathsep + str(mono / 'data_process/Hamba') + os.pathsep + env.get('PYTHONPATH', '')
    env.pop('DISPLAY', None); env.pop('XAUTHORITY', None); env['PYOPENGL_PLATFORM'] = 'egl'; env['HF_HUB_OFFLINE'] = '1'
    side, side_source = mc.fh.resolve_side(root, ep, 'right'); mask, infos = mc.fh.hand_mask_inputs(case)
    T = int(json.loads((prepared / 'metadata.json').read_text())['frame_num'])
    base = mc.WORK / 'pen' / ep; base.mkdir(parents=True, exist_ok=True); logf = base / 'pen.log'
    patched = sw.patched_code(); st = {'episode': ep, 'project': str(a.project), 'started': time.strftime('%Y-%m-%d %H:%M:%S'), 'variants': {}}
    save = lambda: (base / 'pen_test.json').write_text(json.dumps(st, ensure_ascii=False, indent=1))
    outs = []
    for wp in a.wpen:
        name = f'wpen{wp:g}'; out = base / name; outs.append(out)
        if not (out / 'review/mano_review.mp4').exists():
            out.mkdir(exist_ok=True)
            if not (out / 'hamba').exists(): (out / 'hamba').symlink_to(case / 'hands/hamba')  # 그 PC의 HaMBA 결과 재사용
            work, runmono = mc.build_view(out, prepared, mono, ep, mask, infos)
            if not (work / 'final_data.pkl').exists(): (work / 'final_data.pkl').symlink_to(final)
            code = runmono / 'data_process/data_process_hands.py'
            if code.is_symlink(): code.unlink()
            shutil.copy2(patched, code)
            settings = mc.fh.fit_settings(side, side_source, None); settings.update(w_pen=wp, experiment='pen-test')
            (out / 'settings.json').write_text(json.dumps(settings, indent=2))
            if (case / 'hands/observations.pkl').exists() and not (out / 'observations.pkl').exists(): shutil.copy2(case / 'hands/observations.pkl', out / 'observations.pkl')
            cmd = [python, '-u', str(code), '--hamba_base_path', str(out / 'hamba'), '--phystwin_base_path', str(work.parent), '--case_name', ep,
                   '--temp_weight', '500000', '--hamba_weight', '0.01', '--log', 'hand_v1', '--seed', '1027', '--num_iters', '1500', '--num_hands', '1', '--num_cams', '4',
                   '--resume', '--observations-cache', str(out / 'observations.pkl'), '--w_pen', str(wp)]
            for k in ('w_mask', 'w_mask_cover', 'cover_max_px', 'wrist_weight', 'w_silhouette', 'silhouette_size', 'silhouette_every', 'pose_temp_weight', 'w_pose_prior',
                      'camera_init_iters', 'fit_shape_lr', 'fit_shape_max_delta', 'fit_shape_temporal_weight', 'robust_joint_delta'):
                cmd += ['--' + k, str(settings[k])]
            cmd += ['--staged_fit', '--shared_shape'] + [x for k in ('shape_frames', 'shape_frame_count', 'shape_iters', 'shape_lr', 'shape_prior_weight', 'shape_max_delta') for x in ('--' + k, str(settings[k]))]
            t0 = time.time(); st['current'] = name; save()
            mc.gpu(lambda: mc.run_logged(cmd, runmono, env, out / 'logs/19_mano.log'), f'관통 시험 {ep} {name}', logf)
            fit_s = round(time.time() - t0)
            mc.run_logged([python, str(mc.fh.HERE / 'hand_stage_worker.py'), 'fit', '--out', out, '--case', work, '--frames', T, '--side', side], runmono, env, out / 'logs/validate.log')
            mc.run_logged([python, str(mc.fh.HERE / 'hand_stage_worker.py'), 'preview', '--out', out, '--case', work, '--frames', T, '--side', side], runmono, env, out / 'logs/validate.log')
            (out / 'fit_seconds.txt').write_text(str(fit_s))
        st['variants'][name] = {'fit_seconds': int((out / 'fit_seconds.txt').read_text()) if (out / 'fit_seconds.txt').exists() else None}; save()
    # 평가
    pub = None
    if a.published:  # 운영 결과 폴더에는 쓰지 않도록 손 결과만 복사해 평가
        pub = base / 'published'; (pub / 'hand_v1').mkdir(parents=True, exist_ok=True)
        shutil.copy2(a.published / 'hands/hand_v1/hand_reg.pkl', pub / 'hand_v1/hand_reg.pkl')
    targets = [str(o) for o in outs] + ([str(pub)] if pub else [])
    r = subprocess.run([python, '-c', CONTACT, json.dumps({'final': str(final), 'outs': targets})], cwd=mono, env=env, capture_output=True, text=True, timeout=1800)
    print(r.stdout, r.stderr[-500:] if r.returncode else '')
    work0 = outs[0] / '_runtime/inputs' / ep
    for o in outs:
        subprocess.run([python, str(HERE / 'mano_mask_iou.py'), str(o), '--case', str(work0)], env=env, capture_output=True, text=True, timeout=900)
        subprocess.run([python, str(HERE / 'mano_finger_eval.py'), str(o), '--case', str(work0), '--hamba', str(case / 'hands/hamba' / ep), '--obs', str(outs[0] / 'observations.pkl')],
                       env=env, capture_output=True, text=True, timeout=900)
        n = o.name; v = st['variants'].setdefault(n, {})
        for f, k in (('contact_3d.json', 'contact'), ('mask_iou.json', 'mask_iou'), ('finger_metrics.json', 'finger')):
            try: v[k] = json.loads((o / f).read_text())
            except (OSError, ValueError): pass
    if a.published:
        try: st['published'] = json.loads((pub / 'contact_3d.json').read_text())
        except (OSError, ValueError): pass
    st.pop('current', None); st['finished'] = time.strftime('%Y-%m-%d %H:%M:%S'); save()


if __name__ == '__main__':
    main()
