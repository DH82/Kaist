# final_data_hand.pkl : the HAND as a PhysTwin-ready particle system, mirroring the
# object's final_data.pkl (voxel-deduplicated points tracked across all frames). Delivered
# SEPARATELY so KAIST can use the hand as the CONTROLLER in PhysTwin physics fitting --
# final_data.pkl's own controller_points are empty because the hand is tracked/delivered
# separately (per the "hand separate" delivery design).
#
# Same voxel dedup as data_process_sample's non-shape-prior branch, but keyed on the frame
# with the most visible hand points (the hand is absent at frame 0, unlike the object).
#
# Usage (phystwin env):
#   python data_process/export_final_hand.py --case ./data/different_types/ep_XXXX

import os
import pickle
import argparse

import numpy as np


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--case", required=True)
    ap.add_argument("--volume_sample_size", type=float, default=0.005)
    args = ap.parse_args()

    from export_provenance import tracked_hand_source, verify_tracked_hand_source
    d, src, source_provenance = tracked_hand_source(args.case)
    pts, col = d["hand_points"], d["hand_colors"]
    vis, mv = d["hand_visibilities"], d["hand_motions_valid"]

    ref = int(vis.sum(1).argmax())           # frame with most visible hand points
    p_ref = pts[ref]
    mn = p_ref[vis[ref]].min(0) if vis[ref].any() else p_ref.min(0)
    grid, idx = {}, []
    for i in range(pts.shape[1]):
        g = tuple(np.floor((p_ref[i] - mn) / args.volume_sample_size).astype(int))
        if g not in grid:
            grid[g] = 1
            idx.append(i)

    from motion_contract import SCHEMA
    out = {
        "motion_schema": dict(SCHEMA),
        "source_provenance": source_provenance,
        "hand_points": pts[:, idx, :],
        "hand_colors": col[:, idx, :],
        "hand_visibilities": vis[:, idx],
        "hand_motions_valid": mv[:, idx],
    }
    from motion_contract import validate_motion
    validate_motion(out, "hand")
    from export_provenance import atomic_pickle
    verify_tracked_hand_source(args.case, source_provenance)
    atomic_pickle(f"{args.case}/final_data_hand.pkl", out)
    print(f"[export_final_hand] {pts.shape[1]} -> {len(idx)} hand particles x {pts.shape[0]} "
          f"frames (ref frame {ref})  -> {args.case}/final_data_hand.pkl")


if __name__ == "__main__":
    main()
