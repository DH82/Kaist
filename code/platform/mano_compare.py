#!/usr/bin/env python3
"""같은 에피소드를 MANO β 두 방식으로 피팅해 NAS에 나란히 저장합니다 (비교용, 운영 실행과 분리).

  default : 운영 기본 = 에피소드마다 공유 형상 보정(mano-geometric-shared-shape-v6)
  <프로필> : 사람별 손 형상 프로필로 β 고정(mano-geometric-profile-shape-v1), 예: User2

입력은 운영 MANO 단계와 같습니다: 03_prepared(영상·카메라) + 06_reconstruction/<ep>의 승인된 손 마스크.
(MANO 피팅 자체는 3D 복원 결과를 쓰지 않으므로 추적 검수까지 끝난 에피소드면 됩니다.)
명령·설정은 Worker 파이프라인 사본의 18_fit_hands.py(fit_settings·명령 구성)와 같고, HaMBA는 두 방식이 같은 결과를 함께 씁니다.
GPU는 Worker와 같은 슬롯 잠금을 기다렸다가 씁니다. 운영 작업 폴더(06_reconstruction/<ep>/hands)는 건드리지 않습니다.

  python3 mano_compare.py --profile User2 --out /mnt/kaist-data/TEST/Set2 ep_20261126_003904 ...

결과: <out>/<ep>/default/, <out>/<ep>/<프로필>/ (hand_v1 결과·settings.json·review/mano_review.mp4·logs),
      <out>/<ep>/compare.mp4 (왼쪽 default · 오른쪽 프로필, 카메라 4대 투영), <out>/<ep>/summary.json
"""
import argparse, hashlib, importlib, json, os, shutil, subprocess, sys, time, traceback
from pathlib import Path

WORKER_HOME = Path(os.environ.get('KAIST_HAND_SHAPE_WORKER', str(Path.home() / 'Desktop/worker1/airflow')))
CFG = json.loads((WORKER_HOME / 'config/gpu_4090_a.json').read_text())
SCRIPTS = Path(CFG['scripts_dir']); PROJECT = Path(CFG['project_dir'])
PROFILES = Path(CFG['nas_mount']) / 'control/hand_profiles'
WORK = Path(os.environ.get('KAIST_MANO_COMPARE_WORK', str(Path.home() / 'kaist_work/mano_compare')))
sys.path.insert(0, str(SCRIPTS)); sys.path.insert(0, str(WORKER_HOME / 'worker'))
os.chdir(SCRIPTS)
fh = importlib.import_module('18_fit_hands')
import gpu_slots  # noqa: E402
LOCK = str(WORKER_HOME / 'worker/gpu_4090_a.lock')

SIZE_CODE = r'''
import json, sys, pickle, io
import numpy as np, torch
sys.path.insert(0, 'data_process')
from models.mano_wrapper import MANO
class U(pickle.Unpickler):
    def find_class(self, m, n):
        if m == 'torch.storage' and n == '_load_from_bytes': return lambda b: torch.load(io.BytesIO(b), map_location='cpu', weights_only=False)
        return super().find_class(m, n)
def load(p):
    with open(p, 'rb') as f: return U(f).load()
a = json.loads(sys.argv[1])
mano = MANO(data_dir='data_process/Hamba/_DATA/data/', model_path='data_process/Hamba/_DATA/data/mano', gender='neutral', num_hand_joints=15,
            mean_params='data_process/Hamba/_DATA/data/mano_mean_params.npz', create_body_pose=False)
out = {}
b = torch.as_tensor(np.asarray(a['betas'], np.float32).reshape(-1, 10)); n = len(b); eye = torch.eye(3)
j = mano(global_orient=eye[None, None].expand(n, 1, 3, 3), hand_pose=eye[None, None].expand(n, 15, 3, 3), betas=b, pose2rot=False).joints.detach().numpy() * 1000
out['size'] = [{'length': round(float(np.linalg.norm(x[12] - x[0])), 1), 'width': round(float(np.linalg.norm(x[17] - x[5])), 1)} for x in j]
regs = [load(p) for p in a['regs']]
if len(regs) == 2:
    frames = sorted(set(regs[0]) & set(regs[1])); d = []; wrist = []
    for f in frames:
        v0 = np.asarray(regs[0][f]['right'], np.float64); v1 = np.asarray(regs[1][f]['right'], np.float64)
        d.append(np.linalg.norm(v0 - v1, axis=1)); wrist.append(np.linalg.norm(v0.mean(0) - v1.mean(0)))
    d = np.stack(d) * 1000; wrist = np.asarray(wrist) * 1000
    out['between'] = {'frames': len(frames), 'vertex_mean_mm': round(float(d.mean()), 2), 'vertex_p95_mm': round(float(np.percentile(d, 95)), 2),
                      'vertex_max_mm': round(float(d.max()), 2), 'center_mean_mm': round(float(wrist.mean()), 2), 'center_max_mm': round(float(wrist.max()), 2),
                      'per_frame_mean_mm': [round(float(x), 2) for x in d.mean(1)]}
print(json.dumps(out))
'''


def log(msg, f=None):
    line = time.strftime('[%H:%M:%S] ') + msg
    print(line, flush=True)
    if f:
        with open(f, 'a') as h: h.write(line + '\n')


def write_status(status):
    WORK.mkdir(parents=True, exist_ok=True)
    tmp = WORK / 'status.json.tmp'; tmp.write_text(json.dumps(status, ensure_ascii=False, indent=1)); tmp.replace(WORK / 'status.json')


def sha256(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def build_view(out, prepared, mono, ep, mask, infos):
    """18_fit_hands.prepare_view와 같은 런타임 폴더(Mono 링크 + inputs/<ep>), 승인 마스크는 운영 06_reconstruction 것을 읽기만 함."""
    runtime = out / '_runtime'; runmono = runtime / 'Mono'; data = runmono / 'data_process'; data.mkdir(parents=True, exist_ok=True)
    for p in mono.iterdir():
        if p.name != 'data_process': fh.link(runmono / p.name, p)
    for p in (mono / 'data_process').iterdir(): fh.link(data / p.name, p)
    case = runtime / 'inputs' / ep; case.mkdir(parents=True, exist_ok=True)
    wh = json.loads((prepared / 'metadata.json').read_text())['WH']
    if list(wh) != list(fh.HAND_WH):
        fh.resized_view(case, prepared, mask, infos, wh)
    else:
        for name in ('color', 'metadata.json', 'calibrate.pkl', 'source_frames.json'): fh.link(case / name, prepared / name)
        (case / 'mask').mkdir(exist_ok=True)
        for c, info in infos.items():
            fh.link(case / 'mask' / str(c), mask / str(c)); fh.link(case / 'mask' / f'mask_info_{c}.json', info)
    (out / 'hand_v1').mkdir(exist_ok=True); fh.link(case / 'hand_v1', out / 'hand_v1')
    return case, runmono


def run_logged(cmd, cwd, env, logfile):
    logfile.parent.mkdir(parents=True, exist_ok=True)
    with open(logfile, 'a') as f:
        f.write(time.strftime('\n[%H:%M:%S] $ ') + ' '.join(map(str, cmd)) + '\n'); f.flush()
        r = subprocess.run([str(c) for c in cmd], cwd=cwd, env=env, stdout=f, stderr=subprocess.STDOUT)
    if r.returncode: raise RuntimeError(f'종료코드 {r.returncode}: {logfile.name} 확인')


def gpu(fn, what, logf):
    lockf, slot = gpu_slots.acquire(LOCK)
    try:
        log(f'{what} (GPU 슬롯 {slot})', logf); return fn()
    finally: lockf.close()


def compare(ep, profile_id, dest_root, status):
    case = PROJECT / '06_reconstruction' / ep
    base = WORK / ep; base.mkdir(parents=True, exist_ok=True); logf = base / 'compare.log'
    prepared = fh.source(case)
    root, pt, env = fh.pipeline.runtime(); mono = pt / 'Mono'; python = str(root / 'hamba-env/bin/python')
    env['PYTHONPATH'] = str(mono / 'data_process') + os.pathsep + str(mono / 'data_process/Hamba') + os.pathsep + env.get('PYTHONPATH', '')
    env.pop('DISPLAY', None); env.pop('XAUTHORITY', None); env['PYOPENGL_PLATFORM'] = 'egl'; env['HF_HUB_OFFLINE'] = '1'
    side, side_source = fh.resolve_side(root, ep, 'right')
    mask, infos = fh.hand_mask_inputs(case)
    T = int(json.loads((prepared / 'metadata.json').read_text())['frame_num'])
    profile_path = PROFILES / f'{profile_id}.json'
    profile, psha = fh.hand_profile.load(profile_path)
    variants = {'default': None, profile_id: {'id': profile['profile_id'], 'sha256': psha}}
    log(f'== {ep}: 프레임 {T}, 손 {side}({side_source}), 비교 default / {profile_id}({psha[:12]})', logf)
    hamba = base / 'hamba'; hamba.mkdir(exist_ok=True)
    views = {}
    for v in variants:
        out = base / v; out.mkdir(exist_ok=True)
        if not (out / 'hamba').exists(): (out / 'hamba').symlink_to(hamba)
        views[v] = build_view(out, prepared, mono, ep, mask, infos)
    first = next(iter(views.values()))[0]
    if not all((hamba / ep / str(c) / 'results.pt').exists() for c in range(4)):
        status['episodes'][ep].update(step='HaMBA 손 복원'); write_status(status)
        gpu(lambda: run_logged([python, '-u', 'script_hand_recon.py', '--img_folder', first.parent, '--out_folder', hamba],
                               mono / 'data_process/Hamba', env, base / 'logs/18_hamba.log'), '18 · HaMBA 손 복원', logf)
    out0 = base / 'default'
    run_logged([python, str(fh.HERE / 'hand_stage_worker.py'), 'hamba', '--out', out0, '--case', views['default'][0], '--frames', T, '--side', side],
               views['default'][1], env, base / 'logs/18_hamba_check.log')
    results = {}
    for v, prof in variants.items():
        out = base / v; work, runmono = views[v]
        settings = fh.fit_settings(side, side_source, prof)
        (out / 'settings.json').write_text(json.dumps(settings, indent=2))
        if not (out / 'review/mano_review.mp4').exists():
            cmd = [python, '-u', str(runmono / 'data_process/data_process_hands.py'), '--hamba_base_path', str(out / 'hamba'), '--phystwin_base_path', str(work.parent),
                   '--case_name', ep, '--temp_weight', '500000', '--hamba_weight', '0.01', '--log', 'hand_v1', '--seed', '1027', '--num_iters', '1500',
                   '--num_hands', '1' if side == 'right' else '2', '--num_cams', '4', '--resume', '--observations-cache', str(out / 'observations.pkl')]
            for key in ('w_mask', 'w_mask_cover', 'cover_max_px', 'wrist_weight', 'w_silhouette', 'silhouette_size', 'silhouette_every', 'pose_temp_weight',
                        'w_pose_prior', 'camera_init_iters', 'fit_shape_lr', 'fit_shape_max_delta', 'fit_shape_temporal_weight', 'robust_joint_delta'):
                cmd += ['--' + key, str(settings[key])]
            if settings['fit_shape']: cmd += ['--fit_shape']
            if settings['staged_fit']: cmd += ['--staged_fit']
            if settings.get('mp_crop'): cmd += ['--mp_crop']
            if settings.get('mp_smooth'): cmd += ['--mp_smooth', str(settings['mp_smooth'])]
            if settings.get('shape_profile'):
                cmd += ['--shape_profile', str(profile_path), '--shape_profile_sha256', settings['shape_profile']['sha256']]
            if settings['shared_shape']:
                cmd += ['--shared_shape']
                for key in ('shape_frames', 'shape_frame_count', 'shape_iters', 'shape_lr', 'shape_prior_weight', 'shape_max_delta'):
                    cmd += ['--' + key, str(settings[key])]
            status['episodes'][ep].update(step=f'MANO 피팅 · {v}'); write_status(status)
            t0 = time.time()
            gpu(lambda: run_logged(cmd, runmono, env, out / 'logs/19_mano.log'), f'19 · MANO 피팅 ({v})', logf)
            log(f'   {v} 피팅 {time.time() - t0:.0f}초', logf)
            run_logged([python, str(fh.HERE / 'hand_stage_worker.py'), 'fit', '--out', out, '--case', work, '--frames', T, '--side', side], runmono, env, out / 'logs/validate.log')
            run_logged([python, str(fh.HERE / 'hand_stage_worker.py'), 'preview', '--out', out, '--case', work, '--frames', T, '--side', side], runmono, env, out / 'logs/validate.log')
        hv = out / 'hand_v1'
        betas = None
        if (hv / 'shared_shape.json').exists():
            d = json.loads((hv / 'shared_shape.json').read_text())['diagnostics']['right']; betas = d['betas'][0] if isinstance(d['betas'][0], list) else d['betas']
        elif (hv / 'shape_profile_used.json').exists():
            betas = json.loads((hv / 'shape_profile_used.json').read_text())['betas']['right']
        results[v] = {'algorithm': settings['algorithm'], 'betas': [round(float(x), 4) for x in betas] if betas else None}
    # 비교 수치: 기본 자세 손 크기, 두 결과 사이 메시 거리
    names = list(variants)
    status['episodes'][ep].update(step='비교 수치·영상'); write_status(status)
    r = subprocess.run([python, '-c', SIZE_CODE, json.dumps({'betas': [results[v]['betas'] for v in names], 'regs': [str(base / v / 'hand_v1/hand_reg.pkl') for v in names]})],
                       cwd=mono, env=env, capture_output=True, text=True, timeout=600)
    metrics = json.loads(r.stdout.strip().splitlines()[-1]) if r.returncode == 0 else {'error': r.stderr[-500:]}
    for i, v in enumerate(names):
        if 'size' in metrics: results[v]['hand_size_mm'] = metrics['size'][i]
    # 저장
    dest = dest_root / ep; dest.mkdir(parents=True, exist_ok=True)
    for v in names:
        out = base / v; d = dest / v
        tmp = dest / f'.{v}.tmp'
        if tmp.exists(): shutil.rmtree(tmp)
        (tmp / 'hand_v1').mkdir(parents=True); (tmp / 'review').mkdir(); (tmp / 'logs').mkdir()
        for name in ('hand_reg.pkl', 'hand_reg_params.pkl', 'hand_reg_cidx.pkl', 'integrity.json', 'shared_shape.json', 'shared_shape.pkl', 'shape_profile_used.json'):
            if (out / 'hand_v1' / name).exists(): shutil.copy2(out / 'hand_v1' / name, tmp / 'hand_v1' / name)
        shutil.copy2(out / 'settings.json', tmp / 'settings.json')
        for name in ('mano_review.mp4', 'validation.json'): shutil.copy2(out / 'review' / name, tmp / 'review' / name)
        for p in (out / 'logs').glob('*.log'): shutil.copy2(p, tmp / 'logs' / p.name)
        if d.exists(): shutil.rmtree(d)
        tmp.rename(d)
    # 나란히 비교 영상(왼쪽 default, 오른쪽 프로필)
    vids = [str(base / v / 'review/mano_review.mp4') for v in names]
    cmp_tmp = base / 'compare.mp4'
    label = lambda i, t: f"[{i}:v]scale=960:-2,drawbox=x=0:y=ih-34:w=iw:h=34:color=black@0.7:t=fill,drawtext=text='{t}':x=10:y=h-26:fontsize=20:fontcolor=white[v{i}]"
    graph = f"{label(0, 'default (episode shared-shape beta)')};{label(1, profile_id + ' (fixed profile beta)')};[v0][v1]hstack=inputs=2"
    r = subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-y', '-i', vids[0], '-i', vids[1], '-filter_complex', graph, '-c:v', 'libx264', '-crf', '22', '-pix_fmt', 'yuv420p', str(cmp_tmp)], capture_output=True, text=True)
    if r.returncode:  # 글꼴이 없으면 글자 없이
        graph = '[0:v]scale=960:-2[v0];[1:v]scale=960:-2[v1];[v0][v1]hstack=inputs=2'
        subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-y', '-i', vids[0], '-i', vids[1], '-filter_complex', graph, '-c:v', 'libx264', '-crf', '22', '-pix_fmt', 'yuv420p', str(cmp_tmp)], check=True)
    shutil.copy2(cmp_tmp, dest / 'compare.mp4')
    summary = {'episode': ep, 'frames': T, 'side': side, 'created': time.strftime('%Y-%m-%dT%H:%M:%S%z'), 'variants': results,
               'profile': {'id': profile_id, 'sha256': psha, 'path': str(profile_path)},
               'between_variants': metrics.get('between'), 'inputs': {'prepared': str(prepared), 'masks': str(mask)},
               'code': {'data_process_hands.py': sha256(mono / 'data_process/data_process_hands.py'), '18_fit_hands.py': sha256(fh.HERE / '18_fit_hands.py')},
               'note': 'MANO 피팅만 비교(운영 MANO 단계와 같은 명령·설정). 3D 복원·물체 결과와 무관. 운영 결과(results/)와 별개의 비교용 사본.'}
    (dest / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=1))
    b = metrics.get('between') or {}
    log(f'== {ep} 완료: ' + ' / '.join(f"{v} 손 {results[v].get('hand_size_mm', {}).get('length')}mm" for v in names) +
        (f" · 두 결과 메시 거리 평균 {b.get('vertex_mean_mm')}mm 최대 {b.get('vertex_max_mm')}mm" if b else '') + f' → {dest}', logf)
    return summary


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('episodes', nargs='+'); p.add_argument('--profile', required=True); p.add_argument('--out', type=Path, required=True)
    a = p.parse_args()
    status = {'started': time.strftime('%Y-%m-%d %H:%M:%S'), 'profile': a.profile, 'out': str(a.out), 'pid': os.getpid(), 'running': True,
              'episodes': {ep: {'state': 'queued', 'step': ''} for ep in a.episodes}}
    write_status(status)
    for ep in a.episodes:
        status['episodes'][ep].update(state='running', started=time.strftime('%H:%M:%S')); write_status(status)
        try:
            s = compare(ep, a.profile, a.out, status)
            status['episodes'][ep].update(state='done', step='', finished=time.strftime('%H:%M:%S'), between=s.get('between_variants') and {k: v for k, v in s['between_variants'].items() if k != 'per_frame_mean_mm'})
        except Exception as e:
            traceback.print_exc()
            status['episodes'][ep].update(state='failed', error=str(e)[:400])
        write_status(status)
    status['running'] = False; status['finished'] = time.strftime('%Y-%m-%d %H:%M:%S'); write_status(status)


if __name__ == '__main__':
    main()
