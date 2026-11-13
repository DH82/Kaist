"""Content contracts and atomic commits for derived MANO exports."""
import hashlib
import os
import pickle
import tempfile
from pathlib import Path

def sha256(path):
    h=hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda:f.read(1024*1024), b''): h.update(chunk)
    return h.hexdigest()

def fit_contract(case, log):
    root=Path(case)
    files=[f'{log}/hand_reg.pkl', f'{log}/hand_reg_params.pkl', 'metadata.json']
    for optional in ['source_frames.json', 'calibrate.pkl']:
        if (root/optional).is_file(): files.append(optional)
    missing=[f for f in files if not (root/f).is_file()]
    if missing: raise ValueError(f'Missing fit provenance inputs: {missing}')
    return {'version':2, 'fit_log':log, 'files':{f:sha256(root/f) for f in files}}

def validate_trajectory(data, case, log):
    expected=fit_contract(case, log)
    if data.get('fit_provenance') != expected:
        raise ValueError('Stale/unverified MANO trajectory: regenerate export_hand_mano_trajectory.py for the requested --log')
    return expected

def atomic_pickle(path, value):
    path=Path(path); path.parent.mkdir(parents=True,exist_ok=True)
    fd,tmp=tempfile.mkstemp(prefix=path.name+'.partial-', dir=path.parent)
    try:
        with os.fdopen(fd,'wb') as f:
            pickle.dump(value,f); f.flush(); os.fsync(f.fileno())
        os.replace(tmp,path)
    finally:
        if os.path.exists(tmp): os.unlink(tmp)

def tracked_hand_source(case):
    """Finalizer moves the track file; if both layouts exist they MUST agree."""
    import numpy as np
    from motion_contract import validate_motion
    root=Path(case)
    shape=root/'shape/hand/hand_pointcloud_3d.pkl'
    current=root/'hand_track_data.pkl'
    selected=shape if shape.is_file() else current
    if not selected.is_file():
        raise ValueError('No hand point cloud found')
    paths=[selected]
    if shape.is_file() and current.is_file(): paths=[shape,current]
    contract={str(p.relative_to(root)):sha256(p) for p in paths}
    with selected.open('rb') as f:data=pickle.load(f)
    validate_motion(data,'hand')
    if len(paths)==2:
        with current.open('rb') as f:latest=pickle.load(f)
        validate_motion(latest,'hand')
        keys=['hand_points','hand_colors','hand_visibilities','hand_motions_valid']
        same=data.get('motion_schema')==latest.get('motion_schema')
        for key in keys:
            a,b=np.asarray(data[key]),np.asarray(latest[key])
            same = same and a.dtype==b.dtype and np.array_equal(a,b,equal_nan=True)
        if not same:
            raise ValueError('Stale shape/hand/hand_pointcloud_3d.pkl differs from current hand_track_data.pkl; regenerate/finalize the derived hand artifact before export. No source was silently substituted.')
    mode='exact_array_and_schema_equivalence' if len(paths)==2 else ('canonical_moved_shape_source' if selected==shape else 'current_hand_track')
    proof={'version':2,'selected':str(selected.relative_to(root)),'files':contract,'validation':mode}
    verify_tracked_hand_source(root,proof)
    return data,selected,proof

def verify_tracked_hand_source(case,proof):
    root=Path(case)
    for name,expected in proof['files'].items():
        if not (root/name).is_file() or sha256(root/name)!=expected:
            raise ValueError('Tracked hand input changed during export; no output committed')
    # A newer primary track appearing while using a moved canonical file is also a change.
    if proof['validation']=='canonical_moved_shape_source' and (root/'hand_track_data.pkl').exists():
        raise ValueError('New hand_track_data.pkl appeared during export; retry equivalence validation')
