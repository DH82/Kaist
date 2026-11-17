#!/usr/bin/env python3
"""Run sam_object_track.py / sam_object_point_track.py for several cameras in ONE process.

Each tracker main() builds its own SAM3.1 predictor (~8 s); per camera that was 4 model loads per episode.
This driver calls the unchanged tracker main() once per camera with the same command-line arguments, but
build_sam3_predictor returns one predictor built on first use. Every tracker opens and closes its own
sessions, so cameras do not share state. The tracker files themselves are left untouched (their sha256 is
pinned in bbox/point run identities).

Usage: sam_object_multi.py --tracker <tracker.py> --case <dest> --jobs '[{"cam":0,"out":"...","args":[...]}]'
A camera whose main() fails stops the run (same as the per-camera loop); finished cameras keep their
<out>/record.json so the caller can install them.
"""
import argparse
import importlib.util
import json
import sys
from pathlib import Path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--tracker', type=Path, required=True)
    ap.add_argument('--case', required=True)
    ap.add_argument('--jobs', required=True)
    a = ap.parse_args()
    jobs = json.loads(a.jobs)
    sys.path.insert(0, str(a.tracker.resolve().parent))
    import sam3.model_builder as builder
    original, cache = builder.build_sam3_predictor, {}

    def shared(*args, **kwargs):
        key = json.dumps([args, kwargs], sort_keys=True, default=str)
        if key not in cache:
            cache[key] = original(*args, **kwargs)
        return cache[key]
    builder.build_sam3_predictor = shared
    spec = importlib.util.spec_from_file_location('object_tracker', a.tracker)
    tracker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tracker)
    for job in jobs:
        sys.argv = [str(a.tracker), '--case', a.case, '--cam', str(job['cam']), *job['args'], '--out', job['out']]
        tracker.main()


if __name__ == '__main__':
    main()
