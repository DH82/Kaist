# Render a 2-color visibility-check overlay from an existing CoTracker npz:
#   visible point  -> green
#   occluded point -> red (CoTracker still predicts a position; flagged not-visible)
# All active points (born by frame f) are drawn, so you can see occluded points
# drift while visible ones stay glued to the surface. Does NOT touch the data.
#
# Output: cotracker/{cam}_vischeck.mp4   (plain single-color {cam}_30fps.mp4 stays)
#
# Usage (phystwin env):
#   python data_process/viz_cotracker.py --base_path ./data/different_types --case_name ep_test

import os
import glob
import argparse

import numpy as np
import cv2


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base_path", default="./data/different_types")
    ap.add_argument("--case_name", required=True)
    ap.add_argument("--cameras", type=int, nargs="*", default=None)
    ap.add_argument("--track_dir", default="cotracker", help="cotracker or cotracker_hand")
    ap.add_argument("--mask_id", type=int, default=None,
                    help="if set, color VISIBLE points by this mask (on-mask green / "
                         "off-mask=drift red) and skip occluded; else green=visible/red=occluded")
    args = ap.parse_args()

    case = f"{args.base_path}/{args.case_name}"
    td = args.track_dir
    cams = args.cameras if args.cameras is not None else sorted(
        int(os.path.splitext(os.path.basename(p))[0])
        for p in glob.glob(f"{case}/{td}/*.npz"))

    for cam in cams:
        npz = f"{case}/{td}/{cam}.npz"
        if not os.path.isfile(npz):
            print(f"  [skip] cam {cam}: no npz"); continue
        d = np.load(npz)
        tr, vs, qf = d["tracks"], d["visibility"], d["query_frame"]  # tr [y,x]
        T, N = vs.shape
        f0 = cv2.imread(f"{case}/color/{cam}/0.png")
        H, W = f0.shape[:2]
        suffix = "_maskcheck" if args.mask_id is not None else "_vischeck"
        vw = cv2.VideoWriter(f"{case}/{td}/{cam}{suffix}.mp4",
                             cv2.VideoWriter_fourcc(*"mp4v"), 30, (W, H))
        for f in range(T):
            img = cv2.imread(f"{case}/color/{cam}/{f}.png")
            if img is None:
                continue
            active = qf <= f
            yi = np.clip(tr[f, :, 0].astype(int), 0, H - 1)   # tracks are [y,x]
            xi = np.clip(tr[f, :, 1].astype(int), 0, W - 1)
            vis = vs[f]
            if args.mask_id is not None:
                mp = f"{case}/mask/{cam}/{args.mask_id}/{f}.png"
                m = (cv2.imread(mp, 0) > 127) if os.path.isfile(mp) else np.zeros((H, W), bool)
                ng = nr = 0
                for i in np.where(active & vis)[0]:               # visible only
                    if m[yi[i], xi[i]]:
                        cv2.circle(img, (xi[i], yi[i]), 2, (0, 255, 0), -1); ng += 1
                    else:
                        cv2.circle(img, (xi[i], yi[i]), 2, (0, 0, 255), -1); nr += 1
                cv2.putText(img, f"cam{cam} f{f} on-mask={ng} drift={nr}", (8, 24),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2)
            else:
                for i in np.where(active)[0]:
                    col = (0, 255, 0) if vis[i] else (0, 0, 255)  # green / red
                    cv2.circle(img, (xi[i], yi[i]), 2, col, -1)
                nv = int((active & vis).sum()); no = int((active & ~vis).sum())
                cv2.putText(img, f"cam{cam} f{f} vis={nv} occ={no}", (8, 24),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2)
            vw.write(img)
        vw.release()
        print(f"  [cam {cam}] -> {td}/{cam}{suffix}.mp4 (N={N})")

    print(f"[done] {case}/cotracker/")


if __name__ == "__main__":
    main()
