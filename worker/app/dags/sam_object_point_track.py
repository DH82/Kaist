"""Optional SAM3 object mode: first-frame points(+text) seed -> single-object tracking.

bbox_track(sam_object_track.py)의 포인트 버전입니다. sam_object_track.py는 진행 중인 bbox 실행의
코드 체크섬에 고정되어 있으므로 수정하지 않고, 공통 함수만 가져다 씁니다.

시드(첫 프레임 초기 마스크) 선택:
  1) 텍스트가 있으면 SAM3 개념 검출로 같은 종류의 물체를 모두 찾고, 첫 '포함' 점이 들어간 물체를 고릅니다.
     점이 여러 개면 그 물체에 점을 더해 경계를 다듬습니다.
  2) 텍스트로 찾지 못했거나 점 위치에 그 물체가 없으면, 점만으로 분할합니다.
     (SAM3.1 다중 물체 모델은 텍스트 검출을 한 번 거친 세션에서만 점 프롬프트를 받으므로 먼저 텍스트를 한 번 실행)
이후 추적은 bbox_track과 같습니다: 고른 시드 마스크 하나만 추적하고 검출기를 다시 부르지 않습니다.

Usage (one camera):
  sam-env/bin/python sam_object_point_track.py --case <episode view> --cam 0 \
      --points '[[0.52,0.40,1],[0.30,0.20,0]]' --text "light green cloth" --out <new dir>
점은 [x, y, label] (0~1 정규화, label 1=포함 0=제외). 출력은 bbox_track과 같은 구조입니다.
"""
import argparse
import json
import time
from pathlib import Path

import numpy as np
from PIL import Image

from sam_object_track import encode, progress

CLICK_OBJ = 1000  # 점 전용 물체 번호 (텍스트 검출 번호와 겹치지 않게)


def overlay(rgb, mask, points, path):
    view = rgb.copy()
    if mask is not None:
        view[mask] = (view[mask] * .55 + np.array([40, 230, 90]) * .45).astype(np.uint8)
    h, w = view.shape[:2]
    r = max(3, w // 110)
    yy, xx = np.ogrid[:h, :w]
    for x, y, label in points:
        cx, cy = int(x * w), int(y * h)
        disk = (xx - cx) ** 2 + (yy - cy) ** 2 <= r * r
        ring = disk & ((xx - cx) ** 2 + (yy - cy) ** 2 >= (r - 2) ** 2)
        view[disk] = (34, 197, 94) if label else (239, 68, 68)
        view[ring] = (255, 255, 255)
    Image.fromarray(view).save(path, quality=90)


def masks_of(out, h, w):
    found = []
    probs = out.get('out_probs')
    for k, oid in enumerate(out['out_obj_ids']):
        m = np.asarray(out['out_binary_masks'][k]).astype(bool).squeeze()
        if m.shape != (h, w):
            raise ValueError(f'Unexpected seed mask shape {m.shape}')
        found.append({'id': int(oid), 'mask': m,
                      'prob': float(np.asarray(probs[k]).reshape(-1)[0]) if probs is not None and len(probs) > k else None})
    return found


def seed_from_points(p, video, w, h, points, text, thresh):
    xy = [[x, y] for x, y, _ in points]
    labels = [int(l) for _, _, l in points]
    first = next(i for i, l in enumerate(labels) if l == 1)
    px, py = min(w - 1, int(points[first][0] * w)), min(h - 1, int(points[first][1] * h))
    sid = p.handle_request(request=dict(type='start_session', resource_path=str(video),
                                        offload_video_to_cpu=True))['session_id']
    tried = []

    def add(**kw):
        return p.handle_request(request=dict(type='add_prompt', session_id=sid, frame_index=0, **kw))['outputs']
    try:
        detected = masks_of(add(text=text or 'object', output_prob_thresh=thresh), h, w)
        candidates = [{'id': c['id'], 'area': int(c['mask'].sum()), 'prob': c['prob'],
                       'contains_point': bool(c['mask'][py, px])} for c in detected]
        if text:
            tried.append({'attempt': 'text', 'candidates': len(detected)})
            hit = [c for c in detected if c['mask'].any() and c['mask'][py, px]]
            if hit:
                chosen = max(hit, key=lambda c: c['prob'] or 0)
                if len(points) == 1:
                    return 'text+point', chosen, candidates, tried
                try:
                    refined = [c for c in masks_of(add(points=xy, point_labels=labels, obj_id=chosen['id']), h, w)
                               if c['id'] == chosen['id'] and c['mask'].any()]
                    if refined:
                        tried.append({'attempt': 'text+points_refine', 'ok': True})
                        return 'text+points', {**refined[0], 'prob': chosen['prob']}, candidates, tried
                except Exception as e:  # 다듬기 실패 시 텍스트로 고른 마스크를 씁니다.
                    tried.append({'attempt': 'text+points_refine', 'ok': False, 'error': str(e)[:200]})
                return 'text+point', chosen, candidates, tried
        got = [c for c in masks_of(add(points=xy, point_labels=labels, obj_id=CLICK_OBJ), h, w)
               if c['id'] == CLICK_OBJ and c['mask'].any()]
        tried.append({'attempt': 'points_only', 'candidates': len(got)})
        return ('points_only', got[0], candidates, tried) if got else (None, None, candidates, tried)
    finally:
        p.handle_request(request=dict(type='close_session', session_id=sid))


def parse_points(value):
    pts = json.loads(value) if isinstance(value, str) else value
    if not isinstance(pts, list) or not 1 <= len(pts) <= 20:
        raise ValueError('points must be a list of 1..20 [x, y, label]')
    out = []
    for pt in pts:
        if not isinstance(pt, list) or len(pt) != 3:
            raise ValueError('point must be [x, y, label]')
        x, y, label = float(pt[0]), float(pt[1]), int(pt[2])
        if not (0 <= x <= 1 and 0 <= y <= 1) or label not in (0, 1):
            raise ValueError('point outside the frame or bad label')
        out.append([x, y, label])
    if not any(l == 1 for _, _, l in out):
        raise ValueError('at least one positive point is required')
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--case', type=Path, required=True)
    ap.add_argument('--cam', type=int, required=True)
    ap.add_argument('--points', required=True, help='JSON [[x,y,label],...] normalized; label 1=include 0=exclude')
    ap.add_argument('--text', default='')
    ap.add_argument('--out', type=Path, required=True)
    ap.add_argument('--threshold', type=float, default=0.5)
    ap.add_argument('--grounding_batch_size', type=int, default=4)
    a = ap.parse_args()

    case, out = a.case.resolve(), a.out.resolve()
    points = parse_points(a.points)
    meta = json.loads((case / 'metadata.json').read_text())
    w, h = map(int, meta['WH'])
    n = int(meta['frame_num'])
    fps = meta.get('fps', 30)
    out.mkdir(parents=True)  # must be new; never merge into an older result
    (out / 'seed').mkdir()

    import torch
    from sam3.model_builder import build_sam3_predictor

    first = np.asarray(Image.open(case / f'color/{a.cam}/0.png').convert('RGB'))
    if first.shape[:2] != (h, w):
        raise ValueError('Frame size differs from metadata WH')
    # 시드는 첫 프레임 PNG로 바로 만듭니다. 1프레임 mp4에서는 SAM3 텍스트 검출이 물체를 찾지 못하는 경우가
    # 확인됐습니다(같은 프레임 PNG는 정상 검출). 추적은 bbox_track과 같은 무손실 mp4를 씁니다.
    seed_image, track_video = case / f'color/{a.cam}/0.png', out / 'track_input.mp4'
    encode(case, a.cam, n, fps, track_video)

    started = time.monotonic()
    p = build_sam3_predictor(version='sam3.1', use_fa3=False)
    p.model.batched_grounding_batch_size = a.grounding_batch_size
    p.model.postprocess_batch_size = a.grounding_batch_size
    print(f'SAM: 물체 포인트 초기화 cam {a.cam} points={points} text={a.text!r}', flush=True)

    attempt, chosen, rows, tried = seed_from_points(p, seed_image, w, h, points, a.text.strip(), a.threshold)
    overlay(first, None, points, out / 'seed' / 'points.jpg')
    if chosen is None:
        raise SystemExit(f'cam {a.cam}: 첫 프레임 포인트 위치에서 물체를 찾지 못했습니다 ({tried}). '
                         '포인트를 물체 위에 다시 찍어 새로 실행하세요. 빈 추적은 시작하지 않습니다.')
    seed = chosen['mask']
    pos = [(x, y) for x, y, l in points if l == 1]
    hits = sum(bool(seed[min(h - 1, int(y * h)), min(w - 1, int(x * w))]) for x, y in pos)
    if hits == 0:
        raise SystemExit(f'cam {a.cam}: 선택된 초기 마스크가 포함(초록) 포인트를 하나도 덮지 않습니다 ({attempt}). '
                         '포인트를 물체 위에 다시 찍어 새로 실행하세요.')
    overlay(first, seed, points, out / 'seed' / 'selected.jpg')
    print(f'SAM: 초기 마스크 선택 id={chosen["id"]} area={int(seed.sum())} ({attempt}, 포함점 {hits}/{len(pos)})', flush=True)

    # Track only the chosen seed mask (bbox_track과 동일한 추적 경로).
    sid = p.handle_request(request=dict(type='start_session', resource_path=str(track_video),
                                        offload_video_to_cpu=True))['session_id']
    masks_dir = out / '0'
    masks_dir.mkdir()
    empty, last = [], 0
    try:
        model = p.model
        state = p._get_session(sid)['state']
        with torch.inference_mode():
            model._prepare_backbone_feats(state, 0, False)
            init = torch.from_numpy(seed.astype(np.float32) * 2 - 1).to(model.device)[None]
            trackers = model._tracker_add_new_objects(
                frame_idx=0, num_frames=n, new_obj_ids=[0], new_obj_masks=init, tracker_states_local=[],
                orig_vid_height=h, orig_vid_width=w, feature_cache=state['feature_cache'])
            for f in range(n):
                model._prepare_backbone_feats(state, f, False)
                ids, low, _ = model._propogate_tracker_one_frame_local_gpu(
                    trackers, frame_idx=f, reverse=False, run_mem_encoder=True)
                if list(ids) != [0]:
                    raise RuntimeError(f'Tracker returned unexpected objects {ids} at frame {f}')
                m = model._convert_low_res_mask_to_video_res(low[0], state).detach().cpu().numpy().astype(bool).squeeze()
                if m.shape != (h, w):
                    raise RuntimeError(f'Unexpected tracked mask shape {m.shape}')
                if m.any():
                    Image.fromarray(m.astype(np.uint8) * 255).save(masks_dir / f'{f}.png')
                else:
                    empty.append(f)
                if time.monotonic() - last >= 5 or f == n - 1:
                    progress(track_video, n, 'point_track', f + 1, started)
                    last = time.monotonic()
    finally:
        p.handle_request(request=dict(type='close_session', session_id=sid))
    track_video.unlink()
    record = {'camera': a.cam, 'mode': 'point_track', 'points_norm': points,
              'points_px': [[int(x * w), int(y * h), l] for x, y, l in points],
              'text': a.text, 'seed_attempts': tried, 'seed_attempt_used': attempt, 'candidates': rows,
              'selected_candidate': chosen['id'], 'seed_area': int(seed.sum()), 'positive_points_covered': [hits, len(pos)],
              'seed_auto_selected': True, 'frames': n, 'nonempty_frames': n - len(empty), 'empty_frames': empty,
              'wall_s': round(time.monotonic() - started, 1), 'threshold': a.threshold, 'human_approved': False,
              'note': 'Single tracked object from the first-frame point seed; shadows or wrong parts inside the '
                      'same object are still possible. Review frame 0, fast motion, occlusion and the end.'}
    (out / 'record.json').write_text(json.dumps(record, indent=2, ensure_ascii=False))
    print(f'OBJECT_TRACK_COMPLETE cam {a.cam} nonempty {n - len(empty)}/{n} empty {len(empty)}', flush=True)


if __name__ == '__main__':
    main()
