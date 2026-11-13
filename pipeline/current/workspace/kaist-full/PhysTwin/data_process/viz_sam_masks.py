# Visualize SAM3.1 masks: overlay the OBJECT mask (id 0) and HAND mask (id 1) on the
# color frames, tiled across cameras, as a video. Object = green, hand = red.
# This is a CHECK video to confirm the segmentation is clean (e.g. the cloth mask did
# not leak onto clothing, the hand mask is wrist-down).
#
# Usage (phystwin env):
#   python data_process/viz_sam_masks.py --case ./data/different_types/ep_20260630_014921
#
# Output: {case}/viz_sam_masks.mp4

import os
import json
import glob
import argparse

import numpy as np
import cv2


def load_mask(case, cam, oid, f):
    p = f"{case}/mask/{cam}/{oid}/{f}.png"
    if os.path.isfile(p):
        return cv2.imread(p, 0) > 127
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--case", required=True)
    ap.add_argument("--object_id", type=int, default=0)
    ap.add_argument("--hand_id", type=int, default=1)
    ap.add_argument("--out", default=None)
    ap.add_argument("--fps", type=int, default=30)
    ap.add_argument("--alpha", type=float, default=0.45)
    args = ap.parse_args()

    case = args.case
    out = args.out or f"{case}/viz_sam_masks.mp4"
    meta = json.load(open(f"{case}/metadata.json"))
    N = meta["frame_num"]
    cams = sorted(int(os.path.basename(p)) for p in glob.glob(f"{case}/color/*")
                  if os.path.basename(p).isdigit())
    W, H = meta["WH"]
    # 2x2 tile layout (works for up to 4 cams)
    cols = 2 if len(cams) > 1 else 1
    rows = int(np.ceil(len(cams) / cols))
    tw, th = W // 2, H // 2  # downscale each tile
    canvas_w, canvas_h = tw * cols, th * rows

    vw = cv2.VideoWriter(out, cv2.VideoWriter_fourcc(*"mp4v"), args.fps, (canvas_w, canvas_h))
    OBJ = np.array([0, 200, 0], np.float32)    # green (BGR)
    HND = np.array([0, 0, 230], np.float32)     # red (BGR)
    for f in range(N):
        canvas = np.zeros((canvas_h, canvas_w, 3), np.uint8)
        for k, cam in enumerate(cams):
            cp = f"{case}/color/{cam}/{f}.png"
            img = cv2.imread(cp) if os.path.isfile(cp) else np.zeros((H, W, 3), np.uint8)
            img = img.astype(np.float32)
            mo = load_mask(case, cam, args.object_id, f)
            mh = load_mask(case, cam, args.hand_id, f)
            if mo is not None:
                img[mo] = (1 - args.alpha) * img[mo] + args.alpha * OBJ
            if mh is not None:
                img[mh] = (1 - args.alpha) * img[mh] + args.alpha * HND
            img = np.clip(img, 0, 255).astype(np.uint8)
            cv2.putText(img, f"cam{cam} f{f}", (8, 22), cv2.FONT_HERSHEY_SIMPLEX,
                        0.6, (255, 255, 255), 2)
            tile = cv2.resize(img, (tw, th))
            r, c = k // cols, k % cols
            canvas[r * th:(r + 1) * th, c * tw:(c + 1) * tw] = tile
        cv2.putText(canvas, "object=green  hand=red", (8, canvas_h - 10),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 0), 2)
        vw.write(canvas)
        if f % 60 == 0:
            print(f"  frame {f}/{N}")
    vw.release()
    print(f"[done] {out}")


if __name__ == "__main__":
    main()
