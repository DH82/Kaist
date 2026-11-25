"""Convert NumPy-only WFM deliveries into the offline viewer schema."""
from pathlib import Path
import csv
import hashlib
import json
import pickle
import re
import subprocess
import numpy as np

class ArrayUnpickler(pickle.Unpickler):
    def find_class(self, module, name):
        allowed = {
            ('numpy', 'ndarray'), ('numpy', 'dtype'),
            ('numpy.core.multiarray', '_reconstruct'), ('numpy.core.multiarray', 'scalar'),
            ('numpy._core.multiarray', '_reconstruct'), ('numpy._core.multiarray', 'scalar'),
            ('numpy.core.numeric', '_frombuffer'), ('numpy._core.numeric', '_frombuffer'),
        }
        if (module, name) not in allowed:
            raise ValueError(f'Unsupported pickle type {module}.{name}. Export NumPy arrays or frame NPZ files first.')
        return super().find_class(module, name)

def load(path):
    with path.open('rb') as stream:
        return ArrayUnpickler(stream).load()

def flatten(value, digits=5):
    return np.round(np.asarray(value), digits).reshape(-1).tolist()

def convert(source, output, mano_fit=None, fps_override=None):
    source = source.resolve()
    hand_path = source / 'shape/hand/hand_mano_trajectory.pkl'
    object_path = source / 'final_data.pkl'
    for path in (hand_path, object_path):
        if not path.is_file():
            raise ValueError(f'Required file missing: {path}')
    hand, obj = load(hand_path), load(object_path)
    vertices = np.asarray(hand['mano_verts'])
    if vertices.ndim != 4 or vertices.shape[2:] != (778, 3):
        raise ValueError('mano_verts must have shape (frames, hands, 778, 3)')
    count, hands = vertices.shape[:2]
    ids = np.asarray(hand['frame_ids']).tolist()
    if ids != list(range(count)):
        raise ValueError('Expected consecutive prepared frame_ids from 0. Supply an aligned export.')
    sides = hand['hand_order']
    valid_hand = np.asarray(hand['mano_valid'], dtype=bool)
    if len(sides) != hands or valid_hand.shape != (count, hands):
        raise ValueError('Hand dimensions do not match')
    if str(hand.get('units', '')).lower() not in ('meters', 'meter', 'm'):
        raise ValueError('Input must declare hand units as meters')
    points = np.asarray(obj['object_points'])
    if points.ndim != 3 or points.shape[0] != count or points.shape[2] != 3:
        raise ValueError('Object trajectory must align to all hand frames')
    faces = {}
    for side in sides:
        arr = np.asarray(hand['faces'][side])
        if arr.ndim != 2 or arr.shape[1] != 3 or arr.min() < 0 or arr.max() >= 778:
            raise ValueError('Invalid MANO faces')
        faces[side] = arr.reshape(-1).astype(int).tolist()
    provenance_path = source / 'raw_frame_provenance.csv'
    provenance = list(csv.DictReader(provenance_path.open())) if provenance_path.exists() else []
    if provenance and len(provenance) != count:
        raise ValueError('RAW provenance frame count differs')
    variant = 'delivery hand_mano_trajectory.pkl'
    fit_hashes = {}
    if mano_fit:
        mano_fit = mano_fit.resolve()
        mapping_path = mano_fit.parent / 'source_frames.json'
        if not mapping_path.exists() or not provenance:
            raise ValueError('NPZ override requires source_frames.json and raw_frame_provenance.csv')
        mapping = json.loads(mapping_path.read_text(encoding='utf-8'))
        if mapping['prepared_frames'] != ids:
            raise ValueError('NPZ prepared frame mapping differs')
        for i in range(count):
            if mapping['raw_indices'][i] != [int(provenance[i][f'cam_{cam}']) for cam in range(1, 5)]:
                raise ValueError(f'NPZ RAW frame mapping differs at {i}')
        vertices = vertices.copy()
        for i in range(count):
            for h, side in enumerate(sides):
                path = mano_fit / f'joints_{i:06d}_{side}.npz'
                if not path.exists():
                    raise ValueError(f'NPZ override missing: {path.name}')
                with np.load(path, allow_pickle=False) as frame:
                    vertices[i, h] = frame['vertices_world'].reshape(778, 3)
                fit_hashes[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
        variant = 'NPZ override: ' + mano_fit.name
    if not np.isfinite(vertices[valid_hand]).all():
        raise ValueError('Valid hand frames contain nonfinite vertices')
    colors = np.asarray(obj['object_colors']) if 'object_colors' in obj else None
    if colors is not None and colors.shape != points.shape:
        raise ValueError('object_colors must match object_points shape')
    visibility = np.asarray(obj.get('object_visibilities', np.ones(points.shape[:2], dtype=bool))).reshape(points.shape[:2])
    motion = np.asarray(obj.get('object_motions_valid', np.ones(points.shape[:2], dtype=bool))).reshape(points.shape[:2])
    point_ids = np.unique(np.linspace(0, points.shape[1]-1, min(2400, points.shape[1])).astype(int))
    videos = sorted((source/'color').glob('*.mp4'), key=lambda p: int(p.stem))
    if not videos:
        raise ValueError('No color/<camera number>.mp4 found')
    try:  # 오프라인 뷰어는 동봉 ffmpeg, 작업자 플랫폼은 시스템 ffmpeg를 씁니다.
        import imageio_ffmpeg
        ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    except ImportError:
        import shutil
        ffmpeg = shutil.which('ffmpeg')
        if not ffmpeg:
            raise ValueError('ffmpeg not found')
    rgb_paths, rates = [], []
    output.mkdir(parents=True, exist_ok=True)
    for video in videos:
        probe = subprocess.run([ffmpeg, '-hide_banner', '-i', str(video)], capture_output=True, text=True, errors='replace')
        found = re.search(r'(\d+(?:\.\d+)?) fps', probe.stderr)
        if not found and fps_override is None:
            raise ValueError(f'Could not determine fps of {video.name}; supply --fps')
        rates.append(float(found.group(1)) if found else float(fps_override))
        dest = output/'rgb'/('cam'+video.stem)
        dest.mkdir(parents=True, exist_ok=True)
        print(f'Decoding {video.name}', flush=True)
        subprocess.run([ffmpeg, '-hide_banner', '-loglevel', 'error', '-y', '-i', str(video), '-vsync', '0', '-start_number', '0', '-q:v', '3', str(dest/'%04d.jpg')], check=True)
        if len(list(dest.glob('*.jpg'))) != count:
            raise ValueError(f'{video.name}: RGB and 3D frame counts differ')
        rgb_paths.append([f'rgb/cam{video.stem}/{i:04d}.jpg' for i in range(count)])
    if max(rates)-min(rates) > 0.02:
        raise ValueError('Camera frame rates differ')
    fps = float(fps_override) if fps_override is not None else rates[0]
    if fps <= 0:
        raise ValueError('fps must be positive')
    frames = []
    for i in range(count):
        p = points[i, point_ids]
        valid = visibility[i, point_ids].astype(bool) & motion[i, point_ids].astype(bool) & np.isfinite(p).all(-1) & (np.linalg.norm(p, axis=-1)>0)
        record = {'id': i, 'hands': [{'side': side, 'valid': bool(valid_hand[i,h]), 'vertices': flatten(np.nan_to_num(vertices[i,h]))} for h,side in enumerate(sides)], 'object': flatten(np.nan_to_num(p)), 'objectValid': valid.tolist()}
        if colors is not None:
            record['objectColors'] = flatten(np.nan_to_num(colors[i,point_ids]), 3)
        frames.append(record)
    review_path = source/'review_status.json'
    review = json.loads(review_path.read_text(encoding='utf-8')) if review_path.exists() else {}
    data = {'episode': source.name, 'fps': fps, 'frameCount': count, 'frames': frames, 'faces': faces, 'rgb': {'cameraNames': ['CAM '+str(int(v.stem)+1) for v in videos], 'paths': rgb_paths}, 'units': 'meters', 'handVariant': variant, 'sourceNote': variant+' · 물체 3D 추적점 · 검수 상태는 원본 기록 참조', 'provenance': provenance, 'reviewStatus': review}
    (output/'data.json').write_text(json.dumps(data, ensure_ascii=False, separators=(',', ':'), allow_nan=False), encoding='utf-8')
    receipt = {'source': str(source), 'handVariant': variant, 'frameCount': count, 'fps': fps, 'rgbFrameCountsEqual': True, 'rawMappingChecked': bool(provenance), 'sourceHashes': {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in (hand_path,object_path)}, 'fitHashes': fit_hashes, 'objectPointIds': point_ids.tolist(), 'worldConvention': hand.get('world_frame', 'not provided')}
    (output/'data_audit.json').write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding='utf-8')
    return output
