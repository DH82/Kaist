#!/usr/bin/env python3
"""00_scripts/run_sam_progress.py + the image-feature reuse test option (sam_feature_reuse.py).

Usage: sam_run_reuse.py <00_scripts> <segment_object_sam3.py> [its arguments...]
Same as run_sam_progress.py <segment_object_sam3.py> ..., with the backbone cache installed first.
"""
import importlib.util
import sys
from pathlib import Path


def main():
    scripts = Path(sys.argv.pop(1))
    path = sys.argv.pop(1)
    sys.path.insert(0, str(scripts))
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from sam_progress import install
    import sam_feature_reuse
    spec = importlib.util.spec_from_file_location('baseline_sam', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    install(module)
    sam_feature_reuse.install()
    module.main()


if __name__ == '__main__':
    main()
