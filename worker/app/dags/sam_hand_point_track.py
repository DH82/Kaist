"""Optional SAM3 hand mode: first-frame points -> hand tracking (no text baseline, no hand ROI).

물체 포인트 추적(sam_object_point_track.py)의 손 버전입니다. 텍스트 기준선과 손 ROI 확대 단계를 대신합니다.
카메라 여러 대를 한 프로세스에서(모델 1회 로드) 처리합니다.

시드(첫 프레임 손 마스크) 선택 — 손은 둘일 수 있으므로 여러 개를 고를 수 있습니다:
  1) 텍스트 "hand"로 첫 프레임의 손을 모두 찾고, '포함' 점이 들어간 손을 각각 고릅니다('제외' 점이 들어간 손은 버림).
  2) 어느 손에도 들어가지 않은 '포함' 점이 있으면, 그 점들(+제외 점)만으로 한 번 더 분할해 손 하나로 더합니다.
추적: 고른 손들을 각각 추적하고(검출기를 다시 부르지 않음) 프레임마다 합쳐 mask/<cam>/1/<frame>.png 로 씁니다.
빈 프레임은 파일을 쓰지 않습니다(물체 추적과 같음).

Usage:
  sam-env/bin/python sam_hand_point_track.py --case <episode view> \
      --jobs '[{"cam":0,"points":[[0.52,0.40,1]],"out":"<new dir>"}]'
점은 [x, y, label] (0~1 정규화, label 1=포함 0=제외). 출력: <out>/1/<f>.png, <out>/seed/*.jpg, <out>/record.json
"""
import argparse
import json
import time
from pathlib import Path

import numpy as np
from PIL import Image

from sam_object_track import encode, progress
from sam_object_point_track import masks_of, parse_points

CLICK_OBJ = 1000   # 점 전용 번호 (텍스트 검출 번호와 겹치지 않게)
HAND_TEXT = 'hand'
MAX_CLICK_AREA = 0.15   # 점만으로 고른 손 마스크가 화면의 이 비율을 넘으면 손이 아니라고 봅니다(손 하나는 보통 1~5%)
COLORS = [(40, 150, 255), (255, 170, 40), (190, 90, 255), (40, 220, 200)]


def pixel(pt, w, h):
    return min(h - 1, int(pt[1] * h)), min(w - 1, int(pt[0] * w))


def overlay(rgb, masks, points, path):
    view = rgb.copy()
    for k, mask in enumerate(masks):
        view[mask] = (view[mask] * .5 + np.array(COLORS[k % len(COLORS)]) * .5).astype(np.uint8)
    h, w = view.shape[:2]
    r = max(3, w // 110)
    yy, xx = np.ogrid[:h, :w]
    for x, y, label in points:
        cx, cy = int(x * w), int(y * h)
        dist = (xx - cx) ** 2 + (yy - cy) ** 2
        view[dist <= r * r] = (59, 130, 246) if label else (249, 115, 22)
        view[(dist <= r * r) & (dist >= (r - 2) ** 2)] = (255, 255, 255)
    Image.fromarray(view).save(path, quality=90)


def choose_seeds(detected, points, w, h):
    """텍스트로 찾은 손 가운데 '포함' 점이 들어간 것들. 반환: (고른 손들, 어느 손에도 안 들어간 포함 점들)."""
    pos = [pt for pt in points if pt[2] == 1]
    neg = [pt for pt in points if pt[2] == 0]
    usable = [c for c in detected if c['mask'].any() and not any(c['mask'][pixel(n, w, h)] for n in neg)]
    seeds, loose = [], []
    for pt in pos:
        hit = [c for c in usable if c['mask'][pixel(pt, w, h)]]
        if not hit:
            loose.append(pt)
            continue
        best = max(hit, key=lambda c: c['prob'] or 0)
        if all(best['id'] != s['id'] for s in seeds):
            seeds.append({**best, 'source': 'text+point'})
    return seeds, loose


def seeds_from_points(p, image, w, h, points, thresh):
    sid = p.handle_request(request=dict(type='start_session', resource_path=str(image),
                                        offload_video_to_cpu=True))['session_id']
    tried = []

    def add(**kw):
        return p.handle_request(request=dict(type='add_prompt', session_id=sid, frame_index=0, **kw))['outputs']
    try:
        # SAM3.1 다중 물체 모델은 텍스트 검출을 한 번 거친 세션에서만 점 프롬프트를 받습니다.
        detected = masks_of(add(text=HAND_TEXT, output_prob_thresh=thresh), h, w)
        rows = [{'id': c['id'], 'area': int(c['mask'].sum()), 'prob': c['prob']} for c in detected]
        seeds, loose = choose_seeds(detected, points, w, h)
        tried.append({'attempt': 'text', 'candidates': len(detected), 'selected': len(seeds), 'loose_points': len(loose)})
        if loose:
            neg = [pt for pt in points if pt[2] == 0]
            got = [c for c in masks_of(add(points=[[x, y] for x, y, _ in loose + neg],
                                           point_labels=[1] * len(loose) + [0] * len(neg), obj_id=CLICK_OBJ), h, w)
                   if c['id'] == CLICK_OBJ and c['mask'].any()]
            tried.append({'attempt': 'points_only', 'candidates': len(got)})
            if got:
                taken = np.zeros((h, w), bool)
                for s in seeds:
                    taken |= s['mask']
                extra = got[0]['mask'] & ~taken   # 이미 고른 손과 겹치는 부분은 빼고 더합니다.
                if extra.sum() > MAX_CLICK_AREA * h * w:
                    # 텍스트로 손을 찾지 못한 자리의 점이 책상·배경처럼 큰 영역을 잡은 경우입니다.
                    tried.append({'attempt': 'points_only', 'rejected': 'too_large', 'area': int(extra.sum())})
                    raise SystemExit(f'손 포인트 중 손이 아닌 넓은 영역(화면의 {extra.sum() / (h * w):.0%})을 잡은 점이 있습니다. '
                                     '손 위에 다시 찍어 새로 실행하세요.')
                if extra.any():
                    seeds.append({'id': CLICK_OBJ, 'mask': extra, 'prob': got[0]['prob'], 'source': 'points_only'})
        return seeds, rows, tried
    finally:
        p.handle_request(request=dict(type='close_session', session_id=sid))


def track(p, video, seeds, n, w, h, started, on_frame):
    """고른 손들을 추적해 프레임마다 합친 마스크를 on_frame(f, mask)로 넘깁니다. 반환: 사용한 방식."""
    import torch
    model = p.model

    def run(group):
        """group의 손들을 한 번의 추적 패스로 처리. 프레임별 합친 마스크 목록을 돌려줍니다."""
        sid = p.handle_request(request=dict(type='start_session', resource_path=str(video),
                                            offload_video_to_cpu=True))['session_id']
        frames = []
        try:
            state = p._get_session(sid)['state']
            ids = list(range(len(group)))
            with torch.inference_mode():
                model._prepare_backbone_feats(state, 0, False)
                init = torch.from_numpy(np.stack([s['mask'].astype(np.float32) * 2 - 1 for s in group])).to(model.device)
                trackers = model._tracker_add_new_objects(
                    frame_idx=0, num_frames=n, new_obj_ids=ids, new_obj_masks=init, tracker_states_local=[],
                    orig_vid_height=h, orig_vid_width=w, feature_cache=state['feature_cache'])
                last = 0
                for f in range(n):
                    model._prepare_backbone_feats(state, f, False)
                    got, low, _ = model._propogate_tracker_one_frame_local_gpu(
                        trackers, frame_idx=f, reverse=False, run_mem_encoder=True)
                    if sorted(int(i) for i in got) != ids:
                        raise RuntimeError(f'Tracker returned unexpected objects {list(got)} at frame {f}')
                    union = np.zeros((h, w), bool)
                    for k in range(len(ids)):
                        m = model._convert_low_res_mask_to_video_res(low[k], state).detach().cpu().numpy().astype(bool).squeeze()
                        if m.shape != (h, w):
                            raise RuntimeError(f'Unexpected tracked mask shape {m.shape}')
                        union |= m
                    frames.append(union)
                    if time.monotonic() - last >= 5 or f == n - 1:
                        progress(video, n, 'hand_point_track', f + 1, started)
                        last = time.monotonic()
        finally:
            p.handle_request(request=dict(type='close_session', session_id=sid))
        return frames

    method = 'single' if len(seeds) == 1 else 'multi_object'
    try:
        frames = run(seeds)
    except Exception as e:
        if len(seeds) == 1:
            raise
        # 여러 손을 한 번에 추적하지 못하면 손마다 따로 추적해 합칩니다(물체 추적과 같은 검증된 경로).
        print(f'SAM: 손 {len(seeds)}개 동시 추적 실패({type(e).__name__}: {str(e)[:200]}) → 손마다 따로 추적', flush=True)
        method = 'per_object'
        frames = None
        for s in seeds:
            one = run([s])
            frames = one if frames is None else [a | b for a, b in zip(frames, one)]
    for f, mask in enumerate(frames):
        on_frame(f, mask)
    return method


def run_camera(p, case, cam, points, out, thresh):
    meta = json.loads((case / 'metadata.json').read_text())
    w, h = map(int, meta['WH'])
    n = int(meta['frame_num'])
    fps = meta.get('fps', 30)
    out.mkdir(parents=True)   # must be new; never merge into an older result
    (out / 'seed').mkdir()
    first = np.asarray(Image.open(case / f'color/{cam}/0.png').convert('RGB'))
    if first.shape[:2] != (h, w):
        raise ValueError('Frame size differs from metadata WH')
    # 시드는 첫 프레임 PNG로 만듭니다(1프레임 mp4에서는 텍스트 검출이 실패하는 경우가 확인됨). 추적은 무손실 mp4.
    video = out / 'track_input.mp4'
    encode(case, cam, n, fps, video)
    started = time.monotonic()
    print(f'SAM: 손 포인트 초기화 cam {cam} points={points}', flush=True)
    seeds, rows, tried = seeds_from_points(p, case / f'color/{cam}/0.png', w, h, points, thresh)
    overlay(first, [], points, out / 'seed' / 'points.jpg')
    if not seeds:
        raise SystemExit(f'cam {cam}: 첫 프레임 포인트 위치에서 손을 찾지 못했습니다 ({tried}). '
                         '손 위에 포인트를 다시 찍어 새로 실행하세요. 빈 추적은 시작하지 않습니다.')
    union = np.zeros((h, w), bool)
    for s in seeds:
        union |= s['mask']
    pos = [pt for pt in points if pt[2] == 1]
    hits = sum(bool(union[pixel(pt, w, h)]) for pt in pos)
    if hits == 0:
        raise SystemExit(f'cam {cam}: 선택된 손 마스크가 포함 포인트를 하나도 덮지 않습니다. 손 위에 다시 찍어 새로 실행하세요.')
    overlay(first, [s['mask'] for s in seeds], points, out / 'seed' / 'selected.jpg')
    print(f'SAM: 손 초기 마스크 {len(seeds)}개 선택 area={int(union.sum())} (포함점 {hits}/{len(pos)})', flush=True)
    masks_dir = out / '1'
    masks_dir.mkdir()
    empty = []

    def save(f, mask):
        if mask.any():
            Image.fromarray(mask.astype(np.uint8) * 255).save(masks_dir / f'{f}.png')
        else:
            empty.append(f)
    method = track(p, video, seeds, n, w, h, started, save)
    video.unlink()
    record = {'camera': cam, 'mode': 'hand_point_track', 'points_norm': points,
              'points_px': [[int(x * w), int(y * h), l] for x, y, l in points], 'text': HAND_TEXT,
              'seed_attempts': tried, 'candidates': rows,
              'seeds': [{'id': s['id'], 'source': s['source'], 'area': int(s['mask'].sum()), 'prob': s['prob']} for s in seeds],
              'seed_area': int(union.sum()), 'positive_points_covered': [hits, len(pos)], 'track_method': method,
              'frames': n, 'nonempty_frames': n - len(empty), 'empty_frames': empty,
              'wall_s': round(time.monotonic() - started, 1), 'threshold': thresh, 'human_approved': False,
              'note': 'Hands tracked from first-frame point seeds; no text baseline, no hand ROI enlargement, '
                      'no forearm subtraction. Review frame 0, fast motion, occlusion, hand re-entry and the end.'}
    (out / 'record.json').write_text(json.dumps(record, indent=2, ensure_ascii=False))
    print(f'HAND_TRACK_COMPLETE cam {cam} hands {len(seeds)} ({method}) nonempty {n - len(empty)}/{n} empty {len(empty)}', flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--case', type=Path, required=True)
    ap.add_argument('--jobs', required=True, help='JSON [{"cam":0,"points":[[x,y,label],...],"out":"<new dir>"}]')
    ap.add_argument('--threshold', type=float, default=0.5)
    ap.add_argument('--grounding_batch_size', type=int, default=4)
    a = ap.parse_args()
    jobs = [{'cam': int(j['cam']), 'points': parse_points(j['points']), 'out': Path(j['out']).resolve()}
            for j in json.loads(a.jobs)]
    from sam3.model_builder import build_sam3_predictor
    p = build_sam3_predictor(version='sam3.1', use_fa3=False)
    p.model.batched_grounding_batch_size = a.grounding_batch_size
    p.model.postprocess_batch_size = a.grounding_batch_size
    for job in jobs:
        run_camera(p, a.case.resolve(), job['cam'], job['points'], job['out'], a.threshold)


if __name__ == '__main__':
    main()
