# Build the RGBA object image that TRELLIS shape_prior consumes, BYPASSING the broken
# image_upscale step (diffusers/transformers version mismatch in this env). Takes a
# color frame with alpha = the SAM object mask, cropped to the object bbox + padding.
# Picks the first frame that actually has a non-empty object mask.
#
# Usage (phystwin env):
#   python data_process/make_shape_rgba.py --case ./data/different_types/ep_XXXX

import os
import glob
import argparse

import numpy as np
import cv2
from PIL import Image


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--case", required=True)
    ap.add_argument("--cam", type=int, default=0)
    ap.add_argument("--object_id", type=int, default=0)
    ap.add_argument("--pad", type=int, default=25)
    ap.add_argument("--frame", type=int, default=None,
                    help="use THIS frame (a clean flat view) instead of the first non-empty; "
                         "TRELLIS needs a good frame or it reconstructs a wrong shape")
    args = ap.parse_args()

    mask_dir = f"{args.case}/mask/{args.cam}/{args.object_id}"
    frames = sorted(int(os.path.basename(p)[:-4]) for p in glob.glob(f"{mask_dir}/*.png"))
    chosen = None
    if args.frame is not None:
        m = cv2.imread(f"{mask_dir}/{args.frame}.png", 0)
        if m is None or (m > 127).sum() <= 200:
            raise RuntimeError(f"frame {args.frame} has no usable object mask in {mask_dir}")
        chosen = (args.frame, m > 127)
    else:
        for f in frames:
            m = cv2.imread(f"{mask_dir}/{f}.png", 0) > 127
            if m.sum() > 200:
                chosen = (f, m)
                break
    if chosen is None:
        raise RuntimeError(f"no non-empty object mask in {mask_dir}")
    f, m = chosen
    img = cv2.cvtColor(cv2.imread(f"{args.case}/color/{args.cam}/{f}.png"), cv2.COLOR_BGR2RGB)
    ys, xs = np.where(m)
    y0 = max(0, ys.min() - args.pad); x0 = max(0, xs.min() - args.pad)
    y1 = min(img.shape[0], ys.max() + args.pad); x1 = min(img.shape[1], xs.max() + args.pad)
    rgba = np.dstack([img[y0:y1, x0:x1], (m[y0:y1, x0:x1] * 255).astype(np.uint8)])
    os.makedirs(f"{args.case}/shape", exist_ok=True)
    Image.fromarray(rgba).save(f"{args.case}/shape/masked_image.png")
    print(f"[make_shape_rgba] frame {f}, rgba {rgba.shape} -> {args.case}/shape/masked_image.png")


if __name__ == "__main__":
    main()
