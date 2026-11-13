# final_data_hand_mano.pkl : the MANO-fitted HAND as a PhysTwin-ready particle system,
# schema-compatible with export_final_hand.py's CoTracker-derived final_data_hand.pkl so
# inject_hand_controller.py can consume either one.
#
#   in : {case}/shape/hand/hand_mano_trajectory.pkl   (export_hand_mano_trajectory.py)
#        -- or --  {case}/{log}/hand_reg.pkl          (raw Mono stage-2 output)
#   out: {case}/final_data_hand_mano.pkl
#
#          hand_points        (T, N, 3) float64   world xyz, NaN-free (see imputation)
#          hand_colors        (T, N, 3) float64   synthetic, see below
#          hand_visibilities  (T, N)    bool      True where the MANO fit existed
#          hand_motions_valid (T, N)    bool      True where THIS and the next frame were fit
#          hand_imputed       (T,)      bool      frames whose points were interpolated
#          mano_hand_order / mano_vertex_id / mano_side_id   provenance for each column
#
# Why the CoTracker file is left alone: final_data_hand.pkl already shipped to KAIST and
# inject_hand_controller.py defaults to it. This writes a SEPARATE file so the physics fit
# can be pointed at MANO explicitly (--hand final_data_hand_mano.pkl) without changing what
# was already delivered.
#
# Two things MANO does not carry, and what is done about them:
#   colors  -- MANO is a mesh, not image-sampled points. Filled with a constant per side
#              (left/right distinguishable) purely so downstream readers do not break; they
#              carry no photometric meaning. hand_colors_synthetic=True records this.
#   gaps    -- frames with no fit are NaN in the trajectory, which the simulator cannot
#              integrate. Points are linearly interpolated across gaps (edges held), and the
#              affected frames are marked visibilities=False / hand_imputed=True so nothing
#              mistakes imputed motion for observed motion.
#
# Usage (any env with numpy):
#   python data_process/export_final_hand_mano.py --case ./data/different_types/ep_XXXX

import os
import pickle
import argparse

import numpy as np

from motion_contract import forward_valid, SCHEMA
from export_provenance import validate_trajectory, fit_contract, sha256, atomic_pickle

N_MANO_VERTS = 778
# constant RGB per side, 0-255 to match the CoTracker file's colour range
SIDE_COLOR = {"left": (60.0, 120.0, 220.0), "right": (220.0, 120.0, 60.0)}


def _load(path):
    with open(path, "rb") as f:
        return pickle.load(f)


def _from_trajectory(d):
    """hand_mano_trajectory.pkl -> (verts (T,H,778,3), valid (T,H), hand_order)."""
    return (np.asarray(d["mano_verts"], dtype=np.float64),
            np.asarray(d["mano_valid"], dtype=bool),
            list(d["hand_order"]))


def _from_hand_reg(reg, T_hint=0):
    """raw hand_reg.pkl -> (verts, valid, hand_order); mirrors export_hand_mano_trajectory."""
    entries = {}
    for k, e in reg.items():
        entries[int(k)] = {s: v for s, v in (e or {}).items() if v is not None} if isinstance(e, dict) else {}
    order = sorted({s for sides in entries.values() for s in sides})
    T = max([T_hint] + [f + 1 for f in entries])
    verts = np.full((T, len(order), N_MANO_VERTS, 3), np.nan)
    valid = np.zeros((T, len(order)), dtype=bool)
    for f, sides in entries.items():
        if not (0 <= f < T):
            continue
        for s, V in sides.items():
            V = np.asarray(V, dtype=np.float64).reshape(-1, 3)
            if V.shape[0] == N_MANO_VERTS and np.isfinite(V).all():
                verts[f, order.index(s)] = V
                valid[f, order.index(s)] = True
    return verts, valid, order


def _interp_gaps(traj, ok):
    """traj (T,N,3) with NaN where not ok (T,N) -> gap-filled in place along time.

    Linear between the surrounding fitted frames; leading/trailing gaps hold the nearest
    fitted frame. A column with no fitted frame at all is left as NaN (caller drops it).
    """
    T = traj.shape[0]
    t = np.arange(T)
    for i in range(traj.shape[1]):
        good = ok[:, i]
        if good.all() or not good.any():
            continue
        for c in range(3):
            traj[:, i, c] = np.interp(t, t[good], traj[good, i, c])
    return traj


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--case", required=True, help="e.g. ./data/different_types/ep_XXXX")
    ap.add_argument("--log", default="hand_v1", help="fallback: case subdir holding hand_reg.pkl")
    ap.add_argument("--volume_sample_size", type=float, default=0.005,
                    help="voxel dedup size in metres (same default as export_final_hand.py)")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    case = args.case.rstrip("/")
    traj_path = f"{case}/shape/hand/hand_mano_trajectory.pkl"
    reg_path = f"{case}/{args.log}/hand_reg.pkl"

    if not os.path.isfile(traj_path):
        raise SystemExit("[error] Export a provenance-validated hand_mano_trajectory.pkl first; raw fit fallback cannot establish observation validity")
    trajectory_hash = sha256(traj_path)
    trajectory = _load(traj_path)
    provenance = validate_trajectory(trajectory, case, args.log)
    verts, valid, order = _from_trajectory(trajectory)
    src = traj_path

    T, H = verts.shape[0], verts.shape[1]
    if not valid.any():
        raise SystemExit(f"[error] {src} has no fitted frame")

    # (T,H,778,3) -> (T, H*778, 3), with per-column provenance
    pts = verts.reshape(T, H * N_MANO_VERTS, 3)
    vis = np.repeat(valid, N_MANO_VERTS, axis=1)               # (T, H*778)
    side_id = np.repeat(np.arange(H), N_MANO_VERTS)
    vert_id = np.tile(np.arange(N_MANO_VERTS), H)

    # drop hands that were never fitted at all (nothing to interpolate from)
    keep_col = vis.any(0)
    if not keep_col.all():
        dropped = sorted({order[s] for s in side_id[~keep_col]})
        print(f"  [warn] never-fitted hand(s) dropped: {dropped}")
        pts, vis, side_id, vert_id = pts[:, keep_col], vis[:, keep_col], side_id[keep_col], vert_id[keep_col]

    pts = _interp_gaps(pts, vis)
    if not np.isfinite(pts).all():
        raise SystemExit("[error] gap fill left non-finite points")

    # voxel dedup on the best-fit frame, mirroring export_final_hand.py
    ref = int(vis.sum(1).argmax())
    p_ref = pts[ref]
    mn = p_ref[vis[ref]].min(0) if vis[ref].any() else p_ref.min(0)
    grid, idx = set(), []
    for i in range(pts.shape[1]):
        g = tuple(np.floor((p_ref[i] - mn) / args.volume_sample_size).astype(int))
        if g not in grid:
            grid.add(g)
            idx.append(i)
    idx = np.asarray(idx, dtype=int)

    sel_vis = vis[:, idx]
    colors = np.zeros((T, len(idx), 3), dtype=np.float64)
    for h, name in enumerate(order):
        colors[:, side_id[idx] == h, :] = SIDE_COLOR.get(name, (128.0, 128.0, 128.0))

    # Forward interval t -> t+1, identical to object and tracked-hand validity.
    motions_valid = forward_valid(sel_vis)

    out = {
        "motion_schema": dict(SCHEMA),
        "fit_provenance": provenance,
        "trajectory_sha256": trajectory_hash,
        "hand_imputed_per_point": ~sel_vis,
        "hand_imputed_per_hand": ~valid,
        "hand_observation_supported": sel_vis.copy(),
        "hand_points": pts[:, idx, :],
        "hand_colors": colors,
        "hand_visibilities": sel_vis,
        "hand_motions_valid": motions_valid,
        "hand_imputed": ~vis.any(1),
        "hand_colors_synthetic": True,
        "mano_hand_order": order,
        "mano_side_id": side_id[idx],
        "mano_vertex_id": vert_id[idx],
        "source": f"MANO fit via {os.path.basename(src)}; voxel dedup {args.volume_sample_size} m",
        "world_frame": "PhysTwin convention (table z=0, object z<=0, gravity +z); same frame as calibrate.pkl",
    }

    from motion_contract import validate_motion
    validate_motion(out, "hand")
    out_path = args.out or f"{case}/final_data_hand_mano.pkl"
    if fit_contract(case, args.log) != provenance or sha256(traj_path) != trajectory_hash:
        raise RuntimeError("Source changed during final export; no output committed")
    atomic_pickle(out_path, out)

    n_fit = int(vis.any(1).sum())
    print(f"[export_final_hand_mano] {pts.shape[1]} -> {len(idx)} hand particles x {T} frames "
          f"(ref frame {ref}, hands={order})")
    print(f"  fitted frames {n_fit}/{T}; {T - n_fit} interpolated (hand_imputed)")
    print(f"saved -> {out_path}")


if __name__ == "__main__":
    main()
