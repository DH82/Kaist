"""손 포인트 추적(sam_hand_point_track.py)을 카메라별로 180° 돌린 영상에서 실행하는 래퍼입니다.

왜: 카메라 2(인덱스 1)는 작업자 팔이 화면 아래 가장자리에서 들어오고 손가락이 위를 향합니다. 이 방향에서는 SAM3.1
추적 점수가 낮게 시작해 서서히 떨어지고, 0 아래로 내려가면 손을 놓친 뒤 다시 잡지 못합니다(11-25·11-26 촬영분).
같은 영상을 180° 돌려 추적하면 점수가 높게 유지됩니다(시험 30개: 손 프레임 1201→3317/3621, 정상 영상은 그대로 100%,
되돌린 마스크와 운영 마스크 IoU 0.91~0.97).

동작: job에 "rotate": 180 이 있으면 그 카메라 프레임을 180° 돌린 사본을 만들고, 포인트도 같이 돌려
sam_hand_point_track.run_camera()를 그대로 부릅니다. 끝나면 마스크(1/<f>.png)와 첫 프레임 그림(seed/*.jpg)을 원래 방향으로
되돌리고 record.json에 원래 포인트와 회전 사실을 적습니다. 180° 회전은 픽셀 자리만 바꾸므로(보간 없음) 되돌리면 정확히 겹칩니다.
"rotate"가 없는 job은 run_camera()를 그대로 부릅니다(지금까지와 같은 결과).

sam_hand_point_track.py는 실행이 체크섬을 고정하는 파일이라 고치지 않고 이 파일에서 감쌉니다.

Usage (sam_hand_point_track.py와 같음, job에 "rotate" 추가 가능):
  sam-env/bin/python sam_hand_rotate.py --case <episode view> \
      --jobs '[{"cam":1,"points":[[0.52,0.40,1]],"out":"<new dir>","rotate":180}]'
"""
import argparse
import json
import shutil
from pathlib import Path

from PIL import Image

import sam_hand_point_track as hand
from sam_object_point_track import parse_points

ROTATE_180 = Image.Transpose.ROTATE_180


def flip_points(points):
    """정규화 포인트 [x, y, label]를 180° 돌린 자리로 옮깁니다."""
    return [[round(1 - x, 6), round(1 - y, 6), label] for x, y, label in points]


def rotated_case(case, cam, view):
    """case의 metadata.json과 color/<cam>/*.png만 180° 돌려 view에 둡니다(추적에 필요한 것만)."""
    meta = json.loads((case / 'metadata.json').read_text())
    frames = int(meta['frame_num'])
    (view / f'color/{cam}').mkdir(parents=True)
    shutil.copy(case / 'metadata.json', view / 'metadata.json')
    for f in range(frames):
        with Image.open(case / f'color/{cam}/{f}.png') as img:
            img.transpose(ROTATE_180).save(view / f'color/{cam}/{f}.png')
    return meta


def unrotate(out, points, meta):
    """run_camera() 결과를 원래 방향으로 되돌립니다."""
    for path in (out / '1').glob('*.png'):
        with Image.open(path) as img:
            back = img.transpose(ROTATE_180)
        back.save(path)
    for path in (out / 'seed').glob('*.jpg'):
        with Image.open(path) as img:
            back = img.transpose(ROTATE_180)
        back.save(path, quality=90)
    w, h = map(int, meta['WH'])
    record = json.loads((out / 'record.json').read_text())
    record.update(points_norm=points, points_px=[[int(x * w), int(y * h), label] for x, y, label in points],
                  rotated_180=True, tracked_points_norm=record['points_norm'],
                  note=record['note'] + ' Tracked on a 180-degree rotated copy of this camera; masks and seed images were rotated back.')
    (out / 'record.json').write_text(json.dumps(record, indent=2, ensure_ascii=False))


def run_job(p, case, job, thresh):
    if job['rotate'] != 180:
        return hand.run_camera(p, case, job['cam'], job['points'], job['out'], thresh)
    out = job['out']
    view = out.parent / f'.rotated-{out.name}'
    shutil.rmtree(view, ignore_errors=True)
    try:
        meta = rotated_case(case, job['cam'], view)
        print(f'SAM: 손 추적 cam {job["cam"]} — 180° 돌린 영상으로 추적합니다', flush=True)
        hand.run_camera(p, view, job['cam'], flip_points(job['points']), out, thresh)
        unrotate(out, job['points'], meta)
    finally:
        shutil.rmtree(view, ignore_errors=True)


def parse_jobs(text):
    jobs = []
    for j in json.loads(text):
        rotate = int(j.get('rotate', 0))
        if rotate not in (0, 180):
            raise ValueError('rotate는 0 또는 180만 됩니다.')
        jobs.append({'cam': int(j['cam']), 'points': parse_points(j['points']), 'out': Path(j['out']).resolve(), 'rotate': rotate})
    return jobs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--case', type=Path, required=True)
    ap.add_argument('--jobs', required=True, help='JSON [{"cam":0,"points":[[x,y,label],...],"out":"<new dir>","rotate":180}]')
    ap.add_argument('--threshold', type=float, default=0.5)
    ap.add_argument('--grounding_batch_size', type=int, default=4)
    a = ap.parse_args()
    jobs = parse_jobs(a.jobs)
    from sam3.model_builder import build_sam3_predictor
    p = build_sam3_predictor(version='sam3.1', use_fa3=False)
    p.model.batched_grounding_batch_size = a.grounding_batch_size
    p.model.postprocess_batch_size = a.grounding_batch_size
    for job in jobs:
        run_job(p, a.case.resolve(), job, a.threshold)


if __name__ == '__main__':
    main()
