#!/usr/bin/env python3
"""MANO 손가락 추종 설정 탐색 (시험용, 운영 실행과 분리).

문제: 운영 설정(pose_temp_weight 5e5)은 손가락 자세를 이전 프레임에 강하게 묶어 손가락이 거의 움직이지 않음.
탐색: 손가락 시간축 가중치(pose_temp_weight), HaMBA 2D 가중치(hamba_weight), 자세 사전(w_pose_prior),
      프레임 시작 자세(prev=이전 프레임 / hamba=그 프레임 HaMBA 카메라 평균 자세; 시험용 코드 사본에만 추가한 옵션).
β는 프로필(User2)로 고정해 형상 차이를 없애고 손가락 설정만 비교합니다.
입력·HaMBA는 mano_compare.py가 만든 작업 폴더(~/kaist_work/mano_compare/<ep>)를 그대로 씁니다.

단계: 1차(넓게) → 1차 최적 주변 2차 → 최종 후보를 모든 에피소드로 확인.
선택 규칙: 떨림(손가락 끝 2차 차분 평균)이 기준 설정의 2배 이하이고 마스크 안 비율이 기준 이상인 설정 중
          MediaPipe 손가락 끝 오차(평균)가 가장 작은 것, 같으면 HaMBA 상관이 큰 것.
결과: ~/kaist_work/mano_compare/finger_sweep.json, NAS <out>/finger_sweep/ (요약·영상)
"""
import argparse, json, os, shutil, subprocess, sys, time, traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mano_compare as mc  # noqa: E402  (18_fit_hands·GPU 잠금·런타임 폴더 도구)

HERE = Path(__file__).resolve().parent
EVAL = HERE / 'mano_finger_eval.py'
STATUS = mc.WORK / 'finger_sweep.json'
BASE = {'pose_temp_weight': 500000.0, 'hamba_weight': 0.01, 'w_pose_prior': 25.0, 'pose_init': 'prev'}
STAGE1 = [dict(BASE, pose_temp_weight=v) for v in (5e4, 5e3, 5e2, 50.0)] + \
         [dict(BASE, pose_temp_weight=5e3, hamba_weight=0.1), dict(BASE, pose_temp_weight=5e3, pose_init='hamba')]


def name(v):
    return f"pt{v['pose_temp_weight']:g}_hw{v['hamba_weight']:g}_pr{v['w_pose_prior']:g}_{v['pose_init']}"


def patched_code():
    """운영 data_process_hands.py + --pose_init 옵션(기본 prev = 운영과 같음). 시험 폴더에만 둡니다."""
    src = Path(mc.CFG['pipeline_root']) / 'kaist-full/PhysTwin/Mono/data_process/data_process_hands.py'
    s = src.read_text()
    a1 = "parser.add_argument('--w_pose_prior'"
    s = s.replace(a1, "parser.add_argument('--pose_init', choices=['prev', 'hamba'], default='prev', help='(시험) 프레임 시작 손가락 자세')\n" + a1, 1)
    a2 = "            'hand_pose': prev_params['hand_pose'].detach().clone().requires_grad_(True).to(device),"
    assert s.count(a2) == 1, 'patch anchor'
    s = s.replace(a2, "            'hand_pose': _pose_init(prev_params, frame_data).requires_grad_(True).to(device),")
    a3 = "def register(frame_data, c2ws, is_right, prev_params, ref_cidx, fidx, mp_out, obj_pts=None):"
    s = s.replace(a3, '''def _pose_init(prev_params, frame_data):
    hp = prev_params['hand_pose'].detach().clone()
    if args.pose_init != 'hamba': return hp
    Rs = [fd['pred_mano_params']['hand_pose'].detach().reshape(15, 3, 3).float().cpu() for fd in frame_data if fd is not None]
    if not Rs: return hp
    M = torch.stack(Rs).mean(0); Uu, S, Vh = torch.linalg.svd(M)
    d = torch.sign(torch.det(Uu @ Vh)); Uu[:, :, -1] *= d[:, None]
    return (Uu @ Vh).reshape(hp.shape).to(hp.device).to(hp.dtype)


''' + a3, 1)
    out = mc.WORK / 'patched' / 'data_process_hands.py'; out.parent.mkdir(parents=True, exist_ok=True); out.write_text(s)
    return out


def run_variant(ep, v, status, key):
    base = mc.WORK / ep; out = base / 'sweep' / name(v); logf = base / 'sweep.log'
    if (out / 'finger_metrics.json').exists(): return json.loads((out / 'finger_metrics.json').read_text())
    case = mc.PROJECT / '06_reconstruction' / ep; prepared = mc.fh.source(case)
    root, pt, env = mc.fh.pipeline.runtime(); mono = pt / 'Mono'; python = str(root / 'hamba-env/bin/python')
    env['PYTHONPATH'] = str(mono / 'data_process') + os.pathsep + str(mono / 'data_process/Hamba') + os.pathsep + env.get('PYTHONPATH', '')
    env.pop('DISPLAY', None); env.pop('XAUTHORITY', None); env['PYOPENGL_PLATFORM'] = 'egl'; env['HF_HUB_OFFLINE'] = '1'
    side, side_source = mc.fh.resolve_side(root, ep, 'right'); mask, infos = mc.fh.hand_mask_inputs(case)
    T = int(json.loads((prepared / 'metadata.json').read_text())['frame_num'])
    profile_path = mc.PROFILES / f"{status['profile']}.json"; prof, psha = mc.fh.hand_profile.load(profile_path)
    out.mkdir(parents=True, exist_ok=True)
    if not (out / 'hamba').exists(): (out / 'hamba').symlink_to(base / 'hamba')
    work, runmono = mc.build_view(out, prepared, mono, ep, mask, infos)
    code = runmono / 'data_process/data_process_hands.py'
    if code.is_symlink(): code.unlink()
    shutil.copy2(status['patched'], code)
    settings = mc.fh.fit_settings(side, side_source, {'id': prof['profile_id'], 'sha256': psha})
    settings.update(pose_temp_weight=v['pose_temp_weight'], hamba_weight=v['hamba_weight'], w_pose_prior=v['w_pose_prior'], pose_init=v['pose_init'], experiment='finger-sweep')
    (out / 'settings.json').write_text(json.dumps(settings, indent=2))
    obs = base / 'User2' / 'observations.pkl'
    if obs.exists() and not (out / 'observations.pkl').exists(): shutil.copy2(obs, out / 'observations.pkl')  # MediaPipe 검출 재사용(같은 입력)
    cmd = [python, '-u', str(code), '--hamba_base_path', str(out / 'hamba'), '--phystwin_base_path', str(work.parent), '--case_name', ep,
           '--temp_weight', '500000', '--hamba_weight', str(v['hamba_weight']), '--log', 'hand_v1', '--seed', '1027', '--num_iters', '1500',
           '--num_hands', '1', '--num_cams', '4', '--resume', '--observations-cache', str(out / 'observations.pkl'), '--pose_init', v['pose_init']]
    for k in ('w_mask', 'w_mask_cover', 'cover_max_px', 'wrist_weight', 'w_silhouette', 'silhouette_size', 'silhouette_every', 'camera_init_iters',
              'fit_shape_lr', 'fit_shape_max_delta', 'fit_shape_temporal_weight', 'robust_joint_delta'):
        cmd += ['--' + k, str(settings[k])]
    cmd += ['--pose_temp_weight', str(v['pose_temp_weight']), '--w_pose_prior', str(v['w_pose_prior']), '--staged_fit',
            '--shape_profile', str(profile_path), '--shape_profile_sha256', psha]
    status['current'] = {'episode': ep, 'variant': name(v), 'stage': key, 'started': time.strftime('%H:%M:%S')}; save(status)
    t0 = time.time()
    mc.gpu(lambda: mc.run_logged(cmd, runmono, env, out / 'logs/19_mano.log'), f'손가락 시험 {ep} {name(v)}', logf)
    mc.run_logged([python, str(mc.fh.HERE / 'hand_stage_worker.py'), 'fit', '--out', out, '--case', work, '--frames', T, '--side', side], runmono, env, out / 'logs/validate.log')
    mc.run_logged([python, str(mc.fh.HERE / 'hand_stage_worker.py'), 'preview', '--out', out, '--case', work, '--frames', T, '--side', side], runmono, env, out / 'logs/validate.log')
    r = subprocess.run([python, str(EVAL), str(out), '--case', str(work), '--hamba', str(base / 'hamba' / ep), '--obs', str(out / 'observations.pkl')],
                       env=env, capture_output=True, text=True, timeout=900)
    if r.returncode: raise RuntimeError('평가 실패: ' + r.stderr[-400:])
    m = json.loads((out / 'finger_metrics.json').read_text()); m['seconds'] = round(time.time() - t0)
    (out / 'finger_metrics.json').write_text(json.dumps(m, indent=1))
    mc.log(f'   {ep} {name(v)}: 끝 오차 {m["mp2d"]["tips_mean"]}px · 마스크 {m["mask_inside_finger"]} · HaMBA 상관 {m["hamba"]["corr_mean"]} · 움직임 {m["motion_deg"]}° · 떨림 {m["jitter_tip_mm"]}mm ({m["seconds"]}초)', logf)
    return m


def baseline(ep, status):
    """기준(운영 설정 + User2) = 비교 실행의 User2 결과를 같은 지표로 평가."""
    base = mc.WORK / ep; out = base / 'User2'
    if not (out / 'finger_metrics.json').exists():
        root, pt, env = mc.fh.pipeline.runtime(); python = str(root / 'hamba-env/bin/python')
        r = subprocess.run([python, str(EVAL), str(out), '--case', str(out / '_runtime/inputs' / ep), '--hamba', str(base / 'hamba' / ep), '--obs', str(out / 'observations.pkl')],
                           env=env, capture_output=True, text=True, timeout=900)
        if r.returncode: raise RuntimeError('기준 평가 실패: ' + r.stderr[-400:])
    return json.loads((out / 'finger_metrics.json').read_text())


def save(status):
    tmp = STATUS.with_suffix('.tmp'); tmp.write_text(json.dumps(status, ensure_ascii=False, indent=1)); tmp.replace(STATUS)


def score_pick(status, eps, names):
    """선택 규칙(에피소드 평균). 반환: (최적 이름, 표)"""
    rows = []
    for n in names:
        ms = [status['results'][ep].get(n) for ep in eps]
        if not all(ms): continue
        bs = [status['baseline'][ep] for ep in eps]
        avg = lambda k: sum(k(m) for m in ms) / len(ms)
        row = {'name': n, 'tips_mean': avg(lambda m: m['mp2d']['tips_mean']), 'tips_p90': avg(lambda m: m['mp2d']['tips_p90']), 'mask': avg(lambda m: m['mask_inside_finger']),
               'corr': avg(lambda m: m['hamba']['corr_mean']), 'motion': avg(lambda m: m['motion_deg']), 'jitter': avg(lambda m: m['jitter_tip_mm'])}
        row['ok'] = all(m['jitter_tip_mm'] <= 2 * b['jitter_tip_mm'] and m['mask_inside_finger'] >= b['mask_inside_finger'] - 0.005 for m, b in zip(ms, bs))
        rows.append(row)
    good = [r for r in rows if r['ok']] or rows
    best = min(good, key=lambda r: (round(r['tips_mean'], 1), -r['corr']))
    return best['name'], rows


def neighbors(v):
    out = []
    for f in (1 / 3, 3):
        out.append(dict(v, pose_temp_weight=round(v['pose_temp_weight'] * f, 3)))
    out.append(dict(v, hamba_weight=0.1 if v['hamba_weight'] < 0.05 else 0.03))
    out.append(dict(v, pose_init='hamba' if v['pose_init'] == 'prev' else 'prev'))
    out.append(dict(v, w_pose_prior=0.0 if v['w_pose_prior'] > 0 else 25.0))
    return out


def main():
    p = argparse.ArgumentParser(); p.add_argument('--profile', default='User2'); p.add_argument('--out', type=Path, default=Path('/mnt/kaist-data/TEST/Set2'))
    p.add_argument('--episodes', nargs='+', required=True); p.add_argument('--search', type=int, default=2, help='탐색에 쓸 에피소드 수(손가락 움직임 큰 순)')
    a = p.parse_args()
    status = json.loads(STATUS.read_text()) if STATUS.exists() else {}
    status.update(profile=a.profile, running=True, started=status.get('started') or time.strftime('%Y-%m-%d %H:%M:%S'), episodes=a.episodes,
                  baseline=status.get('baseline', {}), results=status.get('results', {}), variants=status.get('variants', {}), log=status.get('log', []))
    status['patched'] = str(patched_code()); save(status)
    def note(msg): status['log'].append(time.strftime('%H:%M ') + msg); save(status); mc.log(msg)
    try:
        for ep in a.episodes:
            status['baseline'][ep] = baseline(ep, status); status['results'].setdefault(ep, {})
        save(status)
        order = sorted(a.episodes, key=lambda e: -status['baseline'][e]['hamba_motion_deg'])
        search = order[:a.search]; status['search_episodes'] = search
        note('탐색 에피소드(HaMBA 손가락 움직임 큰 순): ' + ', '.join(f"{e} {status['baseline'][e]['hamba_motion_deg']}°" for e in search))
        def run_set(vs, eps, key):
            for v in vs:
                status['variants'][name(v)] = v
                for ep in eps:
                    try: status['results'][ep][name(v)] = run_variant(ep, v, status, key)
                    except Exception as e:
                        traceback.print_exc(); status['results'][ep][name(v)] = None; note(f'실패 {ep} {name(v)}: {e}')
                    save(status)
        run_set(STAGE1, search, '1차')
        best1, rows = score_pick(status, search, [name(v) for v in STAGE1]); status['stage1'] = {'best': best1, 'table': rows}
        note(f'1차 최적: {best1}')
        cand = [v for v in neighbors(status['variants'][best1]) if name(v) not in status['variants']]
        run_set(cand, search, '2차')
        names = list(status['variants']); best2, rows = score_pick(status, search, names); status['stage2'] = {'best': best2, 'table': rows}
        note(f'2차 최적: {best2}')
        final = [status['variants'][best2]]
        run_set(final, [e for e in a.episodes], '최종 확인')
        bestf, rowsf = score_pick(status, a.episodes, [best2]); status['final'] = {'best': best2, 'table': rowsf, 'settings': status['variants'][best2]}
        note(f'최종 확인(에피소드 {len(a.episodes)}개) 완료: {best2}')
        # NAS 사본: 요약 + 기준/최적 영상과 지표
        dest = a.out / 'finger_sweep'; dest.mkdir(parents=True, exist_ok=True)
        for ep in a.episodes:
            for n, src in (('baseline', mc.WORK / ep / 'User2'), (best2, mc.WORK / ep / 'sweep' / best2)):
                d = dest / ep / n; d.mkdir(parents=True, exist_ok=True)
                for f in ('review/mano_review.mp4', 'finger_metrics.json', 'settings.json', 'hand_v1/hand_reg.pkl', 'hand_v1/hand_reg_params.pkl'):
                    if (src / f).exists(): shutil.copy2(src / f, d / Path(f).name)
            vids = [str(dest / ep / 'baseline/mano_review.mp4'), str(dest / ep / best2 / 'mano_review.mp4')]
            lab = lambda i, t: f"[{i}:v]scale=960:-2,drawbox=x=0:y=ih-34:w=iw:h=34:color=black@0.7:t=fill,drawtext=text='{t}':x=10:y=h-26:fontsize=20:fontcolor=white[v{i}]"
            subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-y', '-i', vids[0], '-i', vids[1], '-filter_complex',
                            f"{lab(0, 'current (finger pose temp 5e5)')};{lab(1, 'tuned ' + best2)};[v0][v1]hstack=inputs=2", '-c:v', 'libx264', '-crf', '22', '-pix_fmt', 'yuv420p',
                            str(dest / ep / 'compare.mp4')], check=False)
        (dest / 'finger_sweep.json').write_text(json.dumps(status, ensure_ascii=False, indent=1))
    except Exception as e:
        traceback.print_exc(); note(f'중단: {e}')
    finally:
        status['running'] = False; status['finished'] = time.strftime('%Y-%m-%d %H:%M:%S'); status.pop('current', None); save(status)


if __name__ == '__main__':
    main()
