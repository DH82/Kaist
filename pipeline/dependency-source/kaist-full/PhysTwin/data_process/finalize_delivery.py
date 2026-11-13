# Arrange a fully-processed case into the delivery layout, with the HAND mirrored next
# to the OBJECT under a hand/ subfolder (no separate additional/ folder):
#
#   cotracker/{cam}.npz                 object 2D tracks (T,N,2)[y,x] + visibility
#   cotracker/hand/{cam}.npz            hand   2D tracks (same format)
#   track_process_data.pkl              object 3D point cloud (T,N,3)+colors+vis
#   shape/object.glb, object.ply, ...   object 3D reconstruction (TRELLIS mesh)
#   shape/hand/hand_pointcloud_3d.pkl   hand   3D point cloud (T,N,3)+colors+vis
#   final_data.pkl                      PhysTwin physics-fitting input
#   metadata.json                       PhysTwin-native + object_name + interaction
#   mask/  color/  depth/  calibrate.pkl
#
# object_name + interaction are merged INTO metadata.json (no separate delivery_metadata).
# Idempotent: also migrates an earlier additional/ layout into this one. Optional
# verification videos (final_data.mp4, final_pcd.mp4, viz_sam_masks.mp4, review_4cam.mp4,
# delivery_metadata.json) are removed.
#
# Usage (phystwin env):
#   python data_process/finalize_delivery.py --case ./data/different_types/ep_XXXX \
#       --object_name "blue cloth" --interaction "flatten and fold a cloth by hand"

import os
import sys
import json
import glob
import shutil
import argparse
import subprocess
import tempfile


def _move(src, dst):
    if src and os.path.exists(src) and os.path.abspath(src) != os.path.abspath(dst):
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        if os.path.exists(dst):
            (shutil.rmtree if os.path.isdir(dst) else os.remove)(dst)
        shutil.move(src, dst)
        return True
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--case", required=True)
    ap.add_argument("--object_name", required=True)
    ap.add_argument("--interaction", required=True)
    ap.add_argument("--log", default="hand_v1", help="case subdir holding the MANO fit (hand_reg.pkl)")
    ap.add_argument("--keep_verification", action="store_true",
                    help="keep the verification videos and intermediate dirs that step 4 "
                         "normally deletes (final_data.mp4, final_pcd.mp4, review_4cam.mp4, "
                         "additional/, cotracker_hand/). Everything else is unchanged.")
    args = ap.parse_args()
    case = args.case
    add = f"{case}/additional"

    # 1) hand 2D tracks -> cotracker/hand/{cam}.npz  (from cotracker_hand/ or old additional/)
    os.makedirs(f"{case}/cotracker/hand", exist_ok=True)
    for srcdir in (f"{case}/cotracker_hand", f"{add}/hand_2d_tracking"):
        for p in glob.glob(f"{srcdir}/[0-9]*.npz"):
            _move(p, f"{case}/cotracker/hand/{os.path.basename(p)}")
    n2d = len(glob.glob(f"{case}/cotracker/hand/*.npz"))
    print(f"  hand 2D  -> cotracker/hand/ ({n2d} npz)")

    # 2) hand 3D point cloud (CoTracker) -> shape/hand/hand_pointcloud_3d.pkl
    os.makedirs(f"{case}/shape/hand", exist_ok=True)
    for src in (f"{case}/hand_track_data.pkl", f"{add}/hand_pointcloud_3d.pkl"):
        if _move(src, f"{case}/shape/hand/hand_pointcloud_3d.pkl"):
            print("  hand 3D  -> shape/hand/hand_pointcloud_3d.pkl (CoTracker)")
            break

    # 2b) hand MANO vertex trajectory -> shape/hand/hand_mano_trajectory.pkl
    # This is the PRIMARY hand deliverable; the CoTracker cloud above ships alongside it.
    # Exported here (not moved) so the layout stays correct even when this runs before the
    # MANO fit; absent fit => the key is simply left out of metadata.json.
    mano_out = f"{case}/shape/hand/hand_mano_trajectory.pkl"
    has_fit = os.path.isfile(f"{case}/{args.log}/hand_reg.pkl")
    has_mano = has_fit or os.path.isfile(mano_out)
    print(f"  hand MANO-> shape/hand/hand_mano_trajectory.pkl"
          if has_mano else "  hand MANO-> (no MANO fit yet; run Mono/process_hand_data_ours.py)")

    # 3) merge object_name + interaction INTO metadata.json (drop delivery_metadata.json)
    meta = json.load(open(f"{case}/metadata.json"))
    meta["object_name"] = args.object_name
    meta["interaction"] = args.interaction
    ncam = len(glob.glob(f"{case}/mask/mask_info_*.json")) or len(meta.get("intrinsics", []))
    meta["num_cameras"] = int(ncam)
    meta["world_frame"] = "PhysTwin convention (table z=0, object z<=0, gravity +z)"
    meta["layout"] = {
        "object_2d_tracking": "cotracker/{cam}.npz",
        "hand_2d_tracking": "cotracker/hand/{cam}.npz",
        "object_3d_pointcloud": "track_process_data.pkl",
        "object_3d_mesh": "shape/object.glb",
        "hand_3d_pointcloud": "shape/hand/hand_pointcloud_3d.pkl",
        "physics_input": "final_data.pkl",
        "segmentation": "mask/{cam}/{0=object,1=hand}/{frame}.png",
    }
    if has_mano:
        # primary hand deliverable; listed first in the hand notes below
        meta["layout"]["hand_mano_trajectory"] = "shape/hand/hand_mano_trajectory.pkl"
    # The RAW fit ships too. It is what KAIST's own Mono code wrote (and what their
    # render_hand.py reads directly), so declaring it lets the recipient use their own
    # tooling instead of adapting to the repacked array above.
    if os.path.isfile(f"{case}/{args.log}/hand_reg.pkl"):
        meta["layout"]["hand_mano_fit_raw"] = f"{args.log}/hand_reg.pkl"
        if os.path.isfile(f"{case}/{args.log}/hand_reg_params.pkl"):
            meta["layout"]["hand_mano_fit_params"] = f"{args.log}/hand_reg_params.pkl"
    meta["hand_notes"] = {
        "primary": ("shape/hand/hand_mano_trajectory.pkl -- MANO vertex trajectory: "
                    "mano_verts (T,H,778,3) world xyz float32, mano_valid (T,H) bool, "
                    "hand_order (H,) e.g. ['left','right'], faces {side:(1538,3)}. "
                    "Frames with no fit are NaN + valid=False so the frame axis stays "
                    "aligned with color/, depth/, mask/ and the object point cloud.")
        if has_mano else "shape/hand/hand_mano_trajectory.pkl -- not exported for this case",
        "secondary": ("shape/hand/hand_pointcloud_3d.pkl -- CoTracker hand point cloud "
                      "(T,N,3)+colors+visibilities; kept for reference, same processing "
                      "convention as the object cloud."),
        "raw_fit": (f"{args.log}/hand_reg.pkl -- the MANO fit exactly as Mono's "
                    "data_process_hands.py wrote it: {frame: {'right'|'left': (778,3) world "
                    "verts}}. hand_mano_trajectory.pkl above is this same data repacked into "
                    "one array with validity attached; nothing about the vertices is changed. "
                    f"{args.log}/hand_reg_params.pkl carries the per-frame MANO params "
                    "(global_orient/hand_pose/betas/transl) and gt_valid, the per-camera 2D "
                    "confidences the fit was solved against."),
    }
    fd, meta_tmp = tempfile.mkstemp(prefix="metadata.json.partial-", dir=case)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(meta, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(meta_tmp, f"{case}/metadata.json")
    finally:
        if os.path.exists(meta_tmp):
            os.unlink(meta_tmp)
    print(f"  metadata.json += object_name='{args.object_name}', interaction, layout")

    # Metadata is part of the source contract. Export only AFTER its final commit.
    # Do not regenerate particle files here: their voxel settings belong to the caller.
    if has_fit:
        script_dir = os.path.dirname(os.path.abspath(__file__))
        subprocess.run([sys.executable, os.path.join(script_dir, "export_hand_mano_trajectory.py"),
                        "--case", case, "--log", args.log,
                        "--faces", os.path.join(os.path.dirname(script_dir), "data", "mano_faces.pkl")],
                       check=True)
    elif has_mano:
        raise RuntimeError("Existing MANO trajectory has no source fit to validate; retain the requested fit log before finalizing")

    # 4) clean up: empty dirs + optional verification videos + old metadata
    # delivery_metadata.json always goes (its contents were merged into metadata.json above,
    # so leaving it would ship two sources of truth). The rest is verification material and is
    # kept under --keep_verification.
    p = f"{case}/delivery_metadata.json"
    if os.path.exists(p):
        os.remove(p)
    if args.keep_verification:
        print("  kept (--keep_verification): additional/, cotracker_hand/, viz mp4s")
    else:
        for junk in ["additional", "cotracker_hand"]:
            d = f"{case}/{junk}"
            if os.path.isdir(d):
                shutil.rmtree(d)
        for f in ["final_data.mp4", "final_pcd.mp4", "viz_sam_masks.mp4", "review_4cam.mp4"]:
            p = f"{case}/{f}"
            if os.path.exists(p):
                os.remove(p)
        print("  removed: additional/, cotracker_hand/, delivery_metadata.json, optional viz mp4s")
    print(f"[done] delivery layout: {case}")


if __name__ == "__main__":
    main()
