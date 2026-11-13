# Phase 2 of the delivery batch: CoTracking + 3D recon + delivery packaging, run on the
# (human-verified/corrected) masks from Phase 1. Delegates each episode to
# data_process/rerun_from_seg.py, which is the single source of truth for the
# seg -> cotracker -> pcd/track -> shape -> align -> final -> delivery sequence (it drops
# any stale downstream first, so it works both for a fresh episode and for a re-run after
# mask corrections).
#
# Usage:
#   python run_downstream.py                       # all episodes, in order
#   python run_downstream.py --only ep_20260630_061850   # one (or more) episodes
#   python run_downstream.py --keep_shape          # reuse existing TRELLIS shape mesh

import time
import argparse
import subprocess

from data_process.episodes import EPISODES, BASE

import os
PT = os.environ["KP_PY_PHYSTWIN"]
LOG = f"{os.environ.get('KP_LOG_DIR', '/tmp')}/run_downstream.log"


def log(m):
    line = f"[{time.strftime('%m-%d %H:%M:%S')}] {m}"
    print(line, flush=True)
    open(LOG, "a").write(line + "\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", nargs="*", help="episode name(s); default all")
    ap.add_argument("--keep_shape", action="store_true")
    args = ap.parse_args()

    eps = [e for e in EPISODES if not args.only or e["ep"] in args.only]
    log(f"==== DOWNSTREAM PHASE: {len(eps)} episodes ====")
    t0 = time.time()
    for e in eps:
        case = f"{BASE}/{e['ep']}"
        log(f"===== {e['ep']} =====")
        cmd = [PT, "data_process/rerun_from_seg.py", "--case", case,
               "--object_name", e["name"], "--interaction", e["interaction"]]
        if args.keep_shape:
            cmd.append("--keep_shape")
        rc = subprocess.call(cmd)
        log(f"===== DONE {e['ep']} rc={rc} =====")
    log(f"==== DOWNSTREAM PHASE DONE  {(time.time()-t0)/3600:.2f}h ====")


if __name__ == "__main__":
    main()
