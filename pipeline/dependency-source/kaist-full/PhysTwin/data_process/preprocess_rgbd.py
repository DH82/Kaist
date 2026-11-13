# Convert a multi-camera RealSense capture (ep_* directory) into the input
# format expected by PhysTwin's data processing pipeline.
#
# Source layout (per episode):
#   cam_{n}.mp4                       RGB video           (848x480, 30fps)
#   cam_{n}_depth/frame_NNNNNN.npy    depth, uint16 mm    (H, W)
#   cam_{n}_intrinsics.json           color intrinsics + serial + depth_scale
#   cam_{n}_timestamps.csv            frame_idx, hw_timestamp_ms, host_time_s
#   extrinsics.json                   charuco R,T per camera (cam_1 == world origin)
#
# PhysTwin case layout (output):
#   color/{i}.mp4                     synced RGB video per camera
#   color/{i}/{f}.png                 synced RGB frames
#   depth/{i}/{f}.npy                 synced depth, uint16 mm
#   metadata.json                     {intrinsics, WH, frame_num, serial_numbers}
#   calibrate.pkl                     list of 4x4 camera-to-world matrices
#
# Cameras are NOT hardware-synced, so frames are aligned across cameras by
# host wall-clock timestamps onto a common fps grid before export.

import os
import re
import csv
import json
import pickle
import glob
import argparse

import numpy as np
import cv2


def existDir(p):
    if not os.path.exists(p):
        os.makedirs(p)


def discover_cameras(src):
    cams = []
    for p in sorted(glob.glob(os.path.join(src, "cam_*_intrinsics.json"))):
        name = os.path.basename(p)[: -len("_intrinsics.json")]  # cam_1
        cams.append(name)
    # sort cam_1, cam_2, ... numerically
    cams.sort(key=lambda c: int(c.split("_")[1]))
    return cams


def load_intrinsic(src, cam):
    with open(os.path.join(src, f"{cam}_intrinsics.json"), "r") as f:
        d = json.load(f)
    ci = d["color_intrinsics"]
    K = np.array(
        [[ci["fx"], 0.0, ci["ppx"]], [0.0, ci["fy"], ci["ppy"]], [0.0, 0.0, 1.0]],
        dtype=np.float64,
    )
    WH = [int(ci["width"]), int(ci["height"])]
    serial = d.get("serial", cam)
    return K, WH, serial


def load_extrinsics(src):
    with open(os.path.join(src, "extrinsics.json"), "r") as f:
        d = json.load(f)
    out = {}
    for cam, t in d["transforms"].items():
        R = np.array(t["R"], dtype=np.float64).reshape(3, 3)
        T = np.array(t["T"], dtype=np.float64).reshape(3, 1)
        out[cam] = (R, T)
    return out


def make_RT(R, T):
    M = np.eye(4, dtype=np.float64)
    M[:3, :3] = R
    M[:3, 3:4] = T
    return M


def w2c_to_c2w(R, T):
    # X_cam = R X_world + T  ->  X_world = R^T (X_cam - T)
    Rt = R.T
    c2w = np.eye(4, dtype=np.float64)
    c2w[:3, :3] = Rt
    c2w[:3, 3:4] = -Rt @ T
    return c2w


def load_timestamps(src, cam):
    # returns dict local_frame_idx -> host_time_s
    ts = {}
    with open(os.path.join(src, f"{cam}_timestamps.csv"), "r") as f:
        reader = csv.DictReader(f)
        for row in reader:
            ts[int(row["frame_idx"])] = float(row["host_time_s"])
    return ts


def depth_frames(src, cam):
    # returns dict local_frame_idx -> path
    out = {}
    for p in glob.glob(os.path.join(src, f"{cam}_depth", "frame_*.npy")):
        m = re.search(r"frame_(\d+)\.npy$", p)
        if m:
            out[int(m.group(1))] = p
    return out


from sync_mapping import sync_frames, source_mapping

def ransac_plane(pts, thresh=0.01, iters=400, seed=0):
    """Lightweight numpy plane RANSAC -> (unit normal, d, inlier_ratio); plane: n.x+d=0."""
    rng = np.random.default_rng(seed)
    n_pts = len(pts)
    best_n, best_d, best_in = None, None, -1
    for _ in range(iters):
        p = pts[rng.choice(n_pts, 3, replace=False)]
        nrm = np.cross(p[1] - p[0], p[2] - p[0])
        nn = np.linalg.norm(nrm)
        if nn < 1e-9:
            continue
        nrm = nrm / nn
        d = -nrm @ p[0]
        inl = int((np.abs(pts @ nrm + d) < thresh).sum())
        if inl > best_in:
            best_in, best_n, best_d = inl, nrm, d
    # refine on inliers via SVD
    inliers = pts[np.abs(pts @ best_n + best_d) < thresh]
    c = inliers.mean(0)
    _, _, vt = np.linalg.svd(inliers - c, full_matrices=False)
    n = vt[-1] / np.linalg.norm(vt[-1])
    return n, float(-n @ c), best_in / n_pts


def reorient_world_frame(c2ws, scene_pts, thresh=0.01):
    """Rigidly reorient the world so the dominant (table) plane -> z=0 and the
    camera/object side -> z<=0 (PhysTwin convention: gravity +z, align.py and
    data_process_sample clamp z>0 to the table). Returns (M, new_c2ws, info).
    M is a pure rotation+translation (det=+1, no scale) so calibration is preserved.
    """
    if len(scene_pts) > 80000:
        scene_pts = scene_pts[np.random.default_rng(0).choice(len(scene_pts), 80000, replace=False)]
    n, d, ratio = ransac_plane(scene_pts, thresh)
    cam_centers = np.array([c[:3, 3] for c in c2ws])
    p0 = -d * n
    # new +z points AWAY from the cameras (cameras look down from above -> they
    # must end at z<0; objects resting on the table then sit at z<=0)
    nz = -n if np.mean((cam_centers - p0) @ n) > 0 else n
    aligned = abs(abs(nz[2]) - 1.0) < 0.02 and abs(d) < 0.02
    ref = np.array([1.0, 0, 0]) if abs(nz[0]) < 0.9 else np.array([0.0, 1, 0])
    nx = ref - (ref @ nz) * nz
    nx /= np.linalg.norm(nx)
    ny = np.cross(nz, nx)
    R = np.stack([nx, ny, nz], 0)
    M = np.eye(4)
    M[:3, :3] = R
    M[:3, 3] = -R @ p0
    new_c2ws = [M @ c for c in c2ws]
    # rigidity self-check (pairwise camera-center distances must be unchanged)
    def pdist(C):
        return np.linalg.norm(C[:, None] - C[None], axis=-1)
    drift = float(np.abs(pdist(cam_centers) - pdist(np.array([c[:3, 3] for c in new_c2ws]))).max())
    info = (f"table inliers={ratio*100:.0f}% normal={np.round(n,3)} "
            f"already_aligned={aligned} rigidity_drift={drift:.1e}")
    return M, new_c2ws, info


def lift_to_world(depth_mm, K, c2w, zmin=0.2, zmax=1.5):
    depth = depth_mm.astype(np.float64) / 1000.0
    H, W = depth.shape
    xs, ys = np.meshgrid(np.arange(W), np.arange(H))
    pts = np.stack([xs.reshape(-1), ys.reshape(-1), np.ones(W * H)], axis=1)
    pts = pts * depth.reshape(-1, 1)
    cam = pts @ np.linalg.inv(K).T
    z = cam[:, 2]
    mask = (z > zmin) & (z < zmax)
    cam_h = np.hstack([cam, np.ones((cam.shape[0], 1))])
    world = (c2w @ cam_h.T).T[:, :3]
    return world[mask]


def check_convention(src, cams, Ks, extr, sample_depth_paths, conv):
    """Verify the extrinsic convention instead of guessing it.

    The producer is our own code, so the convention is FIXED, not something to
    infer per capture. Exo_video_data/calibration.py builds `transforms` with
    cv2.stereoCalibrate(ref, cam) -> R, T, whose OpenCV meaning is
    X_cam = R @ X_ref + T, and it stores the reference camera as identity
    (transforms[cam_1] = I, 0), i.e. the reference camera IS the world origin.
    Therefore X_cam = R @ X_world + T  ->  the file is **w2c**.

    Guessing it was the bug: the old validate_convention() compared the centroid
    of each camera's whole lifted cloud, which mostly reflects where that camera
    happens to point. On the 2026-08-26 rig it scored 0.11602 (w2c) vs 0.11546
    (c2w) -- 0.5% apart -- picked c2w on that noise, and every downstream 3D
    product was built on four cameras disagreeing by ~15.7 deg (object and hand
    appeared as several overlapping copies in obj_hand_recon.mp4).

    This function no longer chooses; it measures the dominant-plane normal
    agreement under `conv` and warns if the result looks wrong. Same rig:
    w2c = 2.27 deg (correct), c2w = 15.67 deg.
    """
    normals = []
    for cam, K in zip(cams, Ks):
        R, T = extr[cam]
        c2w = w2c_to_c2w(R, T) if conv == "w2c" else make_RT(R, T)
        world = lift_to_world(np.load(sample_depth_paths[cam]), K, c2w)
        if world.shape[0] < 500:
            continue
        idx = np.random.default_rng(0).choice(world.shape[0], min(8000, world.shape[0]),
                                              replace=False)
        n, _, _ = ransac_plane(world[idx], iters=120)
        normals.append(n if n[2] >= 0 else -n)     # plane normal sign is arbitrary
    if len(normals) < 2:
        return float("nan")
    normals = np.array(normals)
    worst = float(max(
        np.degrees(np.arccos(np.clip(normals[i] @ normals[j], -1.0, 1.0)))
        for i in range(len(normals)) for j in range(i + 1, len(normals))
    ))
    print(f"[extrinsic convention] {conv}: cameras disagree on the dominant plane by "
          f"{worst:.2f} deg (good < 3)")
    if worst > 3.0:
        print(f"[extrinsic convention] WARNING: {worst:.2f} deg is above the 3 deg "
              f"guideline; check the extrinsics before trusting this case.")
    return worst


# Above this the cameras no longer describe one scene: the fused cloud shows the object
# as several overlapping copies. 2026-08-26 reference values -- correct convention 0.6-0.8,
# wrong convention 14.5, the 2026-07-21 rig 1.16. 5.0 sits well clear of both sides.
MAX_PLANE_DISAGREE_DEG = 5.0


def _unused_validate_convention(src, cams, Ks, extr, sample_depth_paths):
    """Pick w2c vs c2w by whether the cameras agree on the DOMINANT PLANE's normal.

    The previous test compared the centroid of each camera's whole lifted cloud.
    Each camera frames a different part of the room, so its centroid sits somewhere
    else no matter which convention is used, and the score barely responds: on the
    2026-08-26 rig the two conventions scored 0.11602 (w2c) vs 0.11546 (c2w), i.e.
    0.5% apart. c2w won on that noise and every downstream 3D product was built on
    four cameras that disagreed by 15.7 deg -- the object and hand appeared as
    several overlapping copies in obj_hand_recon.mp4.

    A wrong convention inverts each camera's pose about a different centre, so it
    tilts each lifted cloud differently. Fitting the table plane per camera and
    taking the worst pairwise angle between normals therefore separates the two
    cleanly: on that same rig c2w = 15.67 deg vs w2c = 2.27 deg.

    Returns (best, results) where results maps convention -> worst-pair angle in
    degrees (lower is better), so the caller's printout stays meaningful.
    """
    results = {}
    for conv in ("w2c", "c2w"):
        normals = []
        for cam, K in zip(cams, Ks):
            R, T = extr[cam]
            c2w = w2c_to_c2w(R, T) if conv == "w2c" else make_RT(R, T)
            world = lift_to_world(np.load(sample_depth_paths[cam]), K, c2w)
            if world.shape[0] < 500:
                continue
            if world.shape[0] > 40000:
                idx = np.random.default_rng(0).choice(world.shape[0], 40000, replace=False)
                world = world[idx]
            n, _, _ = ransac_plane(world)
            normals.append(n if n[2] >= 0 else -n)   # sign is arbitrary; fix it
        if len(normals) < 2:
            results[conv] = np.inf
            continue
        normals = np.array(normals)
        results[conv] = float(max(
            np.degrees(np.arccos(np.clip(normals[i] @ normals[j], -1.0, 1.0)))
            for i in range(len(normals)) for j in range(i + 1, len(normals))
        ))
    best = min(results, key=results.get)
    other = min((k for k in results if k != best), key=results.get, default=None)
    if other is not None and results[best] > 0.5 * results[other]:
        print(f"[extrinsic convention] WARNING: {best}={results[best]:.2f}deg vs "
              f"{other}={results[other]:.2f}deg -- the two conventions are not clearly "
              f"separated, so the pick may be wrong. Inspect the extrinsics.")
    if results[best] > 5.0:
        print(f"[extrinsic convention] WARNING: best convention still leaves the cameras "
              f"{results[best]:.2f}deg apart on the dominant plane. Expect multi-layered "
              f"fusion; re-run the extrinsic calibration before trusting this case.")
    return best, results


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True, help="ep_* capture directory")
    ap.add_argument("--out", default="./data/different_types", help="PhysTwin base_path")
    ap.add_argument("--case_name", required=True)
    ap.add_argument("--fps", type=float, default=30.0)
    ap.add_argument("--sync_tol_ms", type=float, default=25.0)
    ap.add_argument("--convention", choices=["auto", "w2c", "c2w"], default="auto")
    ap.add_argument("--allow_bad_extrinsics", action="store_true",
                    help="proceed even if the cameras disagree on the dominant plane by more "
                         f"than the limit (see MAX_PLANE_DISAGREE_DEG); the 3D output will be wrong")
    ap.add_argument("--no_reorient", action="store_true",
                    help="skip auto table->z=0 world reorientation (PhysTwin convention)")
    args = ap.parse_args()

    src = args.src
    case_dir = os.path.join(args.out, args.case_name)
    existDir(case_dir)

    cams = discover_cameras(src)
    print(f"[cameras] {cams}")

    Ks, WHs, serials = [], [], []
    for cam in cams:
        K, WH, serial = load_intrinsic(src, cam)
        Ks.append(K)
        WHs.append(WH)
        serials.append(serial)
    WH = WHs[0]
    assert all(w == WH for w in WHs), f"camera resolutions differ: {WHs}"

    extr = load_extrinsics(src)
    cam_ts = [load_timestamps(src, cam) for cam in cams]
    cam_depths = [depth_frames(src, cam) for cam in cams]

    # Extrinsic convention is FIXED by the producer, not detected. See
    # check_convention() for why guessing it was wrong and for the proof that
    # Exo_video_data/calibration.py writes w2c. We only verify and warn.
    sample_depth = {cam: cam_depths[i][min(cam_depths[i])] for i, cam in enumerate(cams)}
    conv = "w2c" if args.convention == "auto" else args.convention
    print(f"[extrinsic convention] using: {conv}"
          f"{' (default for our charuco calibration)' if args.convention == 'auto' else ' (forced)'}")
    disagree = check_convention(src, cams, Ks, extr, sample_depth, conv)
    # Abort rather than warn. The 2026-08-26 batch ran all 19 episodes to completion on
    # extrinsics that placed the four cameras 14.5 deg apart; nothing failed, so it only
    # surfaced days later when the fused reconstruction was looked at by eye.
    if np.isfinite(disagree) and disagree > MAX_PLANE_DISAGREE_DEG and not args.allow_bad_extrinsics:
        raise SystemExit(
            f"[extrinsic convention] ABORT: cameras disagree on the dominant plane by "
            f"{disagree:.2f} deg (limit {MAX_PLANE_DISAGREE_DEG}). The extrinsics do not "
            f"describe one scene, so every 3D product from this case would be wrong.\n"
            f"  - re-run the extrinsic calibration, or\n"
            f"  - pass --convention {'c2w' if conv == 'w2c' else 'w2c'} if this capture "
            f"really uses the other convention, or\n"
            f"  - pass --allow_bad_extrinsics to proceed anyway (not recommended)."
        )

    c2ws = []
    for cam in cams:
        R, T = extr[cam]
        c2ws.append(w2c_to_c2w(R, T) if conv == "w2c" else make_RT(R, T))

    # temporal sync
    synced, grid_times = sync_frames(cam_ts, args.fps, args.sync_tol_ms / 1000.0)
    N = len(synced)
    if not N:raise ValueError("No synchronized frames inside tolerance")
    print(f"[sync] {N} aligned frames over {grid_times[-1]-grid_times[0]:.2f}s")

    mapping = source_mapping(src, cams, args.sync_tol_ms / 1000.0, args.fps)
    with open(os.path.join(case_dir, 'source_frames.json'), 'w') as f:
        json.dump(mapping, f, indent=2)

    # Auto-reorient: charuco world is rarely z-up (tilted rig). PhysTwin downstream
    # (align.py table clamp, data_process_sample) assumes table=z=0 / object z<=0,
    # so fit the dominant (table) plane and rigidly rotate the world into that frame.
    if not args.no_reorient:
        o_mid = N // 2
        scene = [lift_to_world(np.load(cam_depths[ci][synced[o_mid][ci]]),
                               Ks[ci], c2ws[ci], zmin=0.1, zmax=3.0)
                 for ci in range(len(cams))]
        scene = np.concatenate(scene, 0)
        M, c2ws, info = reorient_world_frame(c2ws, scene)
        np.save(os.path.join(case_dir, "world_reorient_M.npy"), M)
        print(f"[reorient] {info}")

    # prepare output dirs
    existDir(os.path.join(case_dir, "color"))
    existDir(os.path.join(case_dir, "depth"))
    for i in range(len(cams)):
        existDir(os.path.join(case_dir, "color", str(i)))
        existDir(os.path.join(case_dir, "depth", str(i)))

    # open mp4 readers and grab needed frames per camera
    for ci, cam in enumerate(cams):
        cap = cv2.VideoCapture(os.path.join(src, f"{cam}.mp4"))
        # local frame idx -> LIST of output idxs (a source frame may map to >1
        # output when the reference timeline reuses a lagging camera's frame;
        # a plain dict would drop those, leaving fewer color frames than depth)
        from collections import defaultdict as _dd
        want = _dd(list)
        for o in range(N):
            want[synced[o][ci]].append(o)
        writer = cv2.VideoWriter(
            os.path.join(case_dir, "color", f"{ci}.mp4"),
            cv2.VideoWriter_fourcc(*"mp4v"),
            args.fps,
            (WH[0], WH[1]),
        )
        # we must write the mp4 in output order; buffer frames by output idx
        frames_by_out = {}
        local = 0
        while True:
            ret, frame = cap.read()
            if not ret:
                break
            if local in want:
                frame = cv2.resize(frame, (WH[0], WH[1]))
                for o in want[local]:
                    frames_by_out[o] = frame
                    cv2.imwrite(
                        os.path.join(case_dir, "color", str(ci), f"{o}.png"), frame
                    )
            local += 1
        cap.release()
        for o in range(N):
            if o in frames_by_out:
                writer.write(frames_by_out[o])
        writer.release()

        # depth
        for o in range(N):
            li = synced[o][ci]
            depth = np.load(cam_depths[ci][li])
            np.save(os.path.join(case_dir, "depth", str(ci), f"{o}.npy"), depth)
        print(f"[export] {cam} -> cam {ci}: {N} frames")

    # metadata.json
    metadata = {
        "intrinsics": [K.tolist() for K in Ks],
        "WH": WH,
        "frame_num": N,
        "serial_numbers": serials,
    }
    with open(os.path.join(case_dir, "metadata.json"), "w") as f:
        json.dump(metadata, f, indent=2)

    # calibrate.pkl
    with open(os.path.join(case_dir, "calibrate.pkl"), "wb") as f:
        pickle.dump([c2w for c2w in c2ws], f)

    print(f"[done] case written to {case_dir}")
    print(f"       cameras={len(cams)} frames={N} WH={WH}")


if __name__ == "__main__":
    main()
