# Episode -> object mapping for the delivery batch, loaded from a JSON file instead of being
# written into this module. Shared by run_sam_all.py (Phase 1: segmentation), run_downstream.py
# / run_delivery.sh (Phase 2: CoTracking + 3D recon + delivery) and run_physics.py.
#
# The file is {KP_DATA_BASE}/episodes.json by default; set EPISODES_JSON to use another.
# Format: one entry per episode, keyed by the episode (case) directory name:
#
#   {
#     "ep_20260826_083803": {
#       "prompt": "blue cloth",        SAM 3.1 object text prompt
#       "label": "cloth",              mask label for the object (mask_info)
#       "scene": "2d",                 1d / 2d / 3d -> hand-prompt preset in segment_object_sam3.py
#       "name": "blue cloth",          delivery object_name (metadata.json, xlsx)
#       "interaction": "fold, roll and unfold a blue cloth on a table by hand"
#     }
#   }
#
# Every field is required; a missing one stops the run here rather than falling back to a guess.
# The consumers keep importing EPISODES (list of dicts with an "ep" key) and BASE as before.

import json
import os

# KP_DATA_BASE: directory holding the episode case dirs (and, by default, episodes.json)
BASE = os.environ.get("KP_DATA_BASE", "./data/different_types")

EPISODES_JSON = os.environ.get("EPISODES_JSON", os.path.join(BASE, "episodes.json"))
FIELDS = ("prompt", "label", "scene", "name", "interaction")
SCENES = ("1d", "2d", "3d")


def load_episodes(path=EPISODES_JSON):
    with open(path) as f:
        data = json.load(f)
    episodes, errors = [], []
    for ep, entry in data.items():
        missing = [k for k in FIELDS if not str(entry.get(k, "")).strip()]
        if missing:
            errors.append(f"{ep}: missing {missing}")
            continue
        if entry["scene"] not in SCENES:
            errors.append(f"{ep}: scene={entry['scene']!r} not in {SCENES}")
            continue
        episodes.append(dict(ep=ep, **{k: entry[k] for k in FIELDS}))
    if errors:
        raise SystemExit(f"[episodes] invalid entries in {path}:\n  " + "\n  ".join(errors))
    return episodes


EPISODES = load_episodes()
