#!/usr/bin/env python3
"""Same hand-ROI candidate as 00_scripts/run_hand_roi.py -> roi_camera.py, with CPU work hidden behind the GPU.

roi_camera.main() does, per camera: crop + 4x LANCZOS -> PNG files -> ffmpeg (libx264rgb crf 0) -> SAM.
The crop/encode step (~8-13 s per camera) ran while the GPU sat idle. Here:
  * the next camera's ROI video is prepared in a background thread while SAM runs on the current one;
  * frames are piped to ffmpeg as raw RGB (no PNG round trip) and encoded with -preset ultrafast.
    crf 0 with libx264rgb is lossless, so the decoded frames SAM sees are identical.
Everything else (ROI box, prompts, threshold, paste/fallback, records) is roi_camera.py's own code or a
line-for-line copy of it. sam3_fast.compatible() pins roi_camera.py/run_hand_roi.py, so this runner is
only used with the exact version it mirrors.

Usage: sam_roi_fast.py --scripts <00_scripts> --source-script <segment_object_sam3.py> --case <dest>
                       --out <new dir> --profile <cfg.json> --cameras 0 1 2 3
"""
import argparse
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
from PIL import Image


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def prepare(roi, case, out, cam, cfg, w, h, n, fps):
    """roi_camera.main()'s per-camera input preparation, before the SAM session."""
    camera_out = out / f'mask/{cam}'
    camera_out.mkdir(parents=True)
    if (case / f'mask/{cam}/0').exists():
        (camera_out / '0').symlink_to(os.path.relpath(case / f'mask/{cam}/0', camera_out), target_is_directory=True)
    if (case / f'mask/{cam}/1').exists():
        shutil.copytree(case / f'mask/{cam}/1', camera_out / '1')
    baselines = [roi.loadmask(case / f'mask/{cam}/1/{f}.png', (w, h)) for f in range(n)]
    union = np.logical_or.reduce(baselines)
    box = roi.bounding_roi(union, cfg['padding_fraction'], w, h)
    if box is None:
        return baselines, None, None
    work = out / f'roi_camera_{cam}'
    work.mkdir()
    video = work / 'input.mp4'
    size = None
    proc = None
    try:
        for f in range(n):
            im = Image.open(case / f'color/{cam}/{f}.png').convert('RGB').crop(tuple(box))
            im = im.resize((im.width * cfg['scale'], im.height * cfg['scale']), Image.Resampling.LANCZOS)
            if proc is None:
                size = im.size
                proc = subprocess.Popen(['ffmpeg', '-v', 'error', '-n', '-f', 'rawvideo', '-pix_fmt', 'rgb24',
                                         '-s', f'{size[0]}x{size[1]}', '-framerate', str(fps), '-i', '-',
                                         '-frames:v', str(n), '-c:v', 'libx264rgb', '-crf', '0',
                                         '-preset', 'ultrafast', str(video)], stdin=subprocess.PIPE)
            if im.size != size:
                raise ValueError('ROI frame size changed')
            proc.stdin.write(im.tobytes())
        proc.stdin.close()
        if proc.wait() != 0:
            raise subprocess.CalledProcessError(proc.returncode, 'ffmpeg')
    finally:
        if proc and proc.poll() is None:
            proc.kill()
    return baselines, box, video


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--scripts', type=Path, required=True)
    p.add_argument('--source-script', type=Path, required=True)
    p.add_argument('--case', type=Path, required=True)
    p.add_argument('--out', type=Path, required=True)
    p.add_argument('--profile', type=Path, required=True)
    p.add_argument('--cameras', type=int, nargs='+')
    p.add_argument('--feature-reuse', action='store_true', help='test option: sam_feature_reuse.py')
    a = p.parse_args()
    sys.path.insert(0, str(a.scripts))
    roi = load_module('delivered_roi', a.scripts / 'roi_camera.py')
    from sam_progress import install

    cfg = json.loads(a.profile.read_text())
    case, out = a.case.resolve(), a.out.resolve()
    if out.exists():
        raise ValueError('Output exists')
    if not (case / 'mask').exists():
        raise ValueError('Run full-frame SAM first')
    meta = json.loads((case / 'metadata.json').read_text())
    w, h = meta['WH']
    n = int(meta['frame_num'])
    fps = meta.get('fps', 30)
    if n > cfg['max_frames']:
        raise ValueError('Pilot maximum exceeded; chunk/track ROI design required for longer clips')
    sam = load_module('source_sam', a.source_script)
    # run_hand_roi.py's batch=4 memory setting, then the read-only progress wrapper (same order).
    build = sam.build_sam3_predictor

    def configured(*args, **kwargs):
        predictor = build(*args, **kwargs)
        predictor.model.batched_grounding_batch_size = 4
        predictor.model.postprocess_batch_size = 4
        return predictor
    sam.build_sam3_predictor = configured
    install(sam)
    if a.feature_reuse:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import sam_feature_reuse
        sam_feature_reuse.install()
    out.mkdir(parents=True)
    cams = list(a.cameras if a.cameras is not None else range(meta.get('num_cameras', 4)))
    records = []
    with ThreadPoolExecutor(max_workers=1) as pool:
        ahead = pool.submit(prepare, roi, case, out, cams[0], cfg, w, h, n, fps)
        predictor = sam.build_sam3_predictor(version=cfg['model_version'], use_fa3=False)
        for i, cam in enumerate(cams):
            baselines, box, video = ahead.result()
            if i + 1 < len(cams):
                ahead = pool.submit(prepare, roi, case, out, cams[i + 1], cfg, w, h, n, fps)
            if box is None:
                records.append({'camera': cam, 'status': 'no_baseline_detection_roi_not_available'})
                continue
            # From here on: roi_camera.main() unchanged.
            start = time.perf_counter()
            sid = predictor.handle_request(request={'type': 'start_session', 'resource_path': str(video),
                                                    'offload_video_to_cpu': True})['session_id']
            try:
                masks = {}
                first = True
                for prompt in cfg['hand_prompts']:
                    m = sam.union_concept(predictor, sid, prompt, 0, first, cfg['threshold'])
                    first = False
                    for f, v in m.items():
                        masks[f] = v if f not in masks else masks[f] | v
                for prompt in cfg['subtract_prompts']:
                    m = sam.union_concept(predictor, sid, prompt, 0, False, cfg['threshold'])
                    for f, v in m.items():
                        if f in masks:
                            masks[f] &= ~v
            finally:
                predictor.handle_request(request={'type': 'close_session', 'session_id': sid})
            rows = []
            for f, base in enumerate(baselines):
                if f in masks and masks[f].any():
                    outmask = roi.paste_roi(base, masks[f], box)
                    mode = 'replace_inside_roi_preserve_outside'
                else:
                    outmask = base
                    mode = 'empty_roi_fallback_to_baseline'
                path = out / f'mask/{cam}/1/{f}.png'
                path.parent.mkdir(parents=True, exist_ok=True)
                Image.fromarray(outmask.astype('uint8') * 255).save(path)
                union_count = np.logical_or(base, outmask).sum()
                rows.append({'frame': f, 'mode': mode, 'baseline_area': int(base.sum()),
                             'candidate_area': int(outmask.sum()),
                             'difference_pixels': int(np.logical_xor(base, outmask).sum()),
                             'agreement_iou_not_accuracy': float(np.logical_and(base, outmask).sum() / union_count)
                             if union_count else None})
            records.append({'camera': cam, 'roi_xyxy': box, 'frames': rows, 'wall_s': time.perf_counter() - start})
            print('ROI_COMPLETE', cam, flush=True)
            (out / 'progress.json').write_text(json.dumps({'completed_cameras': cam + 1,
                                                           'total_cameras': meta.get('num_cameras', 4)}, indent=2))
    (out / 'candidate_record.json').write_text(json.dumps({
        'profile': cfg, 'source_script_sha256': hashlib.sha256(a.source_script.read_bytes()).hexdigest(),
        'human_approved': False, 'candidate_only': True,
        'note': 'No unseen second hand recovery guarantee. Hand ROI only; object masks are unchanged. No production adoption.',
        'results': records}, indent=2))


if __name__ == '__main__':
    main()
