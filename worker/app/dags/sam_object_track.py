"""Optional SAM3 object mode: first-frame bbox(+text) seed -> single-object tracking.

Runs in the pipeline's sam-env (sam3.1). Candidate only: never approves masks and
never touches the baseline. Ported from PL_SAM3_handoff_20261113_v2/code/run_sam3.py
(seed + track modes). Difference: the seed is chosen automatically from the bbox
candidates (largest area inside the operator's bbox); every candidate is saved as an
overlay and the choice is recorded, and the whole sequence still goes through the
existing mask review (approve/edit) before tracking.

Usage (one camera):
  sam-env/bin/python sam_object_track.py --case <episode view> --cam 0 \
      --bbox 0.40 0.25 0.18 0.17 --text "light green cloth" --out <new dir>
Writes <out>/0/<frame>.png (non-empty frames only, 0/255, PhysTwin layout),
<out>/record.json and <out>/seed/*.jpg.
"""
import argparse
import json
import subprocess
import time
from pathlib import Path

import numpy as np
from PIL import Image


def encode(case, cam, frames, fps, path):
    # Lossless working copy from the exact per-frame PNGs; the prepared input is not modified.
    subprocess.run(['ffmpeg', '-v', 'error', '-n', '-framerate', str(fps), '-start_number', '0',
                    '-i', str(case / f'color/{cam}/%d.png'), '-frames:v', str(frames),
                    '-c:v', 'libx264rgb', '-crf', '0', '-preset', 'fast', str(path)], check=True)


def overlay(rgb, mask, box, path):
    view = rgb.copy()
    view[mask] = (view[mask] * .55 + np.array([40, 230, 90]) * .45).astype(np.uint8)
    x0, y0, x1, y1 = box
    view[y0:y1, [x0, x1 - 1]] = (255, 210, 0)
    view[[y0, y1 - 1], x0:x1] = (255, 210, 0)
    Image.fromarray(view).save(path, quality=90)


def progress(video, frames, prompt, done, start):
    elapsed = time.monotonic() - start
    eta = elapsed / done * max(0, frames - done) if done else None
    print('SAM_PROGRESS ' + json.dumps({'video': str(video), 'frames': frames, 'prompt': prompt, 'done': done,
                                        'elapsed_s': round(elapsed, 1),
                                        'eta_s': round(eta, 1) if eta is not None else None},
                                       ensure_ascii=False), flush=True)


def seed_candidates(p, video, w, h, box_norm, text, thresh):
    """bbox+text first; bbox alone only when the text finds nothing inside the box."""
    attempts = [('bbox+text', text)] if text else []
    attempts.append(('bbox_only', None))
    tried = []
    for name, prompt in attempts:
        sid = p.handle_request(request=dict(type='start_session', resource_path=str(video),
                                            offload_video_to_cpu=True))['session_id']
        try:
            kw = dict(bounding_boxes=[list(box_norm)], bounding_box_labels=[1])
            if prompt:
                kw['text'] = prompt
            out = p.handle_request(request=dict(type='add_prompt', session_id=sid, frame_index=0,
                                                output_prob_thresh=thresh, **kw))['outputs']
        finally:
            p.handle_request(request=dict(type='close_session', session_id=sid))
        found = []
        probs = out.get('out_probs')
        for k, oid in enumerate(out['out_obj_ids']):
            m = np.asarray(out['out_binary_masks'][k]).astype(bool).squeeze()
            if m.shape != (h, w):
                raise ValueError(f'Unexpected seed mask shape {m.shape}')
            if m.any():
                found.append({'id': int(oid), 'mask': m,
                              'prob': float(np.asarray(probs[k]).reshape(-1)[0]) if probs is not None and len(probs) > k else None})
        tried.append({'attempt': name, 'candidates': len(found)})
        if found:
            return name, found, tried
    return None, [], tried


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--case', type=Path, required=True)
    ap.add_argument('--cam', type=int, required=True)
    ap.add_argument('--bbox', type=float, nargs=4, required=True, metavar=('X', 'Y', 'W', 'H'),
                    help='first-frame bbox, normalized to [0,1] (x, y, width, height)')
    ap.add_argument('--text', default='')
    ap.add_argument('--out', type=Path, required=True)
    ap.add_argument('--threshold', type=float, default=0.5)
    ap.add_argument('--grounding_batch_size', type=int, default=4)
    a = ap.parse_args()

    case, out = a.case.resolve(), a.out.resolve()
    meta = json.loads((case / 'metadata.json').read_text())
    w, h = map(int, meta['WH'])
    n = int(meta['frame_num'])
    fps = meta.get('fps', 30)
    bx, by, bw, bh = a.bbox
    if not (0 <= bx < 1 and 0 <= by < 1 and bw > 0 and bh > 0 and bx + bw <= 1.0001 and by + bh <= 1.0001):
        raise ValueError('bbox must be normalized x,y,w,h inside the frame')
    box = (int(round(bx * w)), int(round(by * h)), min(w, int(round((bx + bw) * w))), min(h, int(round((by + bh) * h))))
    if box[2] - box[0] < 4 or box[3] - box[1] < 4:
        raise ValueError('bbox is too small')
    out.mkdir(parents=True)  # must be new; never merge into an older result
    (out / 'seed').mkdir()

    import torch
    from sam3.model_builder import build_sam3_predictor

    first = np.asarray(Image.open(case / f'color/{a.cam}/0.png').convert('RGB'))
    if first.shape[:2] != (h, w):
        raise ValueError('Frame size differs from metadata WH')
    seed_video, track_video = out / 'seed_input.mp4', out / 'track_input.mp4'
    encode(case, a.cam, 1, fps, seed_video)
    encode(case, a.cam, n, fps, track_video)

    started = time.monotonic()
    p = build_sam3_predictor(version='sam3.1', use_fa3=False)
    p.model.batched_grounding_batch_size = a.grounding_batch_size
    p.model.postprocess_batch_size = a.grounding_batch_size
    print(f'SAM: 물체 bbox 초기화 cam {a.cam} bbox(px)={box} text={a.text!r}', flush=True)

    attempt, found, tried = seed_candidates(p, seed_video, w, h, (bx, by, bw, bh), a.text.strip(), a.threshold)
    if not found:
        raise SystemExit(f'cam {a.cam}: 첫 프레임 bbox 안에서 물체 후보를 찾지 못했습니다 '
                         f'({tried}). bbox/텍스트를 다시 지정해 새로 실행하세요. 빈 추적은 시작하지 않습니다.')
    inside = np.zeros((h, w), bool)
    inside[box[1]:box[3], box[0]:box[2]] = True
    rows = []
    for c in found:
        area = int(c['mask'].sum())
        c['area'], c['inside'] = area, int((c['mask'] & inside).sum())
        overlay(first, c['mask'], box, out / 'seed' / f'candidate_{c["id"]}.jpg')
        rows.append({'id': c['id'], 'area': area, 'inside_bbox': c['inside'],
                     'inside_fraction': round(c['inside'] / area, 4), 'prob': c['prob']})
    chosen = max(found, key=lambda c: (c['inside'], c['inside'] / c['area'], c['prob'] or 0))
    if chosen['inside'] / chosen['area'] < 0.5:
        raise SystemExit(f'cam {a.cam}: bbox 후보 마스크가 대부분 bbox 밖에 있습니다 ({rows}). '
                         'bbox를 다시 지정해 새로 실행하세요.')
    seed = chosen['mask']
    overlay(first, seed, box, out / 'seed' / 'selected.jpg')
    print(f'SAM: 초기 마스크 선택 id={chosen["id"]} area={chosen["area"]} '
          f'({attempt}, 후보 {len(found)}개)', flush=True)

    # Track only the chosen seed mask. The detector is not prompted again, so a new
    # text match (shadow, sleeve, second object) cannot be added later in the clip.
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
                    progress(track_video, n, 'bbox_track', f + 1, started)
                    last = time.monotonic()
    finally:
        p.handle_request(request=dict(type='close_session', session_id=sid))
    seed_video.unlink()
    track_video.unlink()
    record = {'camera': a.cam, 'mode': 'bbox_track', 'bbox_norm_xywh': [bx, by, bw, bh], 'bbox_px_xyxy': list(box),
              'text': a.text, 'seed_attempts': tried, 'seed_attempt_used': attempt, 'candidates': rows,
              'selected_candidate': chosen['id'], 'seed_auto_selected': True, 'frames': n,
              'nonempty_frames': n - len(empty), 'empty_frames': empty, 'wall_s': round(time.monotonic() - started, 1),
              'threshold': a.threshold, 'human_approved': False,
              'note': 'Single tracked object from the first-frame seed; shadows or wrong parts inside the '
                      'same object are still possible. Review frame 0, fast motion, occlusion and the end.'}
    (out / 'record.json').write_text(json.dumps(record, indent=2, ensure_ascii=False))
    print(f'OBJECT_TRACK_COMPLETE cam {a.cam} nonempty {n - len(empty)}/{n} empty {len(empty)}', flush=True)


if __name__ == '__main__':
    main()
