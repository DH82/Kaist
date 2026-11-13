"""SAM3.1 마법봉: 검수 화면의 클릭 한 번으로 한 프레임의 손/물체 마스크를 만듭니다.

sam-env Python에서 상주하며 표준입력으로 JSON 요청을 한 줄씩 받고, 표준출력으로 JSON 응답을 한 줄씩 냅니다.
GPU 잠금과 유휴 종료는 이 프로세스를 띄운 worker_service가 관리합니다.

요청: {"image": "<RGB png 경로>", "points": [[x, y, label], ...], "text": "cloth" | "", "threshold": 0.5}
  - x, y는 0~1 정규화 좌표, label 1=포함(양성) 0=제외(음성)
  - text가 있으면 SAM3 개념(텍스트) 검출로 같은 종류의 물체를 모두 찾은 뒤, 첫 양성 클릭이 들어간 물체를 고르고
    나머지 클릭으로 그 물체를 다듬습니다. 클릭 위치에 해당 물체가 없으면 클릭만으로 분할합니다.
응답: {"ok": true, "mask_png": base64, "width", "height", "method", "score", "instances", "message"}
"""
import base64, io, json, os, sys, time, traceback

# 응답은 원래 표준출력으로만 보내고, 라이브러리가 찍는 로그(print 포함)는 모두 표준오류로 돌립니다.
PROTO = os.fdopen(os.dup(1), 'w', buffering=1)
os.dup2(2, 1)
sys.stdout = sys.stderr

import numpy as np
from PIL import Image


def log(*a):
    print('[wand]', *a, file=sys.stderr, flush=True)


log('loading SAM3.1 predictor…')
t0 = time.monotonic()
import torch
from sam3.model_builder import build_sam3_predictor

PREDICTOR = build_sam3_predictor(version='sam3.1', use_fa3=False)
log(f'ready in {time.monotonic() - t0:.1f}s')
SESSIONS = {}  # image path -> session id (가장 최근 몇 개만 유지)
MAX_SESSIONS = 4
CLICK_OBJ = 1000  # 클릭 전용 물체 번호 (텍스트 검출 번호와 겹치지 않게)


def session_for(path):
    sid = SESSIONS.pop(path, None)
    if sid is None:
        sid = PREDICTOR.handle_request(request=dict(type='start_session', resource_path=path,
                                                    offload_video_to_cpu=True))['session_id']
    SESSIONS[path] = sid  # 최근 사용 순서로 다시 넣음
    while len(SESSIONS) > MAX_SESSIONS:
        old_path, old = next(iter(SESSIONS.items()))
        SESSIONS.pop(old_path)
        try:
            PREDICTOR.handle_request(request=dict(type='close_session', session_id=old))
        except Exception:
            pass
    return sid


def reset(sid):
    PREDICTOR.handle_request(request=dict(type='reset_session', session_id=sid))


def masks_of(outputs):
    """add_prompt 결과 -> [(obj_id, bool mask, score)]"""
    result = []
    ids = list(outputs.get('out_obj_ids', []))
    probs = outputs.get('out_probs')
    for i, oid in enumerate(ids):
        m = np.asarray(outputs['out_binary_masks'][i]).astype(bool)
        if m.ndim == 3:
            m = m[0]
        score = float(np.asarray(probs[i]).reshape(-1)[0]) if probs is not None and len(probs) > i else None
        result.append((int(oid), m, score))
    return result


def add(sid, **kw):
    return PREDICTOR.handle_request(request=dict(type='add_prompt', session_id=sid, frame_index=0, **kw))['outputs']


def segment(req):
    path = req['image']
    if not os.path.isfile(path):
        raise ValueError('원본 RGB 프레임이 없습니다.')
    pts = req.get('points') or []
    if not pts or not any(int(p[2]) == 1 for p in pts):
        raise ValueError('포함할 위치를 한 번 이상 클릭하세요.')
    xy = [[min(1.0, max(0.0, float(p[0]))), min(1.0, max(0.0, float(p[1])))] for p in pts]
    labels = [1 if int(p[2]) == 1 else 0 for p in pts]
    text = str(req.get('text') or '').strip()[:120]
    thr = float(req.get('threshold') or 0.5)
    with Image.open(path) as im:
        W, H = im.size
    first = next(i for i, l in enumerate(labels) if l == 1)
    px, py = min(W - 1, int(xy[first][0] * W)), min(H - 1, int(xy[first][1] * H))
    sid = session_for(path)
    method, mask, score, instances, message = None, None, None, 0, ''

    if text:
        reset(sid)
        found = masks_of(add(sid, text=text, output_prob_thresh=thr))
        instances = len(found)
        hit = [f for f in found if f[1].shape == (H, W) and f[1][py, px]]
        if hit:
            oid, mask, score = max(hit, key=lambda f: (f[2] or 0))
            method = 'text'
            message = f'"{text}" 검출 {instances}개 중 클릭한 물체를 선택했습니다.'
            if len(pts) > 1:
                # 같은 물체(obj_id)에 클릭을 더해 경계를 다듬습니다.
                try:
                    refined = [f for f in masks_of(add(sid, points=xy, point_labels=labels, obj_id=oid))
                               if f[0] == oid]
                    if refined and refined[0][1].any():
                        mask, method = refined[0][1], 'text+points'
                        message += ' 추가 클릭으로 다듬었습니다.'
                except Exception as e:  # 다듬기 실패 시 텍스트 결과를 그대로 씁니다.
                    log('refine failed:', e)
                    message += ' (클릭 다듬기는 실패해 이름 검출 결과를 사용)'
        else:
            message = (f'"{text}"(으)로 {instances}개를 찾았지만 클릭 위치에 없어 클릭만으로 분할했습니다.'
                       if instances else f'"{text}"(으)로 찾지 못해 클릭만으로 분할했습니다.')

    if mask is None:
        # SAM3.1 다중 물체 모델은 텍스트 검출을 한 번 거친 세션에서만 점 프롬프트를 받습니다.
        # 이름이 없으면 검출 결과와 무관한 일반어로 세션을 초기화하고, 새 물체 번호로 클릭만 사용합니다.
        if not text:
            reset(sid)
            add(sid, text='object', output_prob_thresh=thr)
        got = [f for f in masks_of(add(sid, points=xy, point_labels=labels, obj_id=CLICK_OBJ))
               if f[0] == CLICK_OBJ and f[1].shape == (H, W)]
        if not got or not got[0][1].any():
            raise ValueError('클릭한 위치에서 영역을 찾지 못했습니다. 다른 위치를 클릭해 보세요.')
        _, mask, score = max(got, key=lambda f: f[1].sum())
        method = 'points'
        message = message or '클릭 위치로 분할했습니다.'

    out = io.BytesIO()
    Image.fromarray((mask.astype(np.uint8) * 255)).save(out, format='PNG')
    return {'ok': True, 'mask_png': base64.b64encode(out.getvalue()).decode(), 'width': W, 'height': H,
            'method': method, 'score': score, 'instances': instances, 'area': int(mask.sum()), 'message': message}


PROTO.write(json.dumps({'ready': True}) + '\n'); PROTO.flush()
for line in sys.stdin:
    if not line.strip():
        continue
    try:
        with torch.inference_mode():
            resp = segment(json.loads(line))
    except Exception as e:
        traceback.print_exc(file=sys.stderr)
        resp = {'ok': False, 'error': str(e)}
    if torch.cuda.is_available():
        torch.cuda.empty_cache()
    PROTO.write(json.dumps(resp) + '\n'); PROTO.flush()
