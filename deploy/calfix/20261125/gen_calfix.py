"""11-25 보정 회차(10:41·14:41, 보드 크기 설정 오류로 카메라 간 거리가 짧게 나옴)의 RAW에 대해,
전처리(preprocess_rgbd.py)와 같은 코드로 calibrate.pkl / world_reorient_M.npy 교체본을 만든다.

교체 extrinsics = 그 RAW 자체 extrinsics 의 회전은 그대로, 이동(T)만 1/s 배.
  s = 기준 보정(11-25 16:29, 23a443a9) 대비 카메라 간 거리 배율.
      기준과 회전이 1° 넘게 다른 카메라(=실제로 움직인 카메라, 10:41 회차의 cam_2)는 s 계산에서 뺀다.
  (10:41 회차는 cam_2 위치가 16:29 와 달라 16:29 값을 그대로 쓰면 cam_2 가 ~38mm 어긋난다.)

  python -I gen_calfix.py <preprocess_rgbd.py> <기준RAW> <out_root> <fps> <ep>...
"""
import sys, json, os, io, pickle, hashlib, itertools, importlib.util, numpy as np
mod_path, ref_raw, out_root, fps = sys.argv[1], sys.argv[2], sys.argv[3], float(sys.argv[4]); eps = sys.argv[5:]
sys.path.insert(0, os.path.dirname(mod_path))
spec = importlib.util.spec_from_file_location('preprocess_rgbd', mod_path); pp = importlib.util.module_from_spec(spec); spec.loader.exec_module(pp)
ref = pp.load_extrinsics(ref_raw)
sha = lambda b: hashlib.sha256(b).hexdigest()


def center(R, T):
    return -np.asarray(R).T @ np.asarray(T).ravel()


def rot_deg(A, B):
    return float(np.degrees(np.arccos(np.clip((np.trace(np.asarray(A) @ np.asarray(B).T) - 1) / 2, -1, 1))))


def scale_info(extr):
    cams = sorted(set(extr) & set(ref))
    moved = [c for c in cams if rot_deg(extr[c][0], ref[c][0]) > 1.0]
    keep = [c for c in cams if c not in moved]
    ratios = {f'{a}-{b}': float(np.linalg.norm(center(*extr[a]) - center(*extr[b])) / np.linalg.norm(center(*ref[a]) - center(*ref[b])))
              for a, b in itertools.combinations(keep, 2)}
    return float(np.median(list(ratios.values()))), ratios, moved


def timestamps(src, cams):
    """옛 전처리(host_time_s, 카메라별 호출)와 11-27 이후 전처리(카메라 시계 우선, 전체 한 번 호출) 모두 지원."""
    if hasattr(pp, 'sync_times'):
        return pp.load_timestamps(src, cams)
    return [pp.load_timestamps(src, c) for c in cams], 'host_time_s'


def build(src, extr):
    cams = pp.discover_cameras(src); Ks = [pp.load_intrinsic(src, c)[0] for c in cams]
    cam_ts, basis = timestamps(src, cams); cam_depths = [pp.depth_frames(src, c) for c in cams]
    c2ws = [pp.w2c_to_c2w(*extr[c]) for c in cams]
    synced, _ = pp.sync_frames(cam_ts, fps, 25.0 / 1000.0); N = len(synced); o = N // 2
    scene = np.concatenate([pp.lift_to_world(np.load(cam_depths[i][synced[o][i]]), Ks[i], c2ws[i], zmin=0.1, zmax=3.0) for i in range(len(cams))], 0)
    M, c2ws, info = pp.reorient_world_frame(c2ws, scene)
    b = io.BytesIO(); pickle.dump([c for c in c2ws], b); m = io.BytesIO(); np.save(m, M)
    return b.getvalue(), m.getvalue(), info, basis


for ep in eps:
    src = f'/mnt/kaist-data/rgbd_raw/{ep}'
    own = pp.load_extrinsics(src)
    s, ratios, moved = scale_info(own)
    fixed = {c: (np.asarray(R), np.asarray(T) / s) for c, (R, T) in own.items()}
    cb, cm, ci, basis = build(src, own); fb, fm, fi, _ = build(src, fixed)
    # 동기화 기준별로 따로 저장: host_time_s → fps15, 카메라 시계 → fps15_hw (apply 가 현재 파일과 맞는 쪽을 고름)
    d = f'{out_root}/{ep}/fps{fps:g}' + ('' if basis == 'host_time_s' else '_hw'); os.makedirs(d, exist_ok=True)
    open(f'{d}/calibrate.pkl', 'wb').write(fb); open(f'{d}/world_reorient_M.npy', 'wb').write(fm)
    rec = {'raw': ep, 'fps': fps, 'timestamp_basis': basis, 'preprocess_sha256': sha(open(mod_path, 'rb').read()), 'raw_extrinsics_sha256': sha(open(f'{src}/extrinsics.json', 'rb').read()),
           'method': 'own extrinsics, T / s', 'scale_s': s, 'scale_pairs': ratios, 'moved_cameras_excluded': moved,
           'scale_reference': os.path.basename(ref_raw), 'scale_reference_extrinsics_sha256': sha(open(f'{ref_raw}/extrinsics.json', 'rb').read()),
           'expected_current': {'calibrate.pkl': sha(cb), 'world_reorient_M.npy': sha(cm)}, 'new': {'calibrate.pkl': sha(fb), 'world_reorient_M.npy': sha(fm)},
           'reorient_info': {'current': ci, 'new': fi}}
    json.dump(rec, open(f'{d}/record.json', 'w'), indent=1); print(ep, 'OK', f's={s:.4f}', basis, flush=True)
