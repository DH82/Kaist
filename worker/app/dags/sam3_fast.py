"""Faster equivalent of 00_scripts/pipeline.sam3() (SAM3 baseline + hand ROI), kept in the adapter.

Why here: every .py in 00_scripts is pinned into each run's identity.json, so editing pipeline.py
would fail every in-flight/MANO-waiting run. This module is not pinned.

What differs from pipeline.sam3():
  A. One SAM3 process per phase instead of one per camera (ROI phase via sam_roi_fast.py, which also
     prepares the next camera's ROI video while SAM runs; baseline previews run during the ROI phase). The model is loaded twice per episode
     instead of 8 times (~8-17 s each). Per-camera SAM sessions are unchanged, so masks are the same.
     Receipts are still written per camera (baseline_camera_N / roi_camera_N, same format), so
     resume and sam_completion work as before. Trade-off: a crash mid-phase reruns that phase's
     unfinished cameras together instead of one camera.
  C. object_tracked=True (bbox_track/point_track): the baseline runs only the hand group
     (--groups controller). The baseline text-object mask is never reviewed in those modes; the
     review source roi_candidate/mask/<cam>/0 is replaced by the object tracker afterwards.
     Hand masks are computed in their own SAM session reset, so they do not depend on the object.

  H. hand_points (손 포인트 방식, object_tracked와 함께만): 텍스트 기준선과 손 ROI 확대를 하지 않습니다.
     '기준선' 구간은 sam_hand_point_track.py가 첫 프레임 포인트에서 손을 추적해 mask/<cam>/1 을 만들고,
     'ROI' 구간은 그 손 마스크를 후보(roi_candidate/mask/<cam>/1)로 그대로 둡니다. 구간 이름·영수증·미리보기·
     완료 검사는 그대로라 재개와 검수 화면이 같은 방식으로 동작합니다.

If any of the pipeline files this mirrors changed (PINNED), compatible() is False and the caller
falls back to the original pipeline.sam3().
"""
import hashlib
import json
import os
import re
import shutil
import tempfile
import time
from pathlib import Path

PINNED = {
    'pipeline.py': 'b86800764bbc0a7c21196f663bb3271638d0d509aa55819ae9466c47f04795e6',
    'roi_workflow.py': 'a4f13cf4bdde04e90a22bb7130d487c5e507b9bbf5afcb2885e54b71d5071e4f',
    'roi_camera.py': 'dbce8ec5277fd90bc6a7f133f88270ff76372202ad43bfeed30d4ef026677ac9',
    'run_hand_roi.py': '19c631afc30d4c678f4948832c962e88013ec51517df16fd5d8020a5cf9e4a87',
    'resume_steps.py': '5975313d18cd2aa005d03141644372b9d37ce68e02a44136029be8c70ac9114d',
    'sam_completion.py': 'cfb6a4577cc39e8789e09489e632bf6886d8ce97fdfa3814b2ff9717983aa2a9',
    'episode_progress.py': '0b98e1a30ff91c2bb643b505cf3406070a5d0427ebd68d536fbc15bf5f4e3630',
    'stage_refs.py': 'af88acec1287af48fb2051286cba8df980c736f9a337300061835e6c3586647d',
    'run_sam_progress.py': '5889eccd44e6702a41e0c00a5351d768f9ccd3ab2f956cd3a31f6bf4af6f5e25',
}
SOURCE_SAM = ('data_process/segment_object_sam3.py',
              '0a6f63d107a10cafaa67c9156f04a3e42abd7678b52fbe614622b6906a5dec7e')
CAMERAS = range(4)
GROUPS_FILE = 'baseline_groups.json'   # _resume/<this>: {cam: 'both'|'controller'|'points:<hash>'}
HAND_TRACKER = Path(__file__).resolve().with_name('sam_hand_point_track.py')


def hand_tag(points):
    """손 포인트 기준선의 표시. 포인트가 바뀌면 값이 달라져 그 기준선을 다시 계산합니다."""
    return 'points:' + hashlib.sha256(json.dumps(points, sort_keys=True).encode()).hexdigest()[:16]


def is_hand_points(group):
    return isinstance(group, str) and group.startswith('points:')
# TEST OPTION (sam_feature_reuse.py): on while this file exists on the worker, or KAIST_SAM_FEATURE_REUSE=1.
FEATURE_REUSE_FLAG = Path(__file__).resolve().parents[1] / 'config' / 'sam_feature_reuse.on'


def feature_reuse_enabled():
    env = os.environ.get('KAIST_SAM_FEATURE_REUSE')
    if env is not None:
        return env == '1'
    return FEATURE_REUSE_FLAG.is_file()


def digest(p):
    with Path(p).open('rb') as f:
        return hashlib.file_digest(f, 'sha256').hexdigest()


def compatible(scripts, source):
    """True only if the mirrored pipeline code is exactly the version this module was written for."""
    try:
        return (all(digest(Path(scripts) / n) == h for n, h in PINNED.items())
                and digest(Path(source) / SOURCE_SAM[0]) == SOURCE_SAM[1])
    except OSError:
        return False


def _valid(dest, name, outputs):
    """Same check resume_steps.step() does before skipping a stage."""
    from resume_steps import snapshot
    receipt = dest / '_resume' / f'{name}.json'
    try:
        return json.loads(receipt.read_text())['files'] == snapshot([dest / o for o in outputs], dest)
    except (OSError, ValueError, KeyError):
        return False


def _write_receipt(dest, name, outputs, seconds):
    """Same receipt format as resume_steps.step()."""
    from resume_steps import snapshot
    receipt = dest / '_resume' / f'{name}.json'
    receipt.parent.mkdir(exist_ok=True)
    tmp = receipt.with_suffix('.tmp')
    tmp.write_text(json.dumps({'files': snapshot([dest / o for o in outputs], dest), 'seconds': seconds}, indent=2))
    tmp.replace(receipt)


def _batched(dest, kind, outputs_of, run, skip_invalid=lambda cam: False):
    """Resume per camera like step(), but run every unfinished camera in one SAM process."""
    import episode_progress
    from resume_steps import preserve
    progress = episode_progress.CURRENT
    pending = []
    for cam in CAMERAS:
        name = f'{kind}_camera_{cam}'
        if not skip_invalid(cam) and _valid(dest, name, outputs_of(cam)):
            if progress:
                progress.finish(name, json.loads((dest / '_resume' / f'{name}.json').read_text()).get('seconds'))
            print('[재개] 완료 구간 건너뜀: ' + name, flush=True)
        else:
            pending.append(cam)
    if not pending:
        return pending
    for cam in pending:
        preserve(dest / '_resume' / f'{kind}_camera_{cam}.json', dest)
    names = ', '.join(f'{kind}_camera_{c}' for c in pending)
    print(f'[실행] {names} (모델 1회 로드, 카메라 {len(pending)}대 연속 처리)', flush=True)
    started = time.monotonic()
    # The ETA (episode_progress) learns per-camera durations. One process covers every camera, so
    # follow SAM_PROGRESS's video path (…/color/<cam>.mp4 or …/roi_camera_<cam>/input.mp4) to see
    # when the next camera starts, instead of reporting one long stage at the end.
    seen = {'cam': pending[0], 't': started}
    seconds = {}
    if progress:
        progress.begin(f'{kind}_camera_{pending[0]}')
        original_update = progress.update

        def update(data):
            m = re.search(r'(?:/color/|/roi_camera_)(\d+)(?:\.mp4|/input\.mp4)$', str(data.get('video', '')))
            if m and int(m[1]) in pending and int(m[1]) != seen['cam']:
                now = time.monotonic()
                seconds[seen['cam']] = now - seen['t']
                progress.finish(f'{kind}_camera_{seen["cam"]}', seconds[seen['cam']])
                seen.update(cam=int(m[1]), t=now)
                progress.begin(f'{kind}_camera_{seen["cam"]}')
            original_update(data)
        progress.update = update
    try:
        run(pending)
    finally:
        if progress:
            del progress.update   # back to the class method
    now = time.monotonic()
    seconds[seen['cam']] = now - seen['t']
    share = (now - started) / len(pending)
    for cam in pending:
        name = f'{kind}_camera_{cam}'
        _write_receipt(dest, name, outputs_of(cam), seconds.get(cam, share))
        if progress and name not in progress.done:
            progress.finish(name, seconds.get(cam, share))
    return pending


def _baseline(pipeline, dest, ep, settings, S, source, env, object_tracked, hand_points=None):
    from resume_steps import preserve
    groups_path = dest / '_resume' / GROUPS_FILE
    try:
        groups = json.loads(groups_path.read_text())
    except (OSError, ValueError):
        groups = {}
    group = hand_tag(hand_points) if hand_points else 'controller' if object_tracked else 'both'
    here = pipeline.HERE

    def run(cams):
        # Same invalidation as pipeline.sam3()'s per-camera baseline().
        for receipt in (dest / '_resume').glob('*.json'):
            if receipt.stem.startswith('roi_') or receipt.stem == 'baseline_preview':
                preserve(receipt, dest)
        for cam in cams:
            preserve(dest / f'mask/{cam}', dest)
        if hand_points:
            _hand_track(pipeline, dest, S, source, env, cams, hand_points)
            for cam in cams:
                groups[str(cam)] = group
            tmp = groups_path.with_suffix('.tmp')
            tmp.write_text(json.dumps(groups, indent=2))
            tmp.replace(groups_path)
            return
        runner = ([str(Path(__file__).with_name('sam_run_reuse.py')), str(here)] if feature_reuse_enabled()
                  else [str(here / 'run_sam_progress.py')])
        pipeline.execute([S, *runner, str(source / SOURCE_SAM[0]),
                          '--base_path', str(dest.parent), '--case_name', ep,
                          '--object_prompts', settings['prompt'], '--object_label', settings['label'],
                          '--scene_type', settings['scene'], '--version', 'sam3.1', '--chunk_frames', '300',
                          '--grounding_batch_size', '4', '--groups', group,
                          '--cameras', *map(str, cams)], dest / '05.log', source, env)
        for cam in cams:
            (dest / f'mask/{cam}').mkdir(parents=True, exist_ok=True)
            groups[str(cam)] = group
        tmp = groups_path.with_suffix('.tmp')
        tmp.write_text(json.dumps(groups, indent=2))
        tmp.replace(groups_path)

    # A hand-only baseline (earlier bbox/point run) cannot be reused by a text-mode run:
    # there the baseline object mask is the reviewed object mask.
    # 손 포인트 기준선은 같은 포인트일 때만 다시 씁니다. 텍스트 손 방식은 손 포인트 기준선을 다시 쓰지 않습니다.
    def stale(cam):
        have = groups.get(str(cam))
        if hand_points:
            return have != group
        return is_hand_points(have) or (not object_tracked and have == 'controller')
    _batched(dest, 'baseline', lambda cam: [f'mask/{cam}', f'mask/mask_info_{cam}.json'], run, skip_invalid=stale)
    return all(groups.get(str(c)) == 'controller' or is_hand_points(groups.get(str(c))) for c in CAMERAS)


def _hand_track(pipeline, dest, S, source, env, cams, hand_points):
    """손 포인트 추적(카메라 여러 대, 모델 1회 로드) → mask/<cam>/1, mask_info, hand_track/<cam>(기록·초기 마스크 그림)."""
    from resume_steps import preserve
    with tempfile.TemporaryDirectory(prefix='.hand-track-', dir=dest) as tmp:
        jobs = [{'cam': cam, 'points': hand_points[str(cam)], 'out': str(Path(tmp) / f'cam{cam}')} for cam in cams]
        pipeline.execute([S, str(HAND_TRACKER), '--case', str(dest), '--jobs', json.dumps(jobs)],
                         dest / '05.log', source, env)
        for cam in cams:
            work = Path(tmp) / f'cam{cam}'
            if not (work / 'record.json').is_file():
                raise ValueError(f'손 포인트 추적 결과가 없습니다: camera {cam}')
            target = dest / f'mask/{cam}'
            target.mkdir(parents=True, exist_ok=True)
            shutil.move(str(work / '1'), str(target / '1'))
            home = dest / 'hand_track' / str(cam)
            if home.exists():
                preserve(home, dest)
            home.parent.mkdir(exist_ok=True)
            shutil.move(str(work), str(home))
            (dest / f'mask/mask_info_{cam}.json').write_text(json.dumps({'1': 'hand'}))


def _roi(pipeline, dest, S, P, source, env, hand_only, hand_points=None):
    """roi_workflow.run() with one ROI process for all unfinished cameras."""
    from resume_steps import preserve, step
    from stage_refs import inputs, link, source as input_source
    here = pipeline.HERE
    candidate = dest / 'roi_candidate'
    bundle = here / 'sam_roi_bundle'
    candidate.mkdir(exist_ok=True)

    def run(cams):
        preserve(dest / '_resume/roi_preview.json', dest)
        with tempfile.TemporaryDirectory(prefix='wfm-roi-') as temp:
            work = Path(temp) / 'candidate'
            # sam_roi_fast.py = run_hand_roi.py/roi_camera.py with the next camera's ROI video prepared
            # while SAM runs (identical frames; see its docstring).
            pipeline.execute([S, str(Path(__file__).with_name('sam_roi_fast.py')), '--scripts', str(here),
                              '--source-script', str(source / SOURCE_SAM[0]),
                              '--case', str(dest), '--out', str(work),
                              '--profile', str(bundle / 'configs/sam_roi_candidate_v1.json'),
                              '--cameras', *map(str, cams),
                              *(['--feature-reuse'] if feature_reuse_enabled() else [])], dest / '05_roi.log', source, env)
            data = json.loads((work / 'candidate_record.json').read_text())
            if sorted(r['camera'] for r in data['results']) != sorted(cams):
                raise ValueError('Incomplete camera record')
            for result in data['results']:
                cam = result['camera']
                target = candidate / 'mask' / str(cam)
                preserve(target, dest)
                target.parent.mkdir(exist_ok=True)
                if (work / 'mask' / str(cam)).exists():
                    shutil.move(str(work / 'mask' / str(cam)), target)
                else:
                    target.mkdir()
                (candidate / f'camera_{cam}.json').write_text(json.dumps({**data, 'results': [result]}))
                object_link = target / '0'
                if object_link.is_symlink():
                    object_link.unlink()
                if (dest / f'mask/{cam}/0').exists():
                    link(object_link, dest / f'mask/{cam}/0')

    def run_points(cams):
        """손 포인트 방식: ROI 확대 없이, 추적한 손 마스크를 후보로 그대로 둡니다."""
        preserve(dest / '_resume/roi_preview.json', dest)
        for cam in cams:
            target = candidate / 'mask' / str(cam)
            preserve(target, dest)
            target.mkdir(parents=True)
            shutil.copytree(dest / f'mask/{cam}/1', target / '1')
            track = json.loads((dest / 'hand_track' / str(cam) / 'record.json').read_text())
            (candidate / f'camera_{cam}.json').write_text(json.dumps({
                'profile': {'profile_id': 'hand_point_track_v1', 'model_version': 'sam3.1', 'hand_prompts': ['hand'],
                            'notes': 'Hands tracked from first-frame point seeds. No text baseline, no ROI enlargement, '
                                     'no forearm subtraction.'},
                'human_approved': False, 'candidate_only': True,
                'note': 'Hand masks come from first-frame point tracking; the candidate equals the tracked hand mask.',
                'results': [{'camera': cam, 'mode': 'hand_point_track', 'hands': len(track['seeds']),
                             'nonempty_frames': track['nonempty_frames'], 'frames': []}]}, ensure_ascii=False))
            if (dest / f'mask/{cam}/0').exists():
                link(target / '0', dest / f'mask/{cam}/0')

    _batched(dest, 'roi', lambda cam: [f'roi_candidate/mask/{cam}', f'roi_candidate/camera_{cam}.json'],
             run_points if hand_points else run)

    # Unchanged tail of roi_workflow.run().
    records = [json.loads((candidate / f'camera_{c}.json').read_text()) for c in CAMERAS]
    combined = dict(records[0])
    combined['results'] = [r['results'][0] for r in records]
    (candidate / 'candidate_record.json').write_text(json.dumps(combined))
    record = json.loads((candidate / 'candidate_record.json').read_text())
    if len(record['results']) != 4 or record['human_approved'] is not False:
        raise ValueError('Incomplete ROI candidate record')
    base = list((dest / 'mask').glob('*/0/*.png'))
    if not base and not hand_only:
        raise ValueError('No baseline object masks')
    for f in base:
        other = candidate / f.relative_to(dest)
        if not other.is_file() or f.read_bytes() != other.read_bytes():
            raise ValueError('ROI changed an object mask')
    inputs(candidate, input_source(dest))

    def previews():
        pipeline.execute([P, str(here / 'process_input_worker.py'), 'masks', str(candidate), '--out', str(candidate)],
                         dest / '06_roi.log', source, env)
        pipeline.execute([P, str(here / 'inspect_masks.py'), '--case', str(candidate), '--out', str(candidate)],
                         dest / '06_roi.log', source, env)
    step(dest, 'roi_preview', previews, ['roi_candidate/mask_validation.json', 'roi_candidate/mask_report.json',
                                         'roi_candidate/mask_contact.jpg']
         + [f'roi_candidate/cam_{c}_mask.webm' for c in range(1, 5)])
    record['integration'] = {'baseline_preserved': True, 'object_masks_verified_identical': True,
                             'grounding_batch_size': 4,
                             'runner_sha256': hashlib.sha256((bundle / 'tools/sam_roi_candidate.py').read_bytes()).hexdigest(),
                             'batched_cameras': True, 'baseline_object': 'skipped' if hand_only else 'computed',
                             **({'hand_mode': 'point_track'} if hand_points else {})}
    (candidate / 'candidate_record.json').write_text(json.dumps(record, ensure_ascii=False, indent=2))


def _finish(dest, started, hand_only):
    """sam_completion.finish(); a hand-only baseline's missing object frames are not warnings."""
    from sam_completion import finish
    record = finish(dest, started)
    if hand_only:
        # Baseline has no object; the candidate's object is filled by the tracker right after
        # (kaist_runtime.object_bbox_track records candidate_warning_count for the tracked masks).
        skipped = 0
        for d in (dest, dest / 'roi_candidate'):
            report = json.loads((d / 'mask_report.json').read_text())
            skipped += sum(len(cam.get('object', {}).get('empty_or_missing_frames', [])) for cam in report['cameras'].values())
        record.update(warning_count=record['warning_count'] - skipped, baseline_object='skipped_object_tracked')
        from sam_settings import save
        save(dest / 'completion.json', record)
        print(f'(기준선 물체 분할 생략: 물체는 bbox/포인트 추적으로 계산 · 기준선 물체 누락 {skipped}건은 경고에서 제외)', flush=True)
    return record


REUSE_NOTE = ('<p><b>테스트 옵션 · 이미지 특징 재사용:</b> 같은 영상의 이미지 특징을 프롬프트 사이에 재사용해 계산했습니다. '
              '마스크가 평소와 다르게 보이면 알려 주세요.</p>')
HAND_ONLY_NOTE = ('<p><b>속도 최적화:</b> 이 실행은 물체를 bbox/포인트 추적으로 계산하므로, '
                  '왼쪽 기준선에는 손만 계산했습니다(물체 초록 표시 없음). 검수 대상은 오른쪽 후보입니다.</p>')


HAND_POINT_NOTE = ('<p><b>손 분할 방식: 첫 프레임 포인트 → 손 추적.</b> 텍스트 기준선과 손 ROI 확대를 하지 않았습니다. '
                   '왼쪽(기준선)과 오른쪽(후보)의 손은 같은 추적 결과입니다. 첫 프레임에서 고른 손(파랑 점=포함, 주황 점=제외): '
                   + ' · '.join(f'<a href="hand_track/{c}/seed/selected.jpg">Camera {c + 1}</a>' for c in range(4)) + '. '
                   '손이 화면 밖으로 나갔다 들어오는 구간, 빠른 움직임, 물체와 겹치는 구간, 팔뚝 혼입을 확인하세요.</p>')


def sam3(pipeline, p, ep, new=False, object_tracked=False, hand_points=None):
    """pipeline.sam3() with A (one model load per phase), C (hand-only baseline when object_tracked)
    and H (hand_points: hands tracked from first-frame points instead of text baseline + ROI)."""
    if hand_points and not object_tracked:
        raise ValueError('손 포인트 방식은 물체 bbox/포인트 추적과 함께만 쓸 수 있습니다.')
    started = time.monotonic()
    load, save, here = pipeline.load, pipeline.save, pipeline.HERE
    fp = pipeline.gate(p, ep)
    prepared = p / '03_prepared' / ep
    rec = load(prepared / 'preparation.json')
    if rec['status'] != 'preprocessed' or rec['input_fingerprint'] != fp:
        raise ValueError('Prepared data not valid')
    from preparation_contract import contract as prep_contract, verify, prepared_fps
    _, source, _ = pipeline.runtime()
    verify(prepared, prep_contract(fp, here, source, prepared_fps(prepared)))
    dest = pipeline.episode_root(p, ep)
    if (dest / 'review/approved.json').exists() and not new:
        print('이미 승인된 에피소드입니다. 다음 단계: 07_track.py', dest)
        return
    from sam_settings import choose
    settings = choose(p, ep, new)
    from roi_workflow import preflight, comparison_body
    preflight(prepared, settings, here)
    root, source, env = pipeline.runtime()
    P = str(root / 'phystwin-env/bin/python')
    S = str(root / 'sam-env/bin/python')
    from resume_steps import step, contract
    if dest.exists() and (new or not (dest / '_resume/contract.json').exists()):
        print('기존 결과 보존 후 시작: 완료 체크포인트가 없거나 새 실행 요청')
        pipeline.archive(p, dest)
    if not dest.exists():
        dest = p / '04_masks/pending' / ep
    elif dest.parent.name != 'pending':
        dest = pipeline.relocate(p, ep, 'pending')
    dest.mkdir(parents=True, exist_ok=True)
    # Same pinned code list as pipeline.sam3(), so runs resume across both implementations.
    contract(dest, prepared, settings,
             [here / n for n in ['pipeline.py', 'roi_workflow.py', 'roi_camera.py', 'run_hand_roi.py', 'stage_refs.py']]
             + [source / SOURCE_SAM[0], here / 'sam_roi_bundle/configs/sam_roi_candidate_v1.json'])
    reuse = feature_reuse_enabled()
    if reuse:
        print('[특징 재사용] 테스트 옵션으로 실행합니다 (끄기: ' + str(FEATURE_REUSE_FLAG) + ' 삭제)', flush=True)
    try:
        from stage_refs import inputs
        inputs(dest, prepared)
        save(dest / 'state.json', {'status': 'running', 'input_fingerprint': fp, 'settings': settings, 'version': 'sam3.1'})
        pipeline.execute([P, str(here / 'process_input_worker.py'), 'validate', str(dest)], dest / '05.log', source, env)
        hand_only = _baseline(pipeline, dest, ep, settings, S, source, env, object_tracked, hand_points)

        def previews():
            pipeline.execute([P, str(here / 'process_input_worker.py'), 'masks', str(dest), '--out', str(dest)],
                             dest / '06.log', source, env)
            pipeline.execute([P, str(here / 'inspect_masks.py'), '--case', str(dest), '--out', str(dest)],
                             dest / '06.log', source, env)
        # Baseline previews are CPU-only and read only mask/: build them while the GPU runs the ROI phase.
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=1) as pool:
            preview = pool.submit(step, dest, 'baseline_preview', previews,
                                  ['mask_validation.json', 'mask_report.json', 'mask_contact.jpg']
                                  + [f'cam_{c}_mask.webm' for c in range(1, 5)])
            try:
                _roi(pipeline, dest, S, P, source, env, hand_only, hand_points)
            finally:
                preview.result()
        save(dest / 'state.json', {'status': 'mask_review_pending',
                                   'method': 'hand_point_track_v1' if hand_points else 'full_frame_plus_static_hand_roi_pilot_v1',
                                   'human_approved': False, 'candidate_mask': 'roi_candidate/mask',
                                   'baseline_mask': 'mask', 'input_fingerprint': fp, 'settings': settings,
                                   'version': 'sam3.1', **({'baseline_object': 'skipped_object_tracked'} if hand_only else {}),
                                   **({'hand_mode': 'point_track', 'hand_points': hand_points} if hand_points else {}),
                                   **({'feature_reuse_test': True} if reuse else {})})
        (dest / 'index.html').write_text(pipeline.page(ep + ' · 기준선 / ROI 손 마스크 비교',
                                                       (REUSE_NOTE if reuse else '') + (HAND_POINT_NOTE if hand_points else HAND_ONLY_NOTE if hand_only else '')
                                                       + comparison_body()))
        record = _finish(dest, started, hand_only)
        if reuse:
            from sam_settings import save as save_json
            save_json(dest / 'completion.json', {**record, 'feature_reuse_test': True})
        pipeline.overview(p)
        print('Mask review:', dest / 'index.html')
    except (Exception, KeyboardInterrupt) as ex:
        save(dest / 'state.json', {'status': 'interrupted_or_failed', 'error': str(ex)})
        raise
