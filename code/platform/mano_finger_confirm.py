#!/usr/bin/env python3
"""손가락 추종 v7 후보 확인 (기본 β 보정 기준, 시험용).

1단계: 시험 에피소드 1개에서 '손가락 500 + MediaPipe 잘라서 재검출'에 관절 평활 반경(0/2/4)을 비교해 고름
       규칙: 끝 오차가 평활 없음 +0.5px 이내이고 마스크 IoU가 -0.005 이내인 것 중 떨림이 가장 작은 것
2단계: 고른 설정을 나머지 에피소드에 실행
평가: 에피소드마다 같은 잣대(그 에피소드의 재검출 MediaPipe 관절, SAM3 마스크 IoU)로 현재 운영(default 결과)과 비교
결과: ~/kaist_work/mano_compare/finger_confirm.json
"""
import json, subprocess, sys, time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import mano_finger_sweep as sw  # noqa: E402
mc = sw.mc
OUT = mc.WORK / 'finger_confirm.json'


def evaluate(ep, d, ref):
    root, pt, env = mc.fh.pipeline.runtime(); python = str(root / 'hamba-env/bin/python'); base = mc.WORK / ep
    case = base / 'default/_runtime/inputs' / ep
    r = subprocess.run([python, str(HERE / 'mano_finger_eval.py'), str(d), '--case', str(case), '--hamba', str(base / 'hamba' / ep), '--obs', str(ref)], env=env, capture_output=True, text=True, timeout=900)
    if r.returncode: raise RuntimeError(r.stderr[-300:])
    m = json.loads((d / 'finger_metrics.json').read_text())
    r = subprocess.run([python, str(HERE / 'mano_mask_iou.py'), str(d), '--case', str(case)], env=env, capture_output=True, text=True, timeout=900)
    if r.returncode: raise RuntimeError(r.stderr[-300:])
    m['mask_iou'] = json.loads((d / 'mask_iou.json').read_text())
    (d / 'finger_metrics.json').write_text(json.dumps(m, indent=1))
    return m


def row(m):
    return {'tips_mean': m['mp2d']['tips_mean'], 'tips_p90': m['mp2d']['tips_p90'], 'iou': m['mask_iou']['iou'], 'precision': m['mask_iou']['precision'],
            'recall': m['mask_iou']['recall'], 'motion': m['motion_deg'], 'hamba_motion': m['hamba_motion_deg'], 'corr': m['hamba']['corr_mean'], 'jitter': m['jitter_tip_mm']}


def main():
    eps = sys.argv[1:] or ['ep_20261126_005323', 'ep_20261126_003904', 'ep_20261126_004348', 'ep_20261126_005247', 'ep_20261126_005410']
    test, rest = eps[0], eps[1:]
    st = {'started': time.strftime('%Y-%m-%d %H:%M:%S'), 'test_episode': test, 'stage1': {}, 'results': {}, 'running': True}
    save = lambda: OUT.write_text(json.dumps(st, ensure_ascii=False, indent=1))
    status = json.loads(sw.STATUS.read_text()) if sw.STATUS.exists() else {'results': {}, 'variants': {}, 'baseline': {}, 'log': []}
    status.update(profile='User2', patched=str(sw.patched_code()))  # 손가락 시험 기록(finger_sweep.json)을 덮어쓰지 않고 이어 씀
    def run(ep, v):
        st['current'] = {'episode': ep, 'variant': sw.name(v), 'started': time.strftime('%H:%M:%S')}; save()
        sw.run_variant(ep, v, status, '확인'); return mc.WORK / ep / 'sweep' / sw.name(v)
    try:
        cands = [dict(sw.BASE, pose_temp_weight=500.0, mp_crop=True, beta='default', **({'mp_smooth': r} if r else {})) for r in (0, 2, 4)]
        dirs = [run(test, v) for v in cands]
        ref = dirs[0] / 'mp_crop.pkl'
        st['stage1'] = {'reference': str(ref), 'baseline': row(evaluate(test, mc.WORK / test / 'default', ref)),
                        'candidates': {sw.name(v): row(evaluate(test, d, ref)) for v, d in zip(cands, dirs)}}
        c0 = st['stage1']['candidates'][sw.name(cands[0])]
        ok = [n for n, r in st['stage1']['candidates'].items() if r['tips_mean'] <= c0['tips_mean'] + 0.5 and r['iou'] >= c0['iou'] - 0.005]
        best = min(ok, key=lambda n: st['stage1']['candidates'][n]['jitter']); st['stage1']['best'] = best; save()
        bv = next(v for v in cands if sw.name(v) == best)
        st['results'][test] = {'baseline': st['stage1']['baseline'], 'v7': st['stage1']['candidates'][best]}
        for ep in rest:
            d = run(ep, bv)
            st['results'][ep] = {'baseline': row(evaluate(ep, mc.WORK / ep / 'default', d / 'mp_crop.pkl')), 'v7': row(evaluate(ep, d, d / 'mp_crop.pkl'))}
            save()
    except Exception as e:
        import traceback; traceback.print_exc(); st['error'] = str(e)
    finally:
        st['running'] = False; st.pop('current', None); st['finished'] = time.strftime('%Y-%m-%d %H:%M:%S'); save()


if __name__ == '__main__':
    main()
