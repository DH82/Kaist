"""Per-case HAND worker (mirror of process_data_ours.py, but for the hand pipeline).

Two stages, each dispatched to its own conda env via os.system (like process_data_ours.py dispatches each
data_process step):
  Stage 1 — HaMBA per-frame hand recon        -> `hamba` env  (pytorch-lightning/detectron2/mmcv)
  Stage 2 — MANO temporal fit (data_process_hands) -> current (`phystwin`) env (mediapipe/pytorch3d)

Run from the repo root (stage 2 uses repo-relative paths). You do NOT need to be in the `hamba` env.
num_hands: -1 = auto (1 if 'single' in case name else 2).
"""
import os
import tempfile
import shutil
from argparse import ArgumentParser

parser = ArgumentParser()
parser.add_argument("--base_path", type=str, default="./data/DynamicDenseHOI/different_types")
parser.add_argument("--case_name", type=str, required=True)
parser.add_argument("--num_hands", type=int, default=-1)
parser.add_argument("--num_cams", type=int, default=-1,
                    help="-1 = auto from the case (metadata.json num_cameras, else color/ dirs). "
                         "data_process_hands.py defaults to 3 and this script used not to pass "
                         "the flag at all, so a 4-cam rig silently fit on cams 0-2 only.")
parser.add_argument("--log", type=str, default="hand_v1")
parser.add_argument("--seed", type=int, default=1027)
args = parser.parse_args()

# Set the stage flags
PROCESS_HAMBA = True   # stage 1: HaMBA per-frame hand recon (own env)
PROCESS_FIT = True     # stage 2: MANO temporal fit (current env)

# Per-stage conda-env python interpreters.
# [local] Both stages run in OUR `hamba` env, not KAIST's /root path and not `phystwin`.
# Stage 1 AND stage 2 both build a MANO layer (smplx + chumpy) and stage 2 also needs
# mediapipe -- none of which are in `phystwin`. Pulling that stack (plus mmcv 1.3.9 /
# detectron2) into `phystwin` would put the physics fit at risk for no benefit, and stage 2
# imports no PhysTwin internals (no qqtt), so running it from `hamba` is safe.
HAMBA_PY = os.environ["KP_PY_HAMBA"]                       # stage 1 (config.env)
STAGE2_PY = HAMBA_PY                                      # stage 2 (see note above)

ROOT = os.path.abspath(os.path.dirname(__file__))
# absolutised because both stages cd elsewhere before running (see below)
base_path = os.path.abspath(args.base_path)
case_name = args.case_name
HAMBA_OUT = os.path.join(ROOT, "data_process/hand_recon_out")

# camera count for stage 2's 2D losses. KAIST's README passes --num_cams 4 explicitly; the
# script's own default is 3, so leaving it unset drops the last camera on our 4-cam rig.
num_cams = args.num_cams
if num_cams < 1:
    case_dir = os.path.join(base_path, case_name)
    try:
        import json
        with open(os.path.join(case_dir, "metadata.json")) as f:
            num_cams = int(json.load(f)["num_cameras"])
    except Exception:
        num_cams = len([d for d in os.listdir(os.path.join(case_dir, "color")) if d.isdigit()])
    print(f"[process_hand_data_ours] num_cams auto-detected = {num_cams}")

if PROCESS_HAMBA:
    # HaMBA hand reconstruction. script_hand_recon iterates os.listdir(img_folder) and reads
    # {seq}/color/{cam}/*.png -> {out}/{seq}/{cam}/results.pt. Scope to this case via a temp symlink dir.
    tmp = tempfile.mkdtemp()
    os.symlink(os.path.abspath(f"{base_path}/{case_name}"), os.path.join(tmp, case_name))
    rc = os.system(
        f"cd {ROOT}/data_process/Hamba && {HAMBA_PY} script_hand_recon.py "
        f"--img_folder {tmp} --out_folder {HAMBA_OUT}"
        # one hand => one person, so duplicate person boxes are always spurious; collapsing
        # them keeps the detection count at 2 and out of the branch that Nones the slots.
        + (" --single_person" if args.num_hands == 1 else "")
    )
    shutil.rmtree(tmp, ignore_errors=True)
    # os.system's status used to be ignored, so a crashed stage exited 0 and the later
    # steps silently skipped on the missing outputs. Propagate the failure instead.
    if rc != 0:
        raise SystemExit(f"[process_hand_data_ours] stage 1 (HaMBA) failed: status={rc}")

if PROCESS_FIT:
    # MANO temporal fit: consumes HaMBA results.pt + the case data -> {case}/{log}/hand_reg*.pkl
    #
    # [local] MUST run with cwd=ROOT (the Mono dir). data_process_hands.py resolves its MANO
    # assets as 'data_process/Hamba/_DATA/data/mano' and its debug dumps as '../debug/...',
    # both relative to Mono -- not to the PhysTwin repo root this script is invoked from.
    # Stage 1 already cd'd; stage 2 did not, so it died on
    #   AssertionError: Path data_process/Hamba/_DATA/data/mano does not exist!
    # the first time a run actually reached it (2026-08-03).
    os.makedirs(os.path.join(os.path.dirname(ROOT), "debug"), exist_ok=True)
    rc = os.system(
        f"cd {ROOT} && {STAGE2_PY} {ROOT}/data_process/data_process_hands.py "
        f"--hamba_base_path {HAMBA_OUT} --phystwin_base_path {base_path} --case_name {case_name} "
        f"--temp_weight 500000 --hamba_weight 0.01 --log {args.log} --seed {args.seed} "
        f"--num_iters 1500 --num_hands {args.num_hands} --num_cams {num_cams}"
    )
    if rc != 0:
        raise SystemExit(f"[process_hand_data_ours] stage 2 (MANO fit) failed: status={rc}")
