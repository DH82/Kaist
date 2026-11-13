# Phase 1 of the delivery batch: PREPROCESS + SAM segmentation for every episode, then
# STOP. Masks are meant to be reviewed/corrected by a human (data_process/sam_mask_refine.py)
# before the expensive CoTracking + 3D recon runs on them (run_downstream.py). This exists
# because SAM is not perfect on hard deformations, so we no longer chain seg -> recon blind.
#
# Run via bin/kp phase1 (sets the interpreters/paths from config.env).
# Resumable: skips preprocess/SAM whose outputs already exist.

import os
import glob
import time
import shlex
import subprocess

from data_process.episodes import EPISODES, BASE

# Interpreters come from config.env (bin/kp exports them). python -u = unbuffered, so
# per-chunk progress streams live through the script(1) PTY. HF_HOME is inherited.
SAM = ["env", "PYTORCH_ALLOC_CONF=expandable_segments:True", os.environ["KP_PY_SAM3"], "-u"]
PT = [os.environ["KP_PY_PHYSTWIN"], "-u"]
LOG_DIR = os.environ.get("KP_LOG_DIR", "/tmp")
LOG = f"{LOG_DIR}/run_sam_all.log"


def log(m):
    line = f"[{time.strftime('%m-%d %H:%M:%S')}] {m}"
    print(line, flush=True)
    open(LOG, "a").write(line + "\n")


def run(name, cmd):
    t0 = time.time()
    log(f"  START {name}")
    # persist EVERY step's stdout/stderr to its own file so a failure's error is not lost
    # (the old code truncated one shared file each step -> a crash was overwritten by the
    # next step). status.sh's live view still works: point run_all_current.log at this step.
    os.makedirs(f"{LOG_DIR}/sam_logs", exist_ok=True)
    logf = f"{LOG_DIR}/sam_logs/{name.replace(':', '_')}.log"
    try:
        os.remove(f"{LOG_DIR}/run_all_current.log")
    except FileNotFoundError:
        pass
    os.symlink(logf, f"{LOG_DIR}/run_all_current.log")
    with open(logf, "w") as f:
        f.write(f"=== {name} ===\n")
    # PTY (script): the TYPESCRIPT FILE (logf) receives the child's transcript incl. live
    # tqdm; -a appends after the header, -f flushes each write. (Sending it to /dev/null
    # or relying on redirected stdout does NOT capture it.)
    rc = subprocess.call(["script", "-qefa", "-c", shlex.join(cmd), logf], timeout=9000)
    log(f"  END   {name} rc={rc} {(time.time()-t0)/60:.1f}m  log={logf}")
    if rc != 0:
        log(f"  !! FAILED {name} -- see {logf}")
    return rc


def all_cams(pat):
    return all(glob.glob(pat.format(c=c)) for c in range(4))


def main():
    log(f"==== SAM PHASE: {len(EPISODES)} episodes ====")
    t0 = time.time()
    for e in EPISODES:
        case = f"{BASE}/{e['ep']}"
        log(f"===== {e['ep']}  object='{e['name']}' prompt='{e['prompt']}' =====")
        # EPISODES is the shared config for BOTH phases, so it keeps entries whose raw capture
        # has since been archived out of BASE (run_downstream still needs their object names).
        # Without this guard preprocess would run on a path that does not exist and fail.
        if not os.path.isdir(case):
            log(f"  source dir absent -> skip ({case})")
            continue
        if not os.path.exists(f"{case}/metadata.json"):
            run(f"{e['ep']}:preprocess", PT + ["data_process/preprocess_rgbd.py",
                "--src", case, "--out", BASE, "--case_name", e["ep"]])
        if not all_cams(f"{case}/mask/{{c}}/0"):
            # drop leading/trailing exposure-bad frames (auto-exposure warmup washes the first
            # frames white) BEFORE SAM, so segmentation never sees unidentifiable frames.
            # Idempotent: on an already-trimmed case it re-detects 0 and no-ops.
            run(f"{e['ep']}:trim", PT + ["data_process/trim_exposure.py", "--case", case, "--apply"])
            run(f"{e['ep']}:SAM", SAM + ["data_process/segment_object_sam3.py",
                "--base_path", BASE, "--case_name", e["ep"], "--object_prompts", e["prompt"],
                "--object_label", e["label"], "--scene_type", e["scene"], "--chunk_frames", "300"])
        else:
            log(f"  SAM already present for {e['ep']} (skip)")
    log(f"==== SAM PHASE DONE  {(time.time()-t0)/3600:.2f}h ====")
    log("NEXT: review masks with data_process/sam_mask_refine.py, then run_downstream.py")


if __name__ == "__main__":
    main()
