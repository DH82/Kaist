"""Trim LEADING/TRAILING exposure-bad frames.

Some captures start (auto-exposure warmup) or end with frames washed to white / crushed to
black, where the object is unidentifiable. Those frames poison SAM, CoTracker and recon.
This detects the bad run at each END of the clip (per camera), takes a COMMON trim across
all cameras (so frame indices stay synchronized between views), and renumbers color/ +
depth/ + mask/ consistently, then re-encodes color/{cam}.mp4 and updates frame_num.

Middle-of-clip bad frames CANNOT be trimmed (that would break temporal continuity) and are
left untouched — only reported.

Two uses:
  * one-time on an already-segmented case (masks exist -> trimmed too);
  * in the pipeline BEFORE SAM (no mask/ yet -> only color+depth trimmed).

Raw cam_N_* capture files are NEVER touched (they stay as the full-length original archive).

Usage (phystwin env):
  python data_process/trim_exposure.py --case <dir>           # detect + print only
  python data_process/trim_exposure.py --case <dir> --apply   # apply
"""
import os
import glob
import json
import argparse

import cv2
import numpy as np

# a frame whose mean brightness is within [LO*median, HI*median] of its camera's median
# exposure is "normal"; outside that band it is over/under-exposed. (Same deadzone the SAM
# editor's exposure-normalization uses, so the two agree on what counts as "bad".)
LO, HI = 0.71, 1.39


def _cam_means(case, cam):
    mp4 = f"{case}/color/{cam}.mp4"
    if not os.path.exists(mp4):
        return None
    cap = cv2.VideoCapture(mp4)
    m = []
    while True:
        ok, im = cap.read()
        if not ok:
            break
        m.append(float(im.mean()))
    cap.release()
    return np.array(m)


def _runs(bad):
    """Leading run, trailing run, and any bad frames strictly in the middle."""
    lead = 0
    for v in bad:
        if v:
            lead += 1
        else:
            break
    trail = 0
    for v in bad[::-1]:
        if v:
            trail += 1
        else:
            break
    mids = [int(i) for i in np.where(bad)[0] if lead <= i < len(bad) - trail]
    return lead, trail, mids


def detect_trim(case, cams=(0, 1, 2, 3)):
    """(trim_start, trim_end, middle_bad_frames) common across cameras. middle_bad only
    lists bad frames that SURVIVE the common [trim_start, N-1-trim_end] cut (i.e. genuinely
    unfixable interior frames) — per-camera bad frames that fall inside the common trim are
    removed, not reported."""
    ts = te = 0
    per_cam_bad = []
    N = 0
    for cam in cams:
        m = _cam_means(case, cam)
        if m is None or len(m) == 0:
            continue
        N = max(N, len(m))
        tgt = float(np.median(m))
        bad = (m < LO * tgt) | (m > HI * tgt)
        lead, trail, _ = _runs(bad)
        ts, te = max(ts, lead), max(te, trail)
        per_cam_bad.append(bad)
    last_keep = N - 1 - te
    mids = sorted({int(i) for bad in per_cam_bad for i in np.where(bad)[0]
                   if ts <= i <= last_keep})
    return ts, te, mids


def _renumber_dir(d, ext, ts, last_keep):
    """Keep {f}{ext} for f in [ts, last_keep] renumbered f->f-ts; delete the rest.
    Sparse-safe (masks skip empty frames) and collision-free (ascending shift-down)."""
    n = len(ext)
    files = sorted(int(os.path.basename(p)[:-n]) for p in glob.glob(f"{d}/*{ext}")
                   if os.path.basename(p)[:-n].lstrip("-").isdigit())
    for f in files:                       # ascending: target f-ts is already vacated
        src = f"{d}/{f}{ext}"
        if f < ts or f > last_keep:
            os.remove(src)
        elif ts:
            os.rename(src, f"{d}/{f - ts}{ext}")


def _rebuild_mp4(case, cam):
    mp4 = f"{case}/color/{cam}.mp4"
    cap = cv2.VideoCapture(mp4)
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    cap.release()
    pngs = sorted(int(os.path.basename(p)[:-4]) for p in glob.glob(f"{case}/color/{cam}/*.png")
                  if os.path.basename(p)[:-4].isdigit())
    if not pngs:
        return
    h, w = cv2.imread(f"{case}/color/{cam}/{pngs[0]}.png").shape[:2]
    vw = cv2.VideoWriter(mp4, cv2.VideoWriter_fourcc(*"mp4v"), fps, (w, h))
    for f in pngs:
        vw.write(cv2.imread(f"{case}/color/{cam}/{f}.png"))
    vw.release()


def apply_trim(case, ts, te):
    """Trim the case in place. Returns the new frame_num."""
    meta_p = f"{case}/metadata.json"
    meta = json.load(open(meta_p))
    N = meta["frame_num"]
    from sync_mapping import trim_mapping
    mapping_p = f'{case}/source_frames.json'
    mapping = trim_mapping(json.load(open(mapping_p)), ts, te, N)
    if ts == 0 and te == 0:
        return N
    last_keep = N - 1 - te
    cams = sorted(int(os.path.basename(p)[:-4]) for p in glob.glob(f"{case}/color/*.mp4"))
    for cam in cams:
        _renumber_dir(f"{case}/color/{cam}", ".png", ts, last_keep)
        _renumber_dir(f"{case}/depth/{cam}", ".npy", ts, last_keep)
        for od in glob.glob(f"{case}/mask/{cam}/*"):      # mask/{cam}/{oid}/ (may not exist pre-SAM)
            if os.path.isdir(od):
                _renumber_dir(od, ".png", ts, last_keep)
        _rebuild_mp4(case, cam)
    json.dump(mapping, open(mapping_p, "w"), indent=2)
    meta["frame_num"] = N - ts - te
    json.dump(meta, open(meta_p, "w"), indent=2)
    return N - ts - te


def trim_case(case, apply=False, verbose=True):
    ts, te, mids = detect_trim(case)
    N = json.load(open(f"{case}/metadata.json"))["frame_num"]
    if verbose:
        print(f"{os.path.basename(case)}: N={N} trim_start={ts} trim_end={te} "
              f"-> new_N={N - ts - te}  mid_bad={mids[:10]}"
              f"{'  (MIDDLE bad frames left as-is)' if mids else ''}", flush=True)
    if apply:
        newN = apply_trim(case, ts, te)
        if verbose:
            print(f"  applied -> frame_num={newN}", flush=True)
        return newN
    return N - ts - te


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--case", required=True)
    ap.add_argument("--apply", action="store_true", help="apply (default: detect + print only)")
    args = ap.parse_args()
    trim_case(args.case, apply=args.apply)


if __name__ == "__main__":
    main()
