"""Versioned timestamp mapping: camera clock (hw_timestamp_ms) when usable, else host arrival time."""
import csv, math
from pathlib import Path
POLICY = 'host_overlap_max_count_reference_nearest_v1'          # host_time_s timeline (fallback, unchanged)
HW_POLICY = 'camera_clock_overlap_max_count_reference_nearest_v2'  # same algorithm on hw_timestamp_ms
DEFAULT_TOL_S = .025
# hw_timestamp_ms is used only when it is RealSense global time (camera clock mapped to the PC clock):
# every row within this distance of host_time_s. Device-internal clocks (counting from 0) fail it -> host_time_s.
HW_MAX_HOST_OFFSET_S = 2.0

def read_rows(src, cam):
    with (Path(src)/f'{cam}_timestamps.csv').open() as f:
        rows=list(csv.DictReader(f))
    ids=[int(r['frame_idx']) for r in rows]
    times=[float(r['host_time_s']) for r in rows]
    if not rows or len(set(ids)) != len(ids) or any(i<0 for i in ids) or any(not math.isfinite(t) for t in times):
        raise ValueError('Invalid/duplicate timestamp entries: '+cam)
    if any(b<a for a,b in zip(times,times[1:])):raise ValueError('Non-monotonic host timestamps: '+cam)
    return {int(r['frame_idx']):r for r in rows}

def sync_times(rows):
    """Per-camera {frame_idx: seconds} used for syncing, and the basis name.

    host_time_s is when a frame reached the PC; USB delivery delay swings from ~60ms to over 1s, so
    frames exposed at the same moment can differ by hundreds of ms there (and get dropped or mis-paired).
    hw_timestamp_ms (global time) follows the camera's exposure clock, so all four cameras are compared
    on it when every camera has valid, non-decreasing values in the PC clock domain."""
    hw = []
    for rs in rows:
        t = {}
        for i, r in rs.items():
            try:
                v = float(r.get('hw_timestamp_ms') or 'nan') / 1000.0
            except ValueError:
                v = float('nan')
            if not math.isfinite(v) or abs(v - float(r['host_time_s'])) > HW_MAX_HOST_OFFSET_S:
                t = None
                break
            t[i] = v
        if t is None:
            break
        ordered = [t[i] for i in sorted(t)]
        if any(b < a for a, b in zip(ordered, ordered[1:])):
            break
        hw.append(t)
    if rows and len(hw) == len(rows):
        return hw, 'hw_timestamp_ms'
    return [{i: float(r['host_time_s']) for i, r in rs.items()} for rs in rows], 'host_time_s'


def frame_stride(frame_times, fps):
    """How many synced frames to step over so the capture is exported at `fps`.

    The cameras record at ~30fps. A lower target (15) keeps every 2nd synced
    frame; the capture rate is measured from the reference timeline (median
    frame gap), so a target equal to the capture rate keeps every frame.
    """
    gaps = sorted(b - a for a, b in zip(frame_times, frame_times[1:]) if b > a)
    if not gaps or not fps or fps <= 0:
        return 1
    return max(1, round(1.0 / gaps[len(gaps) // 2] / fps))


def sync_frames(cam_ts, fps, tol_s):
    """Align cameras using the reference camera's real frame timeline.

    Instead of resampling onto an artificial uniform grid (which drops ~half the
    frames when a fixed grid drifts against ~30fps cameras), use the actual frame
    times of the reference camera (the one with the most frames inside the
    overlap window) as the timeline, and snap every other camera to its nearest
    frame. This preserves full framerate over the whole overlap window.
    When `fps` is lower than the capture rate, every k-th synced frame is kept
    (see frame_stride), so 30fps captures are processed at e.g. 15fps.

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
    step = frame_stride(frame_times, fps)
    return synced[::step], frame_times[::step]


def source_mapping(src,cams,tol_s=DEFAULT_TOL_S,fps=30):
    rows=[read_rows(src,c) for c in cams]
    times,basis=sync_times(rows)
    use_hw=basis=='hw_timestamp_ms'
    picks,anchors=sync_frames(times,None,tol_s)
    step=frame_stride(anchors,fps)
    picks,anchors=picks[::step],anchors[::step]
    lo=max(min(t.values()) for t in times);hi=min(max(t.values()) for t in times)
    ref=max(range(len(times)),key=lambda k:sum(lo-1e-9<=t<=hi+1e-9 for t in times[k].values()))
    seen=[set() for _ in cams];frames=[]
    for f,(indices,t) in enumerate(zip(picks,anchors)):
        views=[]
        for c,i in enumerate(indices):
            r=rows[c][i];host=float(r['host_time_s']);hw=r.get('hw_timestamp_ms')
            views.append({'camera':c,'raw_camera':cams[c],'raw_idx':i,'host_time_s':host,'hw_timestamp_ms':float(hw) if hw else None,'residual_ms':(times[c][i]-t)*1000,'duplicate_source':i in seen[c]})
            seen[c].add(i)
        frames.append({'output_idx':f,'pretrim_idx':f,'pretrim_frame':f,'frame':f,'reference_time_s':t,'reference_host_s':t,'raw_indices':list(indices),'host_ms':[v['host_time_s']*1000 for v in views],'hardware_ms':[v['hw_timestamp_ms'] for v in views],'cameras':views})
    out={'schema_version':1,'raw_indices':[list(ids) for ids in picks],'hardware_common_clock_verified':False,'policy':POLICY,'sync_tol_ms':tol_s*1000,'reference_camera':ref,'timestamp_basis':'host_time_s','clock_domain':'unknown','exposure_semantics':'unknown; host timestamps do not certify simultaneous exposures','overlap_start':lo,'overlap_end':hi,'frames':frames}
    # Camera-clock mappings say so; host-time mappings stay byte-identical to earlier runs.
    if use_hw:out.update(policy=HW_POLICY,timestamp_basis='hw_timestamp_ms',clock_domain='realsense_global_time',exposure_semantics='camera frame timestamps (global time); cameras are not hardware-triggered, so exposures can differ by up to half a frame')
    # Recorded only when frames were dropped, so full-rate mappings stay byte-identical to earlier runs.
    if step>1:out.update(target_fps=float(fps),frame_stride=step)
    return out

def trim_mapping(mapping,ts,te,n):
    if len(mapping['frames'])!=n or ts<0 or te<0 or ts+te>=n:raise ValueError('Invalid source mapping/trim')
    out=dict(mapping);out['frames']=[dict(row,output_idx=i,frame=i) for i,row in enumerate(mapping['frames'][ts:n-te])]
    out['raw_indices']=[r['raw_indices'] for r in out['frames']]
    out['trim_history']=mapping.get('trim_history',[])+[{'before':n,'start':ts,'end':te}]
    return out
