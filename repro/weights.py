"""Resolve independently located model assets without copying them into the repo."""
from pathlib import Path

# key, Compose variable, path relative to a legacy workspace, description
ASSETS = [
    ('hf-hub', 'WEIGHT_HF_HUB', 'hf-cache/hub', 'SAM3.1 / TRELLIS / DINOv2 Hugging Face hub cache'),
    ('torch-hub', 'WEIGHT_TORCH_HUB', 'runtime-cache/torch/hub', 'CoTracker checkpoint and DINOv2 torch hub cache'),
    ('vitdet', 'WEIGHT_VITDET', 'runtime-cache/iopath', 'ViTDet iopath cache'),
    ('mano', 'WEIGHT_MANO', 'kaist-full/PhysTwin/Mono/data_process/Hamba/_DATA/data/mano', 'MANO_LEFT.pkl and MANO_RIGHT.pkl directory'),
    ('mano-mean', 'WEIGHT_MANO_MEAN', 'kaist-full/PhysTwin/Mono/data_process/Hamba/_DATA/data/mano_mean_params.npz', 'mano_mean_params.npz file'),
    ('vitpose', 'WEIGHT_VITPOSE', 'kaist-full/PhysTwin/Mono/data_process/Hamba/_DATA/vitpose_ckpts', 'ViTPose checkpoint directory'),
    ('hamba', 'WEIGHT_HAMBA', 'kaist-full/PhysTwin/Mono/data_process/Hamba/ckpts/hamba/checkpoints', 'directory containing hamba.ckpt'),
    ('superglue', 'WEIGHT_SUPERGLUE', 'kaist-full/PhysTwin/data_process/models/weights', 'SuperPoint / SuperGlue weights directory'),
    ('mano-faces', 'WEIGHT_MANO_FACES', 'kaist-full/PhysTwin/data/mano_faces.pkl', 'mano_faces.pkl file'),
    ('mano-faces-mono', 'WEIGHT_MANO_FACES_MONO', 'kaist-full/PhysTwin/Mono/data/mano_faces.pkl', 'Mono mano_faces.pkl file (normally same as mano-faces)'),
]


def resolve_weights(root, values, overrides=(), interactive=False):
    supplied = {}
    keys = {item[0] for item in ASSETS}
    for item in overrides:
        key, separator, value = item.partition('=')
        if not separator or key not in keys or not value:
            raise ValueError('--weight expects NAME=PATH. Names: ' + ', '.join(sorted(keys)))
        supplied[key] = value
    resolved, missing = {}, []
    for key, variable, relative, description in ASSETS:
        value = supplied.get(key) or values.get(variable)
        if key == 'mano-faces-mono' and not value:
            value = supplied.get('mano-faces') or (resolved.get('WEIGHT_MANO_FACES') if root is None else None)
        if not value and root is not None:
            value = str(root / relative)
        path = Path(value).expanduser().resolve() if value else None
        if (path is None or not path.exists()) and interactive:
            answer = input(f'{key} — {description}\n경로: ').strip()
            path = Path(answer).expanduser().resolve() if answer else None
        is_file = relative.endswith(('.pkl', '.npz'))
        if path is None or not (path.is_file() if is_file else path.is_dir()):
            missing.append(f'{key}: {description}')
        else:
            resolved[variable] = str(path)
    if missing:
        raise ValueError('Missing weights. Set --weights ROOT or --weight NAME=PATH:\n' + '\n'.join(missing))
    return resolved
