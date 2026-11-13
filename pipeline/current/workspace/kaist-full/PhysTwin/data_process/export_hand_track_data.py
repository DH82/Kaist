# Build hand_track_data.pkl: the wrist-down HAND tracked points lifted to 3D, with
# the SAME processing as PhysTwin's object (data_process_track) so it has identical
# structure/quality:
#   - drop track samples that leave the hand mask in a frame (removes forearm drift),
#   - lift to 3D via the per-frame pcd (so colors are the original RGB),
#   - concatenate all cameras into one (T, N, 3),
#   - neighbour-motion-consistency filter -> motions_valid.
#
# Output  hand_track_data.pkl  (mirrors track_process_data's object_* fields):
#   hand_points (T,N,3) f64, hand_colors (T,N,3) f64,
#   hand_visibilities (T,N) bool, hand_motions_valid (T,N) bool
#
# Usage (phystwin env, after seg + cotracker_hand + data_process_pcd/mask):
#   python data_process/export_hand_track_data.py --base_path ./data/different_types --case_name ep_test

import os
import glob
import json
import pickle
import argparse

import numpy as np
import open3d as o3d


def filter_motion(points, colors, vis, neighbor_dist=0.01):
    from motion_contract import frozen_motion_filter
    return frozen_motion_filter(points, vis, neighbor_dist)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base_path", default="./data/different_types")
    ap.add_argument("--case_name", required=True)
    ap.add_argument("--hand_dir", default="cotracker_hand")
    args = ap.parse_args()

    case = f"{args.base_path}/{args.case_name}"
    pcd_path = f"{case}/pcd"
    num_cam = len(glob.glob(f"{case}/mask/mask_info_*.json"))
    frame_num = len(glob.glob(f"{pcd_path}/*.npz"))
    processed = pickle.load(open(f"{case}/mask/processed_masks.pkl", "rb"))

    all_pts, all_col, all_vis = [], [], []
    for cam in range(num_cam):
        hp = f"{case}/{args.hand_dir}/{cam}.npz"
        if not os.path.isfile(hp):
            continue
        d = np.load(hp)
        tracks = np.round(d["tracks"]).astype(int)            # [y,x]
        vis = d["visibility"].copy()
        T, N = vis.shape[0], vis.shape[1]
        # drop samples that leave the hand (controller) mask -> removes forearm drift
        for f in range(T):
            m = processed[f][cam].get("controller")
            if m is None:
                vis[f] = False; continue
            on = np.where(vis[f])[0]
            for j in on:
                y, x = tracks[f, j, 0], tracks[f, j, 1]
                if not (0 <= y < m.shape[0] and 0 <= x < m.shape[1] and m[y, x]):
                    vis[f, j] = False
        # lift to 3D via per-frame pcd (original RGB)
        pts = np.zeros((T, N, 3)); col = np.zeros((T, N, 3))
        for f in range(T):
            z = np.load(f"{pcd_path}/{f}.npz")
            on = np.where(vis[f])[0]
            pts[f, on] = z["points"][cam][tracks[f, on, 0], tracks[f, on, 1]]
            col[f, on] = z["colors"][cam][tracks[f, on, 0], tracks[f, on, 1]]
        all_pts.append(pts); all_col.append(col); all_vis.append(vis)

    hand_points = np.concatenate(all_pts, 1)
    hand_colors = np.concatenate(all_col, 1)
    hand_vis = np.concatenate(all_vis, 1)
    print(f"  hand points combined: {hand_points.shape}  (motion filtering...)")
    hand_mv = filter_motion(hand_points, hand_colors, hand_vis)

    from motion_contract import SCHEMA
    out = {"motion_schema": dict(SCHEMA), "hand_points": hand_points, "hand_colors": hand_colors,
           "hand_visibilities": hand_vis, "hand_motions_valid": hand_mv}
    from motion_contract import validate_motion
    validate_motion(out, "hand")
    from export_provenance import atomic_pickle
    atomic_pickle(f"{case}/hand_track_data.pkl", out)
    print(f"[done] {case}/hand_track_data.pkl  "
          f"points {hand_points.shape}  avg visible/frame {hand_vis.sum()/hand_vis.shape[0]:.0f}")


if __name__ == "__main__":
    main()
