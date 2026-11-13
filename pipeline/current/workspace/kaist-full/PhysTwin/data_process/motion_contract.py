"""Versioned forward motion validity; original point columns never renumbered."""
import numpy as np
from scipy.spatial import cKDTree

SCHEMA = {"version": 2, "direction": "forward", "interval": "[t,t+1]",
          "array_layout": "T,N; last row false", "units": "meters",
          "point_identity": "column index persistent across frames"}

def forward_valid(vis):
    vis = np.asarray(vis, dtype=bool)
    out = np.zeros_like(vis)
    out[:-1] = vis[:-1] & vis[1:]
    return out

def legacy_backward_to_forward(mask):
    """Explicit adapter ONLY for legacy MANO backward masks, never object masks."""
    out = np.zeros_like(np.asarray(mask, dtype=bool))
    out[:-1] = mask[1:]
    return out

def frozen_motion_filter(points, vis, neighbor_dist=0.01, eligible=None):
    points = np.asarray(points)
    source = forward_valid(vis)
    if eligible is not None:
        source &= np.asarray(eligible, dtype=bool)[None, :]
    result = source.copy()
    for t in range(len(points)-1):
        ids = np.flatnonzero(source[t])
        if not len(ids): continue
        xyz = points[t, ids]
        if not np.isfinite(xyz).all():
            raise ValueError("Non-finite visible points in motion filter")
        tree = cKDTree(xyz)
        motion = points[t+1, ids] - xyz
        for local, j in enumerate(ids):
            nb = np.asarray(tree.query_ball_point(xyz[local], neighbor_dist), dtype=int)
            # Open3D radius search is strict; retain the original radius convention.
            nb = nb[np.linalg.norm(xyz[nb]-xyz[local], axis=1) < neighbor_dist]
            if len(nb) < 5 or (np.linalg.norm(motion[local]-motion[nb], axis=1) < neighbor_dist/2).sum() < .5*len(nb):
                result[t,j] = False
    return result

def validate_motion(data, prefix):
    """Fail closed before committing a v2 forward-interval export."""
    if data.get('motion_schema') != SCHEMA:
        raise ValueError('Missing/incompatible motion schema; regenerate upstream tracking')
    points=np.asarray(data[prefix+'_points'])
    vis=np.asarray(data[prefix+'_visibilities'],dtype=bool)
    motion=np.asarray(data[prefix+'_motions_valid'],dtype=bool)
    if points.ndim != 3 or points.shape[-1] != 3 or points.shape[:2] != vis.shape or motion.shape != vis.shape:
        raise ValueError('Motion export dimensions do not match')
    if len(points)==0 or motion[-1].any() or (motion & ~forward_valid(vis)).any():
        raise ValueError('Motion validity is not a supported forward interval')
    if not np.isfinite(points[vis]).all():
        raise ValueError('Visible exported points contain non-finite coordinates')
    return True
