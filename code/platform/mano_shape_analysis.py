#!/usr/bin/env python3
"""두 방식 비교 결과(mano_compare.py)의 β 차이 분석: 원래 방식이 에피소드마다 보정한 β ↔ 프로필 β.

hamba-env 파이썬으로 실행합니다(MANO 모델로 β → 기본 자세 손 치수 mm 환산).
  <hamba-env>/bin/python mano_shape_analysis.py /mnt/kaist-data/TEST/Set2
결과: <Set2>/shape_analysis.json (에피소드별 + 전체 요약)

항목
- β 10개: default(에피소드 보정) · 초기값(HaMBA 평균) · 프로필, 차이(L2·성분별)
- 보정 진단: 쓴 프레임 수, 초기값에서 조정 한계(±1.0)에 닿은 성분 수, 뼈 길이 비율 범위, 마지막 손실
- 기본 자세(손 편 상태) 치수: 손 길이(손목→중지 끝)·폭(검지↔소지 뿌리)·손가락 5개 길이·뼈 20개
- 전체: 작업 영상별 default 치수의 평균·표준편차 ↔ 프로필 값, 프로필을 만든 손 영상들 사이의 표준편차
"""
import ast, json, os, sys, time
from pathlib import Path
import numpy as np, torch

WORKER_HOME = Path(os.environ.get('KAIST_HAND_SHAPE_WORKER', str(Path.home() / 'Desktop/worker1/airflow')))
CFG = json.loads((WORKER_HOME / 'config/gpu_4090_a.json').read_text())
MONO = Path(CFG['pipeline_root']) / 'kaist-full/PhysTwin/Mono'
os.chdir(MONO); sys.path.insert(0, 'data_process')
from models.mano_wrapper import MANO  # noqa: E402

EDGES = [[0,1],[1,2],[2,3],[3,4],[0,5],[5,6],[6,7],[7,8],[0,9],[9,10],[10,11],[11,12],[0,13],[13,14],[14,15],[15,16],[0,17],[17,18],[18,19],[19,20]]
FINGERS = ['thumb', 'index', 'middle', 'ring', 'pinky']
mano = MANO(data_dir='data_process/Hamba/_DATA/data/', model_path='data_process/Hamba/_DATA/data/mano', gender='neutral', num_hand_joints=15,
            mean_params='data_process/Hamba/_DATA/data/mano_mean_params.npz', create_body_pose=False)


def measure(betas):
    b = torch.as_tensor(np.asarray(betas, np.float32).reshape(-1, 10)); n = len(b); eye = torch.eye(3)
    j = mano(global_orient=eye[None, None].expand(n, 1, 3, 3), hand_pose=eye[None, None].expand(n, 15, 3, 3), betas=b, pose2rot=False).joints.detach().numpy() * 1000
    out = []
    for x in j:
        bones = [float(np.linalg.norm(x[c] - x[a])) for a, c in EDGES]
        out.append({'length': round(float(np.linalg.norm(x[12] - x[0])), 1), 'width': round(float(np.linalg.norm(x[17] - x[5])), 1),
                    'fingers': {f: round(sum(bones[i * 4:(i + 1) * 4]), 1) for i, f in enumerate(FINGERS)}, 'bones': [round(v, 2) for v in bones]})
    return out


def flat(b):
    b = b[0] if isinstance(b[0], list) else b
    return [float(v) for v in b]


def last_loss(hist):
    try:
        h = ast.literal_eval(hist) if isinstance(hist, str) else hist
        return {k: round(float(v), 3) for k, v in h[-1].items() if k != 'step'}
    except (ValueError, SyntaxError, IndexError, TypeError, AttributeError):
        return None


def diff(a, b):
    return {'length': round(a['length'] - b['length'], 1), 'width': round(a['width'] - b['width'], 1),
            'fingers': {f: round(a['fingers'][f] - b['fingers'][f], 1) for f in FINGERS},
            'bone_max_abs': round(max(abs(x - y) for x, y in zip(a['bones'], b['bones'])), 2)}


def stats(rows, key):
    v = np.asarray([r[key] for r in rows], float)
    return {'mean': round(float(v.mean()), 1), 'sd': round(float(v.std()), 2), 'min': round(float(v.min()), 1), 'max': round(float(v.max()), 1)}


def main(root):
    root = Path(root); episodes = []; profile = None
    for s in sorted(root.glob('ep_*/summary.json')):
        ep = s.parent; summ = json.loads(s.read_text()); pid = summ['profile']['id']
        ss = ep / 'default/hand_v1/shared_shape.json'; pu = ep / pid / 'hand_v1/shape_profile_used.json'
        if not ss.exists() or not pu.exists(): continue
        d = json.loads(ss.read_text())['diagnostics']['right']; used = json.loads(pu.read_text())
        bd, bi, bp = flat(d['betas']), flat(d['initial_betas']), flat(used['betas']['right'])
        frames = d.get('frames'); frames = ast.literal_eval(frames) if isinstance(frames, str) else frames
        md, mi, mp = measure([bd, bi, bp])
        profile = profile or {'id': pid, 'sha256': summ['profile']['sha256'], 'betas': bp, 'size': mp, 'sources': used.get('profile', {}).get('sources')}
        episodes.append({'episode': ep.name, 'frames': summ.get('frames'),
                         'default': {'betas': [round(v, 4) for v in bd], 'size': md, 'calibration_frames': len(frames or []),
                                     'clamp_hits': int(sum(abs(a - b) >= 0.999 for a, b in zip(bd, bi))),
                                     'bone_ratio': [round(d.get('bone_length_ratio_min', 0), 3), round(d.get('bone_length_ratio_max', 0), 3)], 'final_loss': last_loss(d.get('history'))},
                         'hamba_initial': {'betas': [round(v, 4) for v in bi], 'size': mi},
                         'beta_diff': {'l2': round(float(np.linalg.norm(np.subtract(bd, bp))), 3), 'per_component': [round(a - b, 3) for a, b in zip(bd, bp)]},
                         'size_diff_default_minus_profile': diff(md, mp), 'between_meshes': summ.get('between_variants') and {k: v for k, v in summ['between_variants'].items() if k != 'per_frame_mean_mm'}})
    if not episodes: sys.exit('분석할 결과가 없습니다.')
    sizes = [e['default']['size'] for e in episodes]
    overall = {'episodes': len(episodes), 'default_length': stats(sizes, 'length'), 'default_width': stats(sizes, 'width'),
               'default_fingers': {f: stats([s['fingers'] for s in sizes], f) for f in FINGERS},
               'profile_size': {k: profile['size'][k] for k in ('length', 'width', 'fingers')},
               'mean_abs_diff': {'length': round(float(np.mean([abs(e['size_diff_default_minus_profile']['length']) for e in episodes])), 1),
                                 'width': round(float(np.mean([abs(e['size_diff_default_minus_profile']['width']) for e in episodes])), 1),
                                 'beta_l2': round(float(np.mean([e['beta_diff']['l2'] for e in episodes])), 3)},
               'clamp_hits_mean': round(float(np.mean([e['default']['clamp_hits'] for e in episodes])), 1),
               'calibration_frames_mean': round(float(np.mean([e['default']['calibration_frames'] for e in episodes])), 1),
               'mano_mean_hand': {k: measure([[0.0] * 10])[0][k] for k in ('length', 'width')}}
    src = profile.get('sources') or {}
    if src:
        ss = measure([v['betas'] for v in src.values()])
        overall['profile_source_spread'] = {'episodes': len(ss), 'length': stats(ss, 'length'), 'width': stats(ss, 'width')}
    out = {'created': time.strftime('%Y-%m-%dT%H:%M:%S'), 'profile': {k: profile[k] for k in ('id', 'sha256', 'betas', 'size')}, 'overall': overall, 'episodes': episodes}
    tmp = root / '.shape_analysis.json.tmp'; tmp.write_text(json.dumps(out, ensure_ascii=False, indent=1)); tmp.replace(root / 'shape_analysis.json')
    print(f"{'에피소드':22}{'default 길이/폭':>16}{'User2 길이/폭':>16}{'차이':>14}{'β L2':>7}{'한계':>5}{'보정프레임':>8}")
    for e in episodes:
        a, p, dd = e['default']['size'], profile['size'], e['size_diff_default_minus_profile']
        print(f"{e['episode']:22}{a['length']:>9.1f}/{a['width']:<6.1f}{p['length']:>9.1f}/{p['width']:<6.1f}{dd['length']:>+7.1f}/{dd['width']:<+6.1f}{e['beta_diff']['l2']:>7.2f}{e['default']['clamp_hits']:>5}{e['default']['calibration_frames']:>8}")
    print(json.dumps(overall, ensure_ascii=False))


if __name__ == '__main__':
    main(sys.argv[1] if len(sys.argv) > 1 else '/mnt/kaist-data/TEST/Set2')
