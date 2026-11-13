"""Runs under server Python, using the existing mask approval implementation."""
import argparse
import hashlib
import io
import json
from pathlib import Path
import sys
from PIL import Image
from contextlib import contextmanager
import fcntl
import time


@contextmanager
def episode_lock(project, ep, wait=5.0):
    """에피소드 단위 잠금. 원본 mask_review.lock()은 프로젝트 전체 잠금(99_history/pipeline.lock)이라
    다른 에피소드의 SAM3가 돌면 검수가 막혔습니다. 같은 에피소드의 SAM3(sam3_episode.py)와만 서로 막습니다."""
    folder = Path(project) / '99_history/locks'
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / f'{ep}.lock').open('a') as f:
        deadline = time.monotonic() + wait
        while True:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise ValueError('이 에피소드의 추론 또는 다른 작업이 실행 중입니다. 완료 후 검수하세요.')
                time.sleep(0.2)
        yield


def fingerprint(review, draft):
    value = review.snapshot([draft/'mask', draft/'basis.json'], draft)
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def context(config, episode):
    import re
    if not re.fullmatch(r'ep_[A-Za-z0-9_]+', episode):
        raise ValueError('Invalid episode')
    cfg = json.loads(Path(config).read_text())
    sys.path.insert(0, cfg['scripts_dir'])
    import mask_review as review
    project = Path(cfg['project_dir'])
    return review, project, episode


def inspect(review, project, ep):
    with episode_lock(project, ep):
        draft = review.prepare_draft(project, ep)
        d = review.root(project, ep)
        prepared = review.source(d)
        meta = json.loads((prepared/'metadata.json').read_text())
        return {'episode': ep, 'frames': meta['frame_num'], 'cameras': 4,
                'basis': fingerprint(review, draft),
                'approval': review.approval_summary(project, ep)}


def missing_masks(review, project, ep):
    d = review.root(project, ep)
    draft = d/'review/draft'
    meta = json.loads((review.source(d)/'metadata.json').read_text())
    return [(c, o, f) for c in range(4) for o in range(2) for f in range(meta['frame_num'])
            if not (draft/f'mask/{c}/{o}/{f}.png').exists()]


def fill_missing_masks(review, project, ep):
    """손에 완전히 가려지는 등 SAM3가 대상을 찾지 못한 프레임은 마스크 파일이 아예 없습니다.
    추적·QC·복원은 모든 프레임에 마스크 파일이 있다고 보므로, 승인 직전에 초안의 빈 자리를 빈 마스크(전부 0)로 채웁니다.
    파이프라인 코드(00_scripts)는 실행마다 해시로 고정되므로 바꾸지 않고, 검수 서버에서 데이터만 채웁니다.
    채운 프레임은 승인 기록의 exceptions에 'empty'로 남습니다(승인 사유 필요)."""
    d = review.root(project, ep)
    draft = d/'review/draft'
    width, height = json.loads((review.source(d)/'metadata.json').read_text())['WH']
    filled = []
    for cam, obj, frame in missing_masks(review, project, ep):
        path = draft/f'mask/{cam}/{obj}/{frame}.png'
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix('.tmp')
        Image.new('L', (width, height), 0).save(temp, format='PNG')
        temp.replace(path)
        filled.append([cam, obj, frame])
    if filled:
        edits = json.loads((draft/'edits.json').read_text())
        edits['events'].append({'filled_empty_masks': filled, 'reason': 'SAM3 미검출 프레임을 승인 전 빈 마스크로 채움',
                                'time': time.strftime('%Y-%m-%dT%H:%M:%S%z')})
        review.save(draft/'edits.json', edits)
    return filled


def decide(review, project, ep, data):
    with episode_lock(project, ep):
        draft = review.prepare_draft(project, ep)
        if not data.get('basis') or data['basis'] != fingerprint(review, draft):
            raise ValueError('검수 중 마스크가 변경됐습니다. 화면을 새로 열고 다시 확인하세요.')
        if review.approval_summary(project, ep)['status'] == 'approved':
            raise ValueError('이미 승인됐습니다. 상태를 새로 확인하세요.')
        name = str(data.get('operator', '')).strip()
        if not name:
            raise ValueError('로그인한 검수자 정보가 필요합니다.')
        note = str(data.get('note', ''))
        if data['decision'] == 'approve':
            checked = data.get('checked')
            if not isinstance(checked, list) or len(checked) != 4 or any(v is not True for v in checked):
                raise ValueError('4개 카메라 검수 완료를 모두 확인하세요.')
            # 채운 뒤 승인이 사유 없음으로 실패하면 초안만 바뀌어 다음 승인 시 기준값이 어긋나므로, 채우기 전에 먼저 확인합니다.
            if not note.strip() and missing_masks(review, project, ep):
                raise ValueError('마스크가 없는 프레임(SAM3 미검출)이 있습니다. 승인 사유를 검수 의견에 기록하세요. 예: 손에 가려 물체가 보이지 않음')
            filled = fill_missing_masks(review, project, ep)
            dest = review.approve(project, ep, name, checked, note)
            extra = f' 마스크가 없던 {len(filled)}개 프레임은 빈 마스크로 채웠습니다.' if filled else ''
            return {'status': 'approved', 'version': dest.name,
                    'message': '승인본을 저장했습니다.'+extra+' Airflow가 바로(몇 초 안에) 다음 단계로 진행합니다.'}
        if data['decision'] == 'hold':
            if not note.strip():
                raise ValueError('보류 사유를 입력하세요.')
            review.revoke(review.root(project, ep), '브라우저 검수 보류 · '+name+' · '+note)
            return {'status': 'held', 'message': '보류했습니다. 후속 단계는 진행하지 않습니다.'}
        raise ValueError('Invalid decision')


def image(review, project, ep, cam, frame, mode):
    # Read-only requests must not compete for the nonblocking writer lock.
    # Draft creation and all mutations remain locked in inspect()/decide().
    d = review.root(project, ep)
    draft = d/'review/draft'
    if not draft.is_dir():
        raise ValueError('먼저 검수 시작을 눌러 초안을 불러오세요.')
    review.require_basis(d, draft)
    prepared = review.source(d)
    n = json.loads((prepared/'metadata.json').read_text())['frame_num']
    if cam not in range(4) or frame not in range(n) or mode not in ('overlay','rgb','hand','object'):
        raise ValueError('Invalid image coordinates')
    rgb = Image.open(prepared/f'color/{cam}/{frame}.png').convert('RGB')
    result = rgb.copy()
    for obj, color in [(0, (40, 230, 90)), (1, (245, 70, 205))]:
        if mode == 'rgb' or mode == 'hand' and obj != 1 or mode == 'object' and obj != 0:
            continue
        p = draft/f'mask/{cam}/{obj}/{frame}.png'
        if p.exists():
            with Image.open(p) as im:
                mask = im.convert('L').point(lambda v: 100 if v else 0)
            result.paste(Image.new('RGB', result.size, color), (0, 0), mask)
    out = io.BytesIO();result.save(out, format='JPEG', quality=90)
    return out.getvalue()

def tracking_context(project, ep):
    import tracking_review
    from tracking_paths import episode_root
    return tracking_review, episode_root(project, ep)


def tracking_inspect(project, ep):
    tracking, case = tracking_context(project, ep)
    state = tracking.status(case)
    if not state['ready']:
        raise ValueError(state.get('reason', '추적 결과가 아직 검수 가능한 상태가 아닙니다.'))
    from tracking_qc import verified
    qc = verified(case, tracking.base_basis(case))
    from stage_refs import source
    meta = json.loads((source(case)/'metadata.json').read_text())
    report = json.loads((case/'review/tracking_report.json').read_text())
    import subprocess
    from fractions import Fraction
    rates=[]
    for group in ('cotracker','cotracker_hand'):
        probe=subprocess.run(['ffprobe','-v','error','-select_streams','v:0',
            '-show_entries','stream=avg_frame_rate,nb_frames','-of','json',
            str(case/'review'/f'{group}_review.mp4')],capture_output=True,text=True,check=True,timeout=10)
        stream=json.loads(probe.stdout)['streams'][0]
        if int(stream['nb_frames']) != int(meta['frame_num']):
            raise ValueError('검수 영상과 추적 프레임 수가 다릅니다.')
        rates.append(float(Fraction(stream['avg_frame_rate'])))
    if rates[0]<=0 or rates[0]!=rates[1]:raise ValueError('천·손 검수 영상의 재생 속도가 다릅니다.')
    return {'episode': ep, 'frames': meta['frame_num'], 'fps': rates[0],
            'basis': state['token'], 'approval': state,
            'qc_status': qc['status'] if qc else 'not_available',
            'events': qc['events'] if qc else [], 'has_qc': qc is not None,
            'errors': qc.get('errors', []) if qc else [], 'report': report}


def tracking_decide(review, project, ep, data):
    with episode_lock(project, ep):
        tracking, case = tracking_context(project, ep)
        current = tracking_inspect(project, ep)
        if current['approval']['status'] == 'approved':
            raise ValueError('이미 승인됐습니다. 상태를 새로 확인하세요.')
        if not data.get('basis') or data['basis'] != current['basis']:
            raise ValueError('검수 중 추적 결과 또는 입력이 변경됐습니다. 다시 불러오세요.')
        decision = data.get('decision')
        views = data.get('checked_views', [])
        if decision == 'approve' and (len(views) != 8 or any(v is not True for v in views)):
            raise ValueError('천·손 각각 4개 카메라의 추적 검수 완료를 확인하세요.')
        # Legacy review expects anomaly checks when QC exists; otherwise eight views.
        checked = data.get('checked', []) if current['has_qc'] else views
        result = tracking.decide(case, {'approve':'approved','hold':'held'}.get(decision, ''),
                                 str(data.get('operator', '')), checked, str(data.get('note', '')),
                                 data['basis'], mode='human')
        return {'status': result['status'], 'decision_id': result['id'],
                'message': '추적 승인 완료. Airflow가 바로(몇 초 안에) 복원 단계로 진행합니다.'
                           if result['status']=='approved' else '추적 결과를 보류했습니다. 후속 단계는 진행하지 않습니다.'}


def tracking_video(project, ep, group):
    if group not in ('cotracker', 'cotracker_hand'):
        raise ValueError('Invalid tracking group')
    tracking, case = tracking_context(project, ep)
    state = tracking.status(case)
    if not state['ready']:
        raise ValueError(state.get('reason', '추적 결과를 확인할 수 없습니다.'))
    path = case/'review'/f'{group}_review.mp4'
    if not path.is_file():
        raise ValueError('추적 검수 영상이 없습니다.')
    return {'path': str(path.resolve())}


def queue_items(review, project):
    items = []
    for prepared in sorted((project/'03_prepared').glob('ep_*')):
        ep = prepared.name
        if not (prepared/'metadata.json').is_file():
            continue
        frames = json.loads((prepared/'metadata.json').read_text())['frame_num']
        d = review.root(project, ep)
        if (d/'state.json').is_file():
            state = json.loads((d/'state.json').read_text())
            if state.get('status') == 'mask_review_pending':
                approval = review.approval_summary(project, ep)
                items.append({'episode':ep, 'stage':'mask', 'frames':frames,
                              'status':approval['status'], 'ready':True,
                              'label':'마스크 검수', 'events':0})
        tracking, case = tracking_context(project, ep)
        if case.is_dir() and (case/'review/tracking_report.json').is_file():
            state = tracking.status(case)
            qc_path = case/'qc/report.json'
            qc = json.loads(qc_path.read_text()) if qc_path.exists() else {}
            items.append({'episode':ep, 'stage':'tracking', 'frames':frames,
                          'status':state['status'], 'ready':state['ready'],
                          'reason':state.get('reason', ''), 'label':'추적 검수',
                          'events':len(qc.get('events', []))})
    return {'reviews': items}


if __name__ == '__main__':
    p = argparse.ArgumentParser();p.add_argument('action', choices=['inspect','image','decision','queue','tracking_inspect','tracking_decision','tracking_video'])
    p.add_argument('--config',required=True);p.add_argument('--episode',required=True)
    p.add_argument('--cam',type=int);p.add_argument('--frame',type=int);p.add_argument('--mode',default='overlay');p.add_argument('--group')
    a = p.parse_args()
    try:
        review, project, ep = context(a.config,a.episode)
        if a.action == 'queue':
            print(json.dumps(queue_items(review,project),ensure_ascii=False))
        elif a.action == 'tracking_inspect':
            print(json.dumps(tracking_inspect(project,ep),ensure_ascii=False))
        elif a.action == 'tracking_decision':
            print(json.dumps(tracking_decide(review,project,ep,json.load(sys.stdin)),ensure_ascii=False))
        elif a.action == 'tracking_video':
            print(json.dumps(tracking_video(project,ep,a.group)))
        elif a.action == 'image':
            sys.stdout.buffer.write(image(review,project,ep,a.cam,a.frame,a.mode))
        else:
            result = inspect(review,project,ep) if a.action == 'inspect' else decide(review,project,ep,json.load(sys.stdin))
            print(json.dumps(result,ensure_ascii=False))
    except Exception as ex:
        print(str(ex), file=sys.stderr);sys.exit(1)
