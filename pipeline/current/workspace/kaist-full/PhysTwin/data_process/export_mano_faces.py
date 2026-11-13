# One-off: extract the MANO face topology (shared by every frame/case) out of the
# MANO_{LEFT,RIGHT}.pkl assets into a small standalone pickle, so that neither the
# delivery export nor render_hand.py has to import chumpy.
#
# Must run in the `hamba` env (MANO pkl unpickles chumpy arrays).
#
# Usage:
#   $KP_PY_HAMBA data_process/export_mano_faces.py   (or: bin/kp setup-mano)
#
# Output: {'left': (F,3) int32, 'right': (F,3) int32}, written to BOTH
#   data/mano_faces.pkl        used by data_process/export_hand_mano_trajectory.py
#   Mono/data/mano_faces.pkl   REQUIRED by KAIST's code, which resolves it relative to Mono:
#                              data_process_hands.py:151 (FACES['left'/'right']) and
#                              render_hand.py --faces. Mono/data/ ships empty, so without
#                              this the MANO temporal fit dies at import with
#                              FileNotFoundError: 'data/mano_faces.pkl'.

import os
import pickle
import argparse

import numpy as np

MANO_DIR = "Mono/data_process/Hamba/_DATA/data/mano"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mano_dir", default=MANO_DIR)
    ap.add_argument("--out", default="data/mano_faces.pkl")
    ap.add_argument("--mono_out", default="Mono/data/mano_faces.pkl",
                    help="second copy at the path KAIST's Mono code resolves; '' to skip")
    args = ap.parse_args()

    faces = {}
    for side, fname in (("left", "MANO_LEFT.pkl"), ("right", "MANO_RIGHT.pkl")):
        path = os.path.join(args.mano_dir, fname)
        d = pickle.load(open(path, "rb"), encoding="latin1")
        f = np.asarray(d["f"]).astype(np.int32)
        v = np.asarray(d["v_template"])
        print(f"  {side:5s}: faces {f.shape}  verts {v.shape}")
        faces[side] = f

    for path in [p for p in (args.out, args.mono_out) if p]:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "wb") as fh:
            pickle.dump(faces, fh)
        print(f"[done] {path}")


if __name__ == "__main__":
    main()
