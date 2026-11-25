"""파이프라인 최종 결과(NAS results/<pc>/<대상>/<ep>/<key>)를 WFM 뷰어 입력으로 바꿉니다.

뷰어(app/convert.py)는 표준 납품 폴더(shape/hand/hand_mano_trajectory.pkl, final_data.pkl, color/N.mp4)를 받습니다.
파이프라인 결과에는 hand_mano_trajectory.pkl이 없으므로 hands/hand_v1/hand_reg.pkl(프레임별 778개 정점)에서
같은 형식으로 만들어 임시 납품 폴더를 구성한 뒤, 뷰어의 convert()를 그대로 호출합니다. 원본은 읽기만 합니다.

작업자 플랫폼(code/platform/viz.py)이 UI와 같은 Python(numpy 필요)과 시스템 ffmpeg로 실행합니다:
  python3 viz_prepare.py --episode ep_... --result <결과 폴더> --color <03_prepared/ep/color> --faces <mano_faces.pkl> --out <빈 폴더>
"""
from pathlib import Path
import argparse
import json
import pickle
import shutil
import sys
import tempfile

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE/'app'))
import numpy as np
from convert import convert, load

VERTS = 778


def trajectory(reg, faces_all, frames):
    """hand_reg.pkl {frame: {side: (778,3)}} → 뷰어가 읽는 hand_mano_trajectory 형식."""
    entries = {}
    for key, entry in reg.items():
        if entry is None:
            entries[int(key)] = {}
        elif isinstance(entry, dict):
            entries[int(key)] = {s: v for s, v in entry.items() if v is not None}
        else:
            v = np.asarray(entry, dtype=np.float64).reshape(-1, 3)
            entries[int(key)] = {'left': v[:VERTS], 'right': v[VERTS:]} if len(v) == 2*VERTS else {'right': v}
    order = sorted({s for sides in entries.values() for s in sides})
    if not order:
        raise ValueError('hand_reg.pkl에 피팅된 손이 없습니다.')
    count = max([frames] + [f+1 for f in entries])
    verts = np.full((count, len(order), VERTS, 3), np.nan, dtype=np.float32)
    valid = np.zeros((count, len(order)), dtype=bool)
    for f, sides in entries.items():
        for side, v in sides.items():
            v = np.asarray(v, dtype=np.float32).reshape(-1, 3)
            if f < 0 or v.shape[0] != VERTS or not np.isfinite(v).all():
                continue
            verts[f, order.index(side)] = v
            valid[f, order.index(side)] = True
    return {
        'mano_verts': verts, 'mano_valid': valid, 'hand_order': order,
        'faces': {s: np.asarray(faces_all[s]).astype(np.int32) for s in order},
        'frame_ids': np.arange(count, dtype=np.int32), 'units': 'meters',
        'source': 'hands/hand_v1/hand_reg.pkl (viz_prepare.py)',
        'world_frame': 'PhysTwin convention; same frame as calibrate.pkl',
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--episode', required=True)
    ap.add_argument('--result', type=Path, required=True)
    ap.add_argument('--color', type=Path, required=True)
    ap.add_argument('--out', type=Path, required=True)
    ap.add_argument('--faces', type=Path, required=True)
    args = ap.parse_args()
    final = args.result/'final_data.pkl'
    reg_path = args.result/'hands/hand_v1/hand_reg.pkl'
    for p in (final, reg_path, args.faces):
        if not p.is_file():
            raise ValueError(f'필요한 파일이 없습니다: {p}')
    if not list(args.color.glob('*.mp4')):
        raise ValueError(f'동기화된 RGB 영상(color/N.mp4)이 없습니다: {args.color}')
    print('손 궤적 준비 중', flush=True)
    frames = int(np.asarray(load(final)['object_points']).shape[0])
    traj = trajectory(load(reg_path), load(args.faces), frames)
    with tempfile.TemporaryDirectory(prefix='viz-stage-') as tmp:
        stage = Path(tmp)/args.episode
        (stage/'shape/hand').mkdir(parents=True)
        with (stage/'shape/hand/hand_mano_trajectory.pkl').open('wb') as f:
            pickle.dump(traj, f, protocol=4)
        (stage/'final_data.pkl').symlink_to(final.resolve())
        (stage/'color').symlink_to(args.color.resolve(), target_is_directory=True)
        if args.out.exists():
            shutil.rmtree(args.out)
        print('3D·RGB 변환 중', flush=True)
        convert(stage, args.out)
    audit = json.loads((args.out/'data_audit.json').read_text(encoding='utf-8'))
    audit.update({'source': str(args.result), 'colorSource': str(args.color),
                  'handSource': str(reg_path),
                  'handValidity': 'finite vertices only (camera-support filter of the delivery export is not applied)'})
    (args.out/'data_audit.json').write_text(json.dumps(audit, ensure_ascii=False, indent=2), encoding='utf-8')
    print('완료', flush=True)


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print('ERROR:', exc, file=sys.stderr)
        sys.exit(1)
