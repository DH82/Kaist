# Re-run CoTracking + 3D recon (and delivery packaging) from CORRECTED segmentation.
#
# After a person fixes masks with mask_review_edit.py, every downstream product that
# was derived from the old masks is stale: cotracker (2D tracks are sampled inside the
# mask), the fused object point cloud, the TRELLIS shape prior, the aligned mesh, and
# final_data. This deletes those stale products for one case and regenerates them from
# the masks now on disk. Calibration/world-frame are untouched, so corrected masks ->
# corrected recon. SAM/preprocess are NOT re-run (segmentation is the human's input).
#
# Usage (phystwin env):
#   python data_process/rerun_from_seg.py --case ./data/different_types/ep_20260630_054132
#   # keep the existing TRELLIS shape mesh (skip the slow shape_prior step):
#   python data_process/rerun_from_seg.py --case <dir> --keep_shape
#
# object_name/interaction for the delivery metadata are read from the case's existing
# metadata.json (written by finalize_delivery on the first pass); override with flags.

import os
import re
import glob
import time
import shlex
import shutil
import argparse
import subprocess


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--case", required=True, help="PhysTwin case dir")
    ap.add_argument("--keep_shape", action="store_true",
                    help="do NOT regenerate the TRELLIS shape prior (reuse shape/object.glb)")
    ap.add_argument("--object_name", help="delivery object_name (else read from metadata.json)")
    ap.add_argument("--interaction", help="delivery interaction (else read from metadata.json)")
    ap.add_argument("--step_timeout", type=int, default=21600)   # 6h: pcd/track are genuinely long
    ap.add_argument("--force", action="store_true",
                    help="delete + regenerate everything (default: resume, reuse existing outputs)")
    ap.add_argument("--num_hands", type=int, default=-1,
                    help="passed to Mono/process_hand_data_ours.py. -1 = its own default, which "
                         "infers 2 unless the case name contains 'single'. That inference makes "
                         "the MANO fit crash (KeyError on ref_cidx[1]) on a one-hand capture "
                         "where no phantom second hand happened to be detected in frame 0 "
                         "(ep_20260826_084827 / _084841). Pass 1 for one-hand captures.")
    ap.add_argument("--keep_verification", action="store_true",
                    help="passed to finalize_delivery: keep verification videos + additional/ "
                         "+ cotracker_hand/ instead of deleting them")
    args = ap.parse_args()

    case = os.path.abspath(args.case)
    base = os.path.dirname(case)
    name = os.path.basename(case)
    # interpreters from config.env (bin/kp); -u so tqdm streams live through the PTY
    PT = [os.environ["KP_PY_PHYSTWIN"], "-u"]

    def run(tag, cmd):
        t0 = time.time()
        print(f"[{time.strftime('%H:%M:%S')}] START {tag}", flush=True)
        # run inside a PTY (script) so the child's tqdm streams live to the log instead
        # of being block-buffered -> real-time per-step progress in status.sh. The
        # TYPESCRIPT FILE (here /dev/stdout, i.e. the inherited run_downstream.log) is
        # what actually receives the child's output; -a appends so earlier steps survive,
        # -f flushes each write. (Pointing it at /dev/null loses everything.)
        rc = subprocess.call(["script", "-qefa", "-c", shlex.join(cmd), "/dev/stdout"],
                             timeout=args.step_timeout)
        print(f"[{time.strftime('%H:%M:%S')}] END   {tag} rc={rc} {(time.time()-t0)/60:.1f}m",
              flush=True)
        return rc

    def rm(p):
        if os.path.isdir(p):
            shutil.rmtree(p); print(f"  rm dir  {p}")
        elif os.path.exists(p):
            os.remove(p); print(f"  rm file {p}")

    # read delivery labels from existing metadata (finalize_delivery merged them in)
    obj_name, interaction = args.object_name, args.interaction
    meta_p = f"{case}/metadata.json"
    if (not obj_name or not interaction) and os.path.exists(meta_p):
        import json
        meta = json.load(open(meta_p))
        obj_name = obj_name or meta.get("object_name")
        interaction = interaction or meta.get("interaction")

    print(f"==== rerun-from-seg  {name}  keep_shape={args.keep_shape} force={args.force} ====")
    have = os.path.exists

    # --force: drop every seg-derived product (for re-running after MASK corrections).
    # default: RESUME — keep + reuse whatever already computed (masks unchanged).
    if args.force:
        for p in ["cotracker", "cotracker_hand", "track_process_data.pkl",
                  "final_data.pkl", "final_data_hand.pkl", "hand_track_data.pkl",
                  "shape/hand", "shape/matching",
                  "obj_hand_recon.mp4", "viz_sam_masks.mp4", "segmentation_view.mp4"]:
            rm(f"{case}/{p}")
        if not args.keep_shape:
            rm(f"{case}/shape")

    # 2) CoTracking (object + controller/hand).  --no_viz: skip the slow per-frame overlay
    #    render (a byproduct, not pipeline input) that previously blew the step timeout.
    if not have(f"{case}/cotracker/3.npz"):
        run("cotracker-obj", PT + ["data_process/track_object.py", "--base_path", base,
            "--case_name", name, "--obj_id", "0", "--out_dir", "cotracker", "--no_viz"])
    if not have(f"{case}/cotracker_hand/3.npz"):
        run("cotracker-hand", PT + ["data_process/track_object.py", "--base_path", base,
            "--case_name", name, "--obj_id", "1", "--out_dir", "cotracker_hand", "--no_viz"])

    # 3) shape prior (TRELLIS) from the object mask, unless kept
    if not args.keep_shape and not have(f"{case}/shape/object.glb"):
        if run("shape-rgba", PT + ["data_process/make_shape_rgba.py", "--case", case]) == 0:
            run("shape-prior", PT + ["data_process/shape_prior.py",
                "--img_path", f"{case}/shape/masked_image.png",
                "--output_dir", f"{case}/shape"])

    # 4) 3D recon: fuse RGB-D under masks -> filter -> track pcd
    if not have(f"{case}/track_process_data.pkl"):
        run("pcd", PT + ["data_process/data_process_pcd.py", "--base_path", base, "--case_name", name])
        run("mask", PT + ["data_process/data_process_mask.py", "--base_path", base,
            "--case_name", name, "--controller_name", "hand"])
        run("track", PT + ["data_process/data_process_track.py", "--base_path", base,
            "--case_name", name, "--no_viz"])
    if not have(f"{case}/hand_track_data.pkl") and not have(f"{case}/shape/hand/hand_pointcloud_3d.pkl"):
        run("hand-export", PT + ["data_process/export_hand_track_data.py",
            "--base_path", base, "--case_name", name])

    # NOTE: pcd/ is cleaned only AFTER align (below) — align.py reads pcd/0.npz to scale/
    # register the mesh, so deleting it here would crash align (FileNotFoundError) and lose
    # the shape prior on every case, even textured ones that align fine.

    # 5) align + final physics sample.
    #
    # align failure is FATAL. It used to be skipped so a weekend batch would not stall, on the
    # claim that the shape mesh is optional -- it is not. Without shape/matching/final_mesh.glb
    # data_process_sample.py runs without --shape_prior and writes surface_points and
    # interior_points as empty arrays, and real_data.py:36 then builds
    # structure_points = [object_points[0], (empty), (empty)]: a hollow shell with no interior
    # mass points, so the spring graph has nothing holding the object's thickness, and the
    # chamfer loss can only match against the observed points (spring_mass_warp.py:1038 uses
    # num_surface_points, which collapses to num_original_points). The episode still completes
    # and looks fine in the artifact checklist, which is how ep_20260826_083914 / _084415 /
    # _085058 were delivered with interior_points = 0.
    #
    # Stop here instead. Retrying with a different --ref_frame is NOT a fix: align.py:436 reads
    # pcd/{REF}.npz as the ARAP target, so the fitted mesh takes that frame's pose while the
    # sampled points are concatenated with object_points[0] (frame 0).
    if have(f"{case}/shape/object.glb") and not have(f"{case}/shape/matching/final_mesh.glb"):
        # captured, not streamed: the reason has to survive for review after a batch run
        cmd = PT + ["data_process/align.py", "--base_path", base,
                    "--case_name", name, "--controller_name", "hand"]
        t0 = time.time()
        print(f"[{time.strftime('%H:%M:%S')}] START align", flush=True)
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=args.step_timeout)
        rc = p.returncode
        print(p.stdout, end="")
        print(f"[{time.strftime('%H:%M:%S')}] END   align rc={rc} "
              f"{(time.time()-t0)/60:.1f}m", flush=True)
        if rc != 0 or not have(f"{case}/shape/matching/final_mesh.glb"):
            tail = "\n".join((p.stdout + p.stderr).splitlines()[-60:])
            note = (f"[{time.strftime('%F %T')}] align FAILED  rc={rc}  case={name}\n"
                    f"matched point number: "
                    + (re.findall(r"matched point number (\d+)", p.stdout + p.stderr) or ["-"])[-1]
                    + "\n\n" + tail + "\n")
            open(f"{case}/ALIGN_FAILED.txt", "w").write(note)
            with open(os.path.join(base, "align_failures.log"), "a") as f:
                f.write(note + "=" * 100 + "\n")
            print(f"  align 실패 사유 -> {case}/ALIGN_FAILED.txt "
                  f"및 {base}/align_failures.log", flush=True)
            raise SystemExit(
                f"[rerun_from_seg] SKIP {name}: align failed (rc={rc}), no "
                f"shape/matching/final_mesh.glb. Continuing would write final_data.pkl with "
                f"empty surface/interior arrays -- a different physical model. This case is "
                f"left unfinished on purpose; the batch moves on to the next episode.")
    if not have(f"{case}/final_data.pkl"):
        run("final", PT + ["data_process/data_process_sample.py", "--base_path", base,
            "--case_name", name, "--no_viz", "--shape_prior"])

    # pcd/ (read by align above) is no longer needed once final is done -> reclaim the disk
    if os.path.isdir(f"{case}/pcd") and have(f"{case}/final_data.pkl") \
            and have(f"{case}/track_process_data.pkl"):
        shutil.rmtree(f"{case}/pcd"); print("  cleaned pcd/ (disk)")

    # 5b) MANO hand fit (KAIST's Mono: HaMBA per-frame recon -> temporal MANO fit).
    # Placed AFTER final on purpose: stage 2's optional non-penetration term reads
    # final_data.pkl's object_points, and it must land BEFORE finalize so the exported
    # trajectory is picked up into the delivery layout.
    # Always two-handed (the script's default) -- which hands were really used is decided
    # afterwards from camera support, and forcing --num_hands 1 would discard a left hand.
    # DISPLAY is stripped so an X server crash cannot take the fit down with it.
    if not have(f"{case}/hand_v1/hand_reg.pkl"):
        run("mano", ["env", "-u", "DISPLAY", "-u", "XAUTHORITY",
                     os.environ["KP_PY_HAMBA"], "-u",
                     "Mono/process_hand_data_ours.py",
                     "--base_path", base, "--case_name", name]
                    + (["--num_hands", str(args.num_hands)] if args.num_hands > 0 else []))

    # 6) delivery packaging (layout + metadata merge + separate hand controller + viz)
    # finalize normally runs once (object_name in metadata.json marks it done), but it also
    # has to run again once a MANO fit appears for an already-delivered case -- otherwise
    # shape/hand/hand_mano_trajectory.pkl ships without being declared in layout/hand_notes.
    # Key this on the MANO FIT, not on the exported trajectory: the trajectory is produced
    # BY finalize (it calls export_hand_mano_trajectory.py), so requiring it here would be
    # circular and an already-delivered case that just gained a fit would never export it.
    _meta_txt = open(meta_p).read() if os.path.exists(meta_p) else ""
    _mano_undeclared = (have(f"{case}/hand_v1/hand_reg.pkl")
                        and "hand_mano_trajectory" not in _meta_txt)
    if obj_name and interaction and ("object_name" not in _meta_txt or _mano_undeclared):
        run("finalize", PT + ["data_process/finalize_delivery.py", "--case", case,
            "--object_name", obj_name, "--interaction", interaction]
            + (["--keep_verification"] if args.keep_verification else []))
    if not have(f"{case}/final_data_hand.pkl"):
        run("final-hand", PT + ["data_process/export_final_hand.py", "--case", case])
    # MANO counterpart of final_data_hand.pkl: the hand as PhysTwin-ready particles, which
    # is what inject_hand_controller.py --hand mano consumes in the physics phase. Without
    # this step that phase fails on a missing file.
    if have(f"{case}/shape/hand/hand_mano_trajectory.pkl") and not have(f"{case}/final_data_hand_mano.pkl"):
        run("final-hand-mano", PT + ["data_process/export_final_hand_mano.py", "--case", case])
    if not have(f"{case}/obj_hand_recon.mp4"):
        run("viz", ["bash", "viz_delivery.sh", case])

    print(f"==== rerun-from-seg DONE  {name} ====")


if __name__ == "__main__":
    main()
