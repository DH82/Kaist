"""TEST OPTION: reuse SAM3's image-encoder (ViT trunk) output across prompts on the same video.

Why: SAM3's text-prompt path resets the whole session state per prompt ("semantic prompt: start over",
feature_cache.clear()), so the image backbone re-encodes every frame for hand, forearm, object...
Measured on a 98-frame camera: the backbone is ~70 % of one prompt's propagation (8.0 s of 11.3 s).

How: Sam3Image._get_img_feats() runs backbone.forward_image(frames) -> Sam3TriViTDetNeck.forward(),
which calls the ViT trunk (the expensive part) and then three small conv necks. The trunk output
depends only on the frame pixels, so it is cached on the GPU per (loaded video, frame ids) and handed
to the unchanged neck on the next prompt. The necks, text encoder, detector, tracker and
post-processing all run exactly as before, on bit-identical inputs.
  * cache is on the GPU (~10 MB/frame), one video at a time, cleared when another video is loaded;
  * stops adding entries above KAIST_SAM_FEATURE_CACHE_GB (default 4) or when < 2 GB GPU memory is
    free; uncached chunks are simply recomputed.
(A first version cached the full neck outputs in CPU RAM: ~160 MB/frame, the transfers ate most of
the gain.)
Off unless install() is called (sam3_fast decides; see sam3_fast.feature_reuse_enabled()).
"""
import os
import sys

_STATE = {'video': None, 'key': None, 'entries': {}, 'bytes': 0, 'hits': 0, 'misses': 0, 'skipped': 0,
          'full': False}


def install(cap_gb=None):
    """Patch Sam3Image._get_img_feats and Sam3TriViTDetNeck.forward in this process (idempotent)."""
    import torch
    from torch import nn
    from sam3.model import necks, sam3_image
    image_cls, neck_cls = sam3_image.Sam3Image, necks.Sam3TriViTDetNeck
    if getattr(image_cls, '_kaist_feature_reuse', False):
        return
    cap = float(cap_gb if cap_gb is not None else os.environ.get('KAIST_SAM_FEATURE_CACHE_GB', '4')) * 2 ** 30
    original_feats, original_neck = image_cls._get_img_feats, neck_cls.forward

    class Cached(nn.Module):
        """Stands in for the trunk for one call and returns the cached trunk output."""
        def __init__(self, xs):
            super().__init__()
            self.xs = xs

        def forward(self, *args, **kwargs):
            return self.xs

    def _get_img_feats(self, backbone_out, img_ids):
        if 'backbone_fpn' in backbone_out:
            return original_feats(self, backbone_out, img_ids)
        img_batch = backbone_out['img_batch_all_stages']
        if _STATE['video'] is not img_batch:
            # Another video was loaded (new session / chunk): drop everything from the previous one.
            _STATE.update(video=img_batch, entries={}, bytes=0, full=False)
        unique_ids = torch.unique(img_ids) if img_ids.numel() > 1 else img_ids
        _STATE['key'] = tuple(int(i) for i in unique_ids.reshape(-1).tolist())
        try:
            return original_feats(self, backbone_out, img_ids)   # -> forward_image -> neck forward below
        finally:
            _STATE['key'] = None

    def forward(self, tensor_list, **kwargs):
        key = _STATE['key']
        if key is None:
            return original_neck(self, tensor_list, **kwargs)
        hit = _STATE['entries'].get(key)
        if hit is None:
            xs = self.trunk(tensor_list)
            _STATE['misses'] += 1
            size = sum(getattr(x, 'tensors', x).numel() * getattr(x, 'tensors', x).element_size() for x in xs)
            if not _STATE['full'] and (_STATE['bytes'] + size > cap
                                       or torch.cuda.mem_get_info()[0] < 2 * 2 ** 30):
                _STATE['full'] = True
                print(f'[특징 재사용] 캐시 상한 도달({_STATE["bytes"] / 2 ** 30:.1f} GB) — 이후 구간은 다시 계산합니다',
                      flush=True)
            if _STATE['full']:
                _STATE['skipped'] += 1
            else:
                _STATE['entries'][key] = xs
                _STATE['bytes'] += size
        else:
            xs = hit
            _STATE['hits'] += 1
        trunk = self._modules['trunk']
        self._modules['trunk'] = Cached(xs)
        try:
            return original_neck(self, tensor_list, **kwargs)
        finally:
            self._modules['trunk'] = trunk

    image_cls._get_img_feats = _get_img_feats
    neck_cls.forward = forward
    image_cls._kaist_feature_reuse = True
    import atexit
    atexit.register(report)
    print(f'[특징 재사용] 테스트 옵션 켜짐 — 같은 영상의 이미지 인코더 출력을 프롬프트 사이에 재사용합니다 '
          f'(GPU 캐시 상한 {cap / 2 ** 30:.0f} GB)', flush=True)


def report():
    s = _STATE
    print(f'[특징 재사용] 재사용 {s["hits"]}회 · 새로 계산 {s["misses"]}회 · 캐시 생략 {s["skipped"]}회 · '
          f'마지막 캐시 {s["bytes"] / 2 ** 30:.2f} GB', file=sys.stdout, flush=True)
