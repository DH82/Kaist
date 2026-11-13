# Scalable COVERAGE-GAP dense tracking of an OBJECT with CoTracker3 (online).
#
# Naive PhysTwin seeding samples query points only from the FRAME-0 object mask,
# so object parts occluded at frame 0 (revealed later) are never tracked.
#
# The signal we actually want is NOT "how did the seg area/boundary change"
# (those change for hand OCCLUSION too, so they confuse occlusion with reveal),
# but "is there object surface that NO tracked point is currently covering?".
# That is the COVERAGE GAP, and it is the direct measure of a reveal:
#   - hand covering object: those px leave the object mask -> nothing to cover ->
#     no gap -> no seed (no over-seeding on occlusion).
#   - hand leaves / a frame-0-occluded part appears: that surface re-enters the
#     mask with no tracked point on it -> gap -> seed ONLY those px.
#   - gradual reveal over [t, t+b]: a little uncovered area appears each frame ->
#     a little seeding each time -> GRADUAL tracking.
# It also minimizes compute: we only ever seed genuinely-uncovered pixels
# (no whole-mask re-sampling + dedup waste).
#
# Algorithm (single forward scan, memory-bounded online model => scales to long
# videos): seed first-appearance frame -> track forward. Then for each frame f
# (after a cooldown), gap = mask[f] & ~dilate(coverage by tracked pts visible @ f).
# If |gap| exceeds a threshold, sample query pts from the GAP at f and track them
# forward; merge. Repeat to the end.
#
# Output (PhysTwin-compatible):
#   cotracker/{cam}.npz   -> tracks (T,N,2)[y,x], visibility (T,N), query_frame (N,)
#   cotracker/{cam}_30fps.mp4
#
# Usage (phystwin env):
#   python data_process/track_object.py --base_path ./data/different_types \
#       --case_name ep_test --obj_id 0

import os
import sys
import glob
import argparse

import numpy as np
import torch
import cv2
import imageio.v3 as iio


def obj_mask(case, cam, obj_id, frame):
    mp = f"{case}/mask/{cam}/{obj_id}/{frame}.png"
    return (cv2.imread(mp, 0) > 127) if os.path.isfile(mp) else None


def sample_xy(mask_bool, spacing, max_pts, rng):
    """Sample ~1 point per spacing^2 px from the mask region (capped at max_pts)."""
    ys, xs = np.where(mask_bool)
    if len(xs) == 0:
        return np.zeros((0, 2), np.float32)
    n = max(1, int(len(xs) / (spacing * spacing)))
    n = min(n, max_pts, len(xs))
    if len(xs) > n:
        idx = rng.choice(len(xs), n, replace=False)
        xs, ys = xs[idx], ys[idx]
    return np.stack([xs, ys], 1).astype(np.float32)  # (x,y)


def online_forward(model, video, queries_xy, device):
    """Track queries (seeded at local frame 0) forward over `video` (1,T,3,H,W)."""
    q = np.concatenate([np.zeros((len(queries_xy), 1)), queries_xy], 1)  # (frame=0,x,y)
    q = torch.tensor(q, dtype=torch.float32, device=device)[None]
    model(video_chunk=video, is_first_step=True, queries=q)
    pt = pv = None
    for ind in range(0, max(1, video.shape[1] - model.step), model.step):
        pt, pv = model(video_chunk=video[:, ind:ind + model.step * 2])
    if pt is None:                                   # clip shorter than one window
        pt, pv = model(video_chunk=video)
    return pt[0].cpu().numpy(), pv[0].cpu().numpy()  # (T,n,2)x,y ; (T,n)


def coverage_mask(tr, vs, qf, f, H, W, dilate):
    """Binary mask of where tracked points (born by f and visible at f) sit, dilated."""
    cov = np.zeros((H, W), np.uint8)
    active = (qf <= f) & vs[f]
    if active.any():
        xi = np.clip(tr[f, active, 0].astype(int), 0, W - 1)
        yi = np.clip(tr[f, active, 1].astype(int), 0, H - 1)
        cov[yi, xi] = 1
        k = 2 * dilate + 1
        cov = cv2.dilate(cov, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k)))
    return cov.astype(bool)


def render_track_video(out_mp4, case, cam, G_tr, G_vs, G_qf, T, H, W):
    """Overlay tracked points on the frames, drawing ONLY the points that are VISIBLE at
    that frame (occluded points are omitted). Previously every born point was drawn
    uniform green, which hid occlusion entirely (visibility mean ~0.32 -> most drawn
    points were actually occluded). G_tr is [x,y] here (the npz stores [y,x])."""
    vw = cv2.VideoWriter(out_mp4, cv2.VideoWriter_fourcc(*"mp4v"), 30, (W, H))
    for f in range(T):
        img = cv2.imread(f"{case}/color/{cam}/{f}.png")
        if img is None:
            continue
        born = G_qf <= f
        vis = G_vs[f]
        xi = np.clip(G_tr[f, :, 0].astype(int), 0, W - 1)
        yi = np.clip(G_tr[f, :, 1].astype(int), 0, H - 1)
        for i in np.where(born & ~vis)[0]:                   # occluded first (drawn under)
            cv2.circle(img, (xi[i], yi[i]), 2, (0, 0, 255), -1)     # RED = occluded (BGR)
        for i in np.where(born & vis)[0]:
            cv2.circle(img, (xi[i], yi[i]), 2, (0, 255, 0), -1)     # green = visible
        cv2.putText(img, f"cam{cam} f{f}  green=visible red=occluded  "
                    f"vis={int((born & vis).sum())}/{int(born.sum())}",
                    (8, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2)
        vw.write(img)
    vw.release()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base_path", default="./data/different_types")
    ap.add_argument("--case_name", required=True)
    ap.add_argument("--cotracker_repo", default=os.environ.get("KP_COTRACKER_REPO"))
    ap.add_argument("--obj_id", type=int, default=0)
    ap.add_argument("--out_dir", default="cotracker", help="output subdir under case")
    ap.add_argument("--cameras", type=int, nargs="*", default=None)
    ap.add_argument("--seed_spacing", type=float, default=3.0,
                    help="query point spacing in px (~1 pt per spacing^2)")
    ap.add_argument("--max_pts_per_seed", type=int, default=4000)
    ap.add_argument("--cov_dilate", type=int, default=7,
                    help="px: dilate tracked-point coverage so spacing gaps don't read as holes")
    ap.add_argument("--gap_min_area", type=int, default=600,
                    help="px: uncovered object area above which to seed a new (revealed) region")
    ap.add_argument("--cooldown", type=int, default=4, help="min frames between seeds")
    ap.add_argument("--min_track_frames", type=int, default=8,
                    help="skip seeding a reveal with fewer remaining frames than this")
    ap.add_argument("--no_viz", action="store_true")
    args = ap.parse_args()

    case = f"{args.base_path}/{args.case_name}"
    device = "cuda"
    os.makedirs(f"{case}/{args.out_dir}", exist_ok=True)
    cams = args.cameras if args.cameras is not None else sorted(
        int(os.path.basename(p)) for p in glob.glob(f"{case}/depth/*") if os.path.basename(p).isdigit())
    print(f"[track_object] case={args.case_name} cams={cams} obj_id={args.obj_id} (coverage-gap seeding)")

    sys.path.insert(0, args.cotracker_repo)
    model = torch.hub.load(args.cotracker_repo, "cotracker3_online", source="local").to(device)

    for cam in cams:
        mp4 = f"{case}/color/{cam}.mp4"
        if not os.path.isfile(mp4):
            print(f"  [skip] cam {cam}: no mp4"); continue
        frames = iio.imread(mp4, plugin="FFMPEG")
        T, H, W = frames.shape[:3]
        masks = {f: obj_mask(case, cam, args.obj_id, f) for f in range(T)}
        appear = [f for f in range(T) if masks[f] is not None and masks[f].any()]
        if not appear:
            print(f"  [skip] cam {cam}: object never segmented"); continue
        f0 = appear[0]

        video = torch.tensor(frames).permute(0, 3, 1, 2)[None].float().to(device)
        rng = np.random.default_rng(0)

        # merged trajectory buffers, grown one seed at a time
        G_tr = np.zeros((T, 0, 2), np.float32)
        G_vs = np.zeros((T, 0), bool)
        G_qf = np.zeros((0,), np.int32)

        def add_seed(fs, region):
            nonlocal G_tr, G_vs, G_qf
            if T - fs < args.min_track_frames:
                return 0
            q = sample_xy(region, args.seed_spacing, args.max_pts_per_seed, rng)
            if len(q) == 0:
                return 0
            pt, pv = online_forward(model, video[:, fs:], q, device)
            n = pt.shape[1]
            g_tr = np.zeros((T, n, 2), np.float32)
            g_vs = np.zeros((T, n), bool)
            g_tr[fs:] = pt
            g_vs[fs:] = pv > 0.5
            G_tr = np.concatenate([G_tr, g_tr], 1)
            G_vs = np.concatenate([G_vs, g_vs], 1)
            G_qf = np.concatenate([G_qf, np.full(n, fs, np.int32)])
            torch.cuda.empty_cache()
            return n

        n0 = add_seed(f0, masks[f0])
        seeds = [(f0, n0)]
        last = f0
        for f in range(f0 + 1, T):
            if masks[f] is None or not masks[f].any():
                continue
            if f - last < args.cooldown:
                continue
            cov = coverage_mask(G_tr, G_vs, G_qf, f, H, W, args.cov_dilate)
            gap = masks[f] & ~cov
            if int(gap.sum()) >= args.gap_min_area:
                n = add_seed(f, gap)
                if n > 0:
                    seeds.append((f, n)); last = f

        print(f"  [cam {cam}] seeds (frame, npts): {seeds}  -> {G_tr.shape[1]} tracks total")

        np.savez(f"{case}/{args.out_dir}/{cam}.npz",
                 tracks=G_tr[:, :, ::-1], visibility=G_vs, query_frame=G_qf)  # [y,x] for PhysTwin
        print(f"  [cam {cam}] saved -> cotracker/{cam}.npz")

        if not args.no_viz:
            render_track_video(f"{case}/{args.out_dir}/{cam}_30fps.mp4", case, cam,
                               G_tr, G_vs, G_qf, T, H, W)
            print(f"  [cam {cam}] viz -> cotracker/{cam}_30fps.mp4")

    print(f"[done] {case}/{args.out_dir}/")


if __name__ == "__main__":
    main()
