"""Command-line wrapper for capture_qc.py (the collection-side RGB-D quality gate).

Runs on a RAW capture directory, i.e. the folder the RealSense recorder writes:
    cam_N.mp4, cam_N_depth/frame_XXXXXX.npy, cam_N_intrinsics.json,
    cam_N_timestamps.csv, extrinsics.json

The thresholds per scene type are the same presets the collection server uses
(PhysicalAI_data_code/Exo_video_data/pipeline/router.py, _SCENE_QC_PRESETS).

Usage:
    python run_capture_qc.py <raw_episode_dir> [--scene 1D|2D|3D] [--json out.json]
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from capture_qc import run_capture_qc  # noqa: E402

SCENE_QC_PRESETS = {
    # 1D (rope): IR-absorbing and thin -> very low depth coverage/overlap allowed
    "1D": dict(sharp_rel_thr=0.3, depth_dropout_pct=0.20, residual_warn_mm=30.0,
               depth_coverage_pass_pct=50.0, depth_coverage_warn_pct=35.0,
               depth_coverage_tolerance_pct=10.0, depth_dropout_tolerance_pct=0.05,
               overlap_pass_pct=45.0, overlap_warn_pct=30.0, overlap_comp_pct=55.0),
    # 2D (cloth): planar, uneven IR reflection -> medium coverage/overlap allowed
    "2D": dict(sharp_rel_thr=0.3, depth_dropout_pct=0.15, residual_warn_mm=25.0,
               depth_coverage_pass_pct=70.0, depth_coverage_warn_pct=55.0,
               depth_coverage_tolerance_pct=5.0, depth_dropout_tolerance_pct=0.05,
               overlap_pass_pct=65.0, overlap_warn_pct=50.0, overlap_comp_pct=75.0),
    # 3D (plush / rigid): volumetric -> coverage/overlap must be high
    "3D": dict(sharp_rel_thr=0.4, depth_dropout_pct=0.05, residual_warn_mm=15.0,
               depth_coverage_pass_pct=80.0, depth_coverage_warn_pct=65.0,
               depth_coverage_tolerance_pct=3.0, depth_dropout_tolerance_pct=0.02,
               overlap_pass_pct=75.0, overlap_warn_pct=60.0, overlap_comp_pct=85.0),
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("episode_dir")
    ap.add_argument("--scene", choices=sorted(SCENE_QC_PRESETS), default="3D")
    ap.add_argument("--json", default=None, help="also write the full result here")
    args = ap.parse_args()

    ep = os.path.abspath(args.episode_dir)
    extr = os.path.join(ep, "extrinsics.json")
    if not os.path.exists(extr) or os.path.getsize(extr) == 0:
        raise SystemExit(f"extrinsics.json missing or empty in {ep}")

    res = run_capture_qc(ep, **SCENE_QC_PRESETS[args.scene])
    if not res.get("ok"):
        raise SystemExit(f"capture QC failed: {res.get('error')}")

    print(f"episode {res['episode_id']}  frames={res['frame_count']}  cameras={res['cameras']}  "
          f"scene={args.scene}")
    for c in res["checks"]:
        print(f"  [{c['status']:4s}] {c['label']} ({c['name']}): {c['detail']}")
        if c.get("recommendation"):
            print(f"         -> {c['recommendation']}")
    print(f"OVERALL: {res['overall']}")
    if args.json:
        with open(args.json, "w") as f:
            json.dump(res, f, indent=2, ensure_ascii=False)
    sys.exit(0 if res["overall"] != "FAIL" else 1)


if __name__ == "__main__":
    main()
