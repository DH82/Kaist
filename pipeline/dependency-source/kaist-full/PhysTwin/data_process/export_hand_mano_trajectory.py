# Convert the MANO temporal fit (Mono stage 2 output) into the HAND deliverable:
# a dense per-frame vertex trajectory in world coordinates.
#
#   in : {case}/{log}/hand_reg.pkl        {frame: {'right': (778,3)[, 'left': (778,3)]}}
#        {case}/{log}/hand_reg_params.pkl {frame: {side: MANO params}}   (optional, passed through)
#        data/mano_faces.pkl              {'left': (F,3), 'right': (F,3)}
#   out: {case}/shape/hand/hand_mano_trajectory.pkl
#
#          mano_verts   (T, H, 778, 3) float32   world xyz; NaN on frames with no fit
#          mano_valid   (T, H)         bool      True where the fit exists
#          hand_order   list[str]                e.g. ['left', 'right'] -> axis H
#          faces        {side: (F,3) int32}      MANO topology, constant over time
#          frame_ids    (T,)          int32      PhysTwin frame index
#          source / world_frame / units          provenance strings
#
# This is the MANO counterpart of shape/hand/hand_pointcloud_3d.pkl (CoTracker). Both
# ship: MANO is the primary hand deliverable, CoTracker is kept alongside it.
#
# Frames where the fit failed are NaN + valid=False rather than dropped, so the array
# stays index-aligned with color/, depth/, mask/ and the object point cloud.
#
# Usage:
#   python data_process/export_hand_mano_trajectory.py \
#       --case ./data/different_types/ep_20260721_072818 [--log hand_v1]

import os
import pickle
import argparse
import io

import numpy as np

from export_provenance import fit_contract, sha256, atomic_pickle

N_MANO_VERTS = 778


class CPUUnpickler(pickle.Unpickler):
    def find_class(self, module, name):
        if module == 'torch.storage' and name == '_load_from_bytes':
            import torch
            return lambda b: torch.load(io.BytesIO(b), map_location='cpu', weights_only=False)
        return super().find_class(module, name)


def _load(path):
    with open(path, "rb") as f:
        return CPUUnpickler(f).load()


def _frame_entries(reg):
    """-> {int frame: {side: (778,3)}}; tolerates str keys and bare-array entries."""
    out = {}
    for k, entry in reg.items():
        fidx = int(k)
        if entry is None:
            out[fidx] = {}
            continue
        if isinstance(entry, dict):
            sides = {s: v for s, v in entry.items() if v is not None}
        else:
            # bare array: (778,3) = right, (1556,3) = left+right stacked
            V = np.asarray(entry, dtype=np.float64).reshape(-1, 3)
            if V.shape[0] == 2 * N_MANO_VERTS:
                sides = {"left": V[:N_MANO_VERTS], "right": V[N_MANO_VERTS:]}
            else:
                sides = {"right": V}
        out[fidx] = sides
    return out


def _to_numpy(x):
    """Recursively strip torch tensors so the delivered pkl needs only numpy to open."""
    if hasattr(x, "detach") and hasattr(x, "cpu"):
        return x.detach().cpu().numpy()
    if isinstance(x, dict):
        return {k: _to_numpy(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return type(x)(_to_numpy(v) for v in x)
    return x


def camera_support(params, hand_order, T):
    """(T, H) int8: how many cameras actually observed each hand on each frame.

    Stage 2 stores the MediaPipe 2D confidences it fitted against as
    hand_reg_params[frame][side]['gt_valid'] with shape (NUM_CAMS, 21 joints, 1). A camera
    counts as supporting the hand when any of its joints carried confidence.

    This is the ONLY way to tell an observed hand from a fabricated one: the fit always
    emits finite vertices, even for a hand no camera ever saw (run a one-handed episode as
    two-handed and the absent hand comes out fully finite, carried by the temporal term
    alone). Vertex finiteness therefore cannot be used as a validity signal.
    """
    n = np.zeros((T, len(hand_order)), dtype=np.int8)
    for f, sides in (params or {}).items():
        f = int(f)
        if not (0 <= f < T) or not isinstance(sides, dict):
            continue
        for side, d in sides.items():
            if side not in hand_order or not isinstance(d, dict) or "gt_valid" not in d:
                continue
            gv = np.asarray(_to_numpy(d["gt_valid"]), dtype=np.float64)
            n[f, hand_order.index(side)] = int((gv.reshape(gv.shape[0], -1) > 0).any(1).sum())
    return n


def classify_hands(kept):
    """-> (hand_type, single_type) from the hands that survived phantom filtering.

        2 hands observed -> ('double', None)
        1 hand  observed -> ('single', 'right' | 'left')

    Which hand a one-handed episode used is kept as its own field rather than folded into
    the type string, because it changes what can be done with the case: stage 2's
    --num_hands 1 path discards the person's LEFT hand outright (it zeroes MediaPipe label
    'Right'), so a left-handed episode must stay on the two-handed path.
    """
    if len(kept) >= 2:
        return "double", None
    if len(kept) == 1:
        return "single", kept[0]
    return None, None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--case", required=True, help="e.g. ./data/different_types/ep_XXXX")
    ap.add_argument("--log", default="hand_v1", help="dir under the case holding hand_reg.pkl")
    ap.add_argument("--faces", default="data/mano_faces.pkl")
    ap.add_argument("--out", default=None)
    ap.add_argument("--min_cams", type=int, default=2,
                    help="cameras needed for a frame to count as observed (2 = the minimum "
                         "for the 3D position to be triangulated rather than guessed)")
    ap.add_argument("--min_frames", type=int, default=10,
                    help="a hand observed on fewer frames than this is treated as a phantom "
                         "and dropped from the deliverable")
    args = ap.parse_args()

    case = args.case.rstrip("/")
    reg_path = f"{case}/{args.log}/hand_reg.pkl"
    if not os.path.exists(reg_path):
        raise SystemExit(f"[error] no MANO fit at {reg_path} -- run Mono/process_hand_data_ours.py first")

    provenance = fit_contract(case, args.log)
    faces_hash = sha256(args.faces)
    entries = _frame_entries(_load(reg_path))
    faces_all = _load(args.faces)

    # frame axis: keep it aligned with the rest of the case, not just with the fitted frames
    n_meta = 0
    meta_path = f"{case}/metadata.json"
    if os.path.exists(meta_path):
        import json
        n_meta = int(json.load(open(meta_path)).get("frame_num", 0))
    T = max([n_meta] + [f + 1 for f in entries]) if (entries or n_meta) else 0

    hand_order = sorted({s for sides in entries.values() for s in sides})
    if not hand_order:
        raise SystemExit(f"[error] {reg_path} has no fitted hand on any frame")
    H = len(hand_order)

    verts = np.full((T, H, N_MANO_VERTS, 3), np.nan, dtype=np.float32)
    finite = np.zeros((T, H), dtype=bool)

    for fidx, sides in entries.items():
        if not (0 <= fidx < T):
            print(f"  [warn] frame {fidx} outside [0,{T}) -- skipped")
            continue
        for side, V in sides.items():
            V = np.asarray(V, dtype=np.float32).reshape(-1, 3)
            if V.shape[0] != N_MANO_VERTS:
                print(f"  [warn] frame {fidx} '{side}': {V.shape[0]} verts != {N_MANO_VERTS} -- skipped")
                continue
            if not np.isfinite(V).all():
                continue
            h = hand_order.index(side)
            verts[fidx, h] = V
            finite[fidx, h] = True

    # observed-vs-fabricated. Episodes are fitted as two-handed regardless of how many hands
    # were actually used, so an unused hand comes back fully finite; camera support is what
    # separates the two.
    params_path = f"{case}/{args.log}/hand_reg_params.pkl"
    params = _load(params_path) if os.path.exists(params_path) else None
    n_cams = camera_support(params, hand_order, T)
    valid = finite & (n_cams >= args.min_cams)

    keep = [i for i in range(H) if int(valid[:, i].sum()) >= args.min_frames]
    dropped = [hand_order[i] for i in range(H) if i not in keep]
    for i, s in enumerate(hand_order):
        if i not in keep:
            print(f"  [drop] '{s}': observed on {int(valid[:, i].sum())}/{T} frames "
                  f"(< {args.min_frames}) -- phantom, not delivered")
    if not keep:
        raise SystemExit(f"[error] no hand in {reg_path} is camera-observed; nothing to deliver")

    verts, valid, n_cams = verts[:, keep], valid[:, keep], n_cams[:, keep]
    hand_order = [hand_order[i] for i in keep]
    hand_type, single_type = classify_hands(hand_order)

    out_dict = {
        "fit_provenance": provenance,
        "faces_sha256": faces_hash,
        "export_schema_version": 2,
        "mano_verts": verts,
        "mano_valid": valid,
        "mano_n_cams": n_cams,
        "hand_order": hand_order,
        "hand_type": hand_type,
        "single_type": single_type,
        "faces": {s: np.asarray(faces_all[s]).astype(np.int32) for s in hand_order},
        "frame_ids": np.arange(T, dtype=np.int32),
        "source": f"MANO temporal fit (HaMBA + Mono/data_process/data_process_hands.py), log={args.log}",
        "world_frame": "PhysTwin convention (table z=0, object z<=0, gravity +z); same frame as calibrate.pkl",
        "units": "meters",
        "validity_note": (f"mano_valid = the fit is finite AND >= {args.min_cams} cameras observed "
                          f"the hand (mano_n_cams). Vertices exist on every frame regardless, so "
                          f"mano_n_cams is the quality signal; 0 means nothing observed it."),
    }
    if dropped:
        out_dict["dropped_phantom_hands"] = dropped

    # MANO pose/shape params, converted off torch so the deliverable opens with numpy alone
    if params is not None:
        out_dict["mano_params"] = _to_numpy({k: v for k, v in params.items()})
        out_dict["mano_params_note"] = "per-frame per-hand MANO params as fitted (stage-2 format, numpy)"

    out = args.out or f"{case}/shape/hand/hand_mano_trajectory.pkl"
    os.makedirs(os.path.dirname(out), exist_ok=True)
    if fit_contract(case, args.log) != provenance or sha256(args.faces) != faces_hash:
        raise RuntimeError("Source changed during trajectory export; no output committed")
    atomic_pickle(out, out_dict)

    per_hand = ", ".join(
        f"{s}: observed {int(valid[:, i].sum())}/{T}, mean cams {n_cams[:, i].mean():.2f}"
        for i, s in enumerate(hand_order))
    print(f"  mano_verts {verts.shape}  hand_type={hand_type}"
          + (f" ({single_type})" if single_type else ""))
    print(f"  {per_hand}")
    print(f"[done] {out}")


if __name__ == "__main__":
    main()
