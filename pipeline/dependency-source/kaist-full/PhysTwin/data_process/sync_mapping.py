"""Versioned host timestamp mapping. Clock/exposure semantics are unknown."""
import csv, math
from pathlib import Path
POLICY = 'host_overlap_max_count_reference_nearest_v1'
DEFAULT_TOL_S = .025

def read_rows(src, cam):
    with (Path(src)/f'{cam}_timestamps.csv').open() as f:
        rows=list(csv.DictReader(f))
    ids=[int(r['frame_idx']) for r in rows]
    times=[float(r['host_time_s']) for r in rows]
    if not rows or len(set(ids)) != len(ids) or any(i<0 for i in ids) or any(not math.isfinite(t) for t in times):
        raise ValueError('Invalid/duplicate timestamp entries: '+cam)
    if any(b<a for a,b in zip(times,times[1:])):raise ValueError('Non-monotonic host timestamps: '+cam)
    return {int(r['frame_idx']):r for r in rows}

def sync_frames(cam_ts, fps, tol_s):
    """Align cameras using the reference camera's real frame timeline.

    Instead of resampling onto an artificial uniform grid (which drops ~half the
    frames when a fixed grid drifts against ~30fps cameras), use the actual frame
    times of the reference camera (the one with the most frames inside the
    overlap window) as the timeline, and snap every other camera to its nearest
    frame. This preserves full framerate over the whole overlap window.

    cam_ts: list (per camera) of dict local_idx -> host_time_s
    Returns: list of tuples (one local frame idx per camera), and the frame times.
    """
    cam_sorted = [sorted((t, i) for i, t in ts.items()) for ts in cam_ts]
    overlap_start = max(items[0][0] for items in cam_sorted)
    overlap_end = min(items[-1][0] for items in cam_sorted)
    if overlap_end <= overlap_start:
        raise RuntimeError("No overlapping time window across cameras.")

    def in_window(items):
        return [(t, i) for t, i in items if overlap_start - 1e-9 <= t <= overlap_end + 1e-9]

    # reference = camera with the most real frames spanning the overlap window
    ref = max(range(len(cam_sorted)), key=lambda k: len(in_window(cam_sorted[k])))
    ref_frames = in_window(cam_sorted[ref])

    synced, frame_times = [], []
    for t_ref, _ in ref_frames:
        picks, ok = [], True
        for items in cam_sorted:
            best_i, best_d = None, None
            for tt, idx in items:
                d = abs(tt - t_ref)
                if best_d is None or d < best_d:
                    best_d, best_i = d, idx
            if best_d is None or best_d > tol_s:
                ok = False
                break
            picks.append(best_i)
        if ok:
            synced.append(tuple(picks))
            frame_times.append(t_ref)
    return synced, frame_times


def source_mapping(src,cams,tol_s=DEFAULT_TOL_S,fps=30):
    rows=[read_rows(src,c) for c in cams]
    times=[{i:float(r['host_time_s']) for i,r in rs.items()} for rs in rows]
    picks,anchors=sync_frames(times,fps,tol_s)
    lo=max(min(t.values()) for t in times);hi=min(max(t.values()) for t in times)
    ref=max(range(len(times)),key=lambda k:sum(lo-1e-9<=t<=hi+1e-9 for t in times[k].values()))
    seen=[set() for _ in cams];frames=[]
    for f,(indices,t) in enumerate(zip(picks,anchors)):
        views=[]
        for c,i in enumerate(indices):
            r=rows[c][i];host=float(r['host_time_s']);hw=r.get('hw_timestamp_ms')
            views.append({'camera':c,'raw_camera':cams[c],'raw_idx':i,'host_time_s':host,'hw_timestamp_ms':float(hw) if hw else None,'residual_ms':(host-t)*1000,'duplicate_source':i in seen[c]})
            seen[c].add(i)
        frames.append({'output_idx':f,'pretrim_idx':f,'pretrim_frame':f,'frame':f,'reference_time_s':t,'reference_host_s':t,'raw_indices':list(indices),'host_ms':[v['host_time_s']*1000 for v in views],'hardware_ms':[v['hw_timestamp_ms'] for v in views],'cameras':views})
    return {'schema_version':1,'raw_indices':[list(ids) for ids in picks],'hardware_common_clock_verified':False,'policy':POLICY,'sync_tol_ms':tol_s*1000,'reference_camera':ref,'timestamp_basis':'host_time_s','clock_domain':'unknown','exposure_semantics':'unknown; host timestamps do not certify simultaneous exposures','overlap_start':lo,'overlap_end':hi,'frames':frames}

def trim_mapping(mapping,ts,te,n):
    if len(mapping['frames'])!=n or ts<0 or te<0 or ts+te>=n:raise ValueError('Invalid source mapping/trim')
    out=dict(mapping);out['frames']=[dict(row,output_idx=i,frame=i) for i,row in enumerate(mapping['frames'][ts:n-te])]
    out['raw_indices']=[r['raw_indices'] for r in out['frames']]
    out['trim_history']=mapping.get('trim_history',[])+[{'before':n,'start':ts,'end':te}]
    return out
