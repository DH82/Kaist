"""Worker-side adapter. No torch/Airflow imports; invoke existing revising CLIs."""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import uuid


def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def read(path):
    return json.loads(Path(path).read_text())


def module(path):
    sys.path.insert(0, str(path.parent))
    spec = importlib.util.spec_from_file_location('kaist_adapter_' + path.stem, path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def inventory(root):
    return {str(p.relative_to(root)): {'size': p.stat().st_size, 'sha256': digest(p)}
            for p in sorted(root.rglob('*')) if p.is_file() and p.name != 'manifest.json'}


def publish(source, destination, required, provenance):
    """Copy selected artifacts, verify every byte, then rename within the NAS mount."""
    source, destination = Path(source), Path(destination)
    for name in required:
        rel = Path(name)
        if rel.is_absolute() or '..' in rel.parts:
            raise ValueError('Artifact paths must be relative')
        if not (source / rel).exists():
            raise FileNotFoundError(source / rel)
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = destination.parent / ('.' + destination.name + '.upload-' + uuid.uuid4().hex)
    staging.mkdir()
    try:
        for name in required:
            src, dst = source / name, staging / name
            dst.parent.mkdir(parents=True, exist_ok=True)
            if src.is_dir():
                # Selected output trees must be self-contained; reject directory links/cycles.
                if src.is_symlink() or any(p.is_symlink() for p in src.rglob('*')):
                    raise ValueError('Publish explicit files instead of linked output trees: ' + name)
                shutil.copytree(src, dst)
            else:
                shutil.copy2(src, dst)
        records = inventory(staging)
        for name, rec in records.items():
            if rec['sha256'] != digest(source / name):
                raise RuntimeError('Source changed during upload: ' + name)
        manifest = {'provenance': provenance, 'files': records}
        (staging / 'manifest.json').write_text(json.dumps(manifest, indent=2))
        if destination.exists():
            if read(destination / 'manifest.json') != manifest or inventory(destination) != records:
                raise RuntimeError('Existing NAS result differs; choose a new run')
            return str(destination)
        # Never silently publish onto a local directory when the NAS is unmounted.
        staging.rename(destination)
        return str(destination)
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def check_project_mount(cfg):
    # NAS 마운트가 없으면 같은 경로에 로컬 작업 파일을 생성하지 않습니다.
    mount = Path(cfg["nas_mount"]).resolve()
    mounted = mount.is_mount()
    if Path(cfg["project_dir"]).resolve().is_relative_to(mount) and not mounted:
        raise RuntimeError(f"NAS 작업 폴더의 마운트를 확인하세요: {mount}")
    return mounted


def publish_result(source, cfg, episode, key, identity, receipt):
    nas = Path(cfg['nas_mount'])
    nas_ready = check_project_mount(cfg)
    root = nas / cfg['nas_results_subdir'] if nas_ready else Path(cfg['project_dir']) / '07_deliverables' / 'local_results'
    destination = root / episode / key
    result = publish(source, destination, cfg['artifacts'], identity)
    record = {'destination': result, 'storage': 'nas' if nas_ready else 'local',
              'nas_pending': not nas_ready, 'configured_nas': str(nas)}
    (Path(receipt) / 'published.json').write_text(json.dumps(record, indent=2))
    print(('NAS 저장 완료: ' if nas_ready else 'NAS 미연결 · GPU PC 로컬 저장 완료 (NAS 전송 대기): ') + result, flush=True)
    return result


def sam_settings_changed(project, scripts, episode, prompt, scene):
    dest = module(Path(scripts) / 'mask_paths.py').episode_root(Path(project), episode)
    contract = dest / '_resume/contract.json'
    if not contract.exists():
        return False
    old = read(contract)['settings']
    return old.get('prompt') != prompt.strip() or old.get('scene') != scene


def tracking_needs_new_attempt(project, scripts, episode, masks):
    """Use the native tracking fingerprint; never reuse a different approval/input."""
    out = module(Path(scripts) / 'tracking_paths.py').episode_root(Path(project), episode)
    if not out.exists():
        return False
    approval = module(Path(scripts) / 'mask_review.py').approved(Path(project), episode)
    prepared = module(Path(scripts) / 'stage_refs.py').source(Path(masks))
    current = module(Path(scripts) / 'tracking_contract.py').current_contract(prepared, approval, Path(scripts))
    contract = out / '_resume/contract.json'
    try:
        previous = read(contract)
    except (FileNotFoundError, json.JSONDecodeError):
        print('추적 실행 기록 없음: 기존 결과를 이력에 보관하고 새로 실행합니다.', flush=True)
        return True
    if previous == current:
        return False
    changed = sorted(k for k in set(previous) | set(current) if previous.get(k) != current.get(k))
    print('추적 기준 변경 (' + ', '.join(changed) + '): --new-attempt로 기존 결과를 보관합니다.', flush=True)
    return True


OBJECT_MODES = ('text', 'bbox_track')
OBJECT_TRACKER = Path(__file__).resolve().with_name('sam_object_track.py')


def parse_object_bboxes(value):
    """{"0".."3": [x, y, w, h]} normalized to the first prepared frame of each camera."""
    data = json.loads(value) if isinstance(value, str) else value
    if not isinstance(data, dict) or sorted(data) != ['0', '1', '2', '3']:
        raise ValueError('bbox 물체 추적은 카메라 4대 모두의 첫 프레임 bbox가 필요합니다.')
    result = {}
    for cam, box in data.items():
        if (not isinstance(box, list) or len(box) != 4
                or any(isinstance(v, bool) or not isinstance(v, (int, float)) for v in box)):
            raise ValueError('bbox 형식이 잘못되었습니다: camera ' + cam)
        x, y, w, h = map(float, box)
        if not (0 <= x < 1 and 0 <= y < 1 and w >= 0.01 and h >= 0.01 and x + w <= 1.0001 and y + h <= 1.0001):
            raise ValueError('bbox가 화면 밖이거나 너무 작습니다: camera ' + cam)
        result[cam] = [x, y, w, h]
    return result


def mask_digest(folder):
    return {p.name: digest(p) for p in sorted(Path(folder).glob('*.png'))}


def park_object_tracks(scripts, project, episode):
    """Before (re)running 05_sam3, put the baseline object links back exactly as the
    ROI workflow left them, so its receipts and 'object mask unchanged' check still hold.
    Tracked masks are parked in roi_candidate/object_track/<cam>/0 for reuse."""
    dest = module(Path(scripts) / 'mask_paths.py').episode_root(Path(project), episode)
    candidate = dest / 'roi_candidate'
    if not candidate.is_dir():
        return
    steps = module(Path(scripts) / 'resume_steps.py')
    from stage_refs import link
    for cam in range(4):
        target = candidate / f'mask/{cam}/0'
        if target.is_symlink() or not target.is_dir():
            continue
        home = candidate / 'object_track' / str(cam)
        if (home / 'record.json').is_file():
            steps.preserve(home / '0', dest)
            target.rename(home / '0')
        else:
            steps.preserve(target, dest)
        if (dest / f'mask/{cam}/0').exists():
            link(target, dest / f'mask/{cam}/0')
        print(f'[bbox 물체 추적] camera {cam + 1}: 기준선 링크 복원 후 SAM3 재개', flush=True)


def object_bbox_track(scripts, project, episode, prompt, bboxes, gpu_uuid):
    """Optional object mode, after the unchanged baseline + hand ROI run (05_sam3.py).
    Replaces only roi_candidate/mask/<cam>/0 (the review draft source) with a single
    object tracked from the operator's first-frame bbox. Baseline masks stay untouched."""
    import tempfile
    import time
    scripts, project = Path(scripts).resolve(), Path(project).resolve()
    dest = module(scripts / 'mask_paths.py').episode_root(project, episode)
    candidate = dest / 'roi_candidate'
    state_path = dest / 'state.json'
    state = read(state_path)
    if state.get('status') not in ('mask_review_pending', 'object_bbox_tracking'):
        raise RuntimeError('SAM3 기준선/손 ROI 결과가 완료되지 않았습니다: ' + str(state.get('status')))
    if (dest / 'review/draft').exists() or (dest / 'review/approved.json').exists():
        raise RuntimeError('이미 검수가 시작된 결과입니다. 물체 마스크를 교체하지 않습니다. 새 실행을 사용하세요.')
    pipeline = module(scripts / 'pipeline.py')
    live = module(scripts / 'live_log.py')
    steps = module(scripts / 'resume_steps.py')
    root, source, env = pipeline.runtime()
    env.update(CUDA_VISIBLE_DEVICES=gpu_uuid, PYTHONDONTWRITEBYTECODE='1')
    sam_python, preview_python = root / 'sam-env/bin/python', root / 'phystwin-env/bin/python'
    frames = read(module(scripts / 'stage_refs.py').source(dest) / 'metadata.json')['frame_num']
    # Hide the episode from the review queue while object masks are being replaced.
    (state_path.with_suffix('.tmp')).write_text(json.dumps({**state, 'status': 'object_bbox_tracking'}, indent=2))
    state_path.with_suffix('.tmp').replace(state_path)
    started = time.monotonic()
    records = {}
    for cam in range(4):
        box = bboxes[str(cam)]
        home = candidate / 'object_track' / str(cam)
        target = candidate / f'mask/{cam}/0'
        rec = read(home / 'record.json') if (home / 'record.json').is_file() else None
        same = bool(rec and rec['bbox_norm_xywh'] == box and rec['text'] == prompt and rec['frames'] == frames)
        if same and target.is_dir() and not target.is_symlink() and mask_digest(target) == rec['mask_sha256']:
            print(f'[bbox 물체 추적] camera {cam + 1}: 완료 결과 재사용', flush=True)
        elif same and (home / '0').is_dir() and mask_digest(home / '0') == rec['mask_sha256']:
            if target.is_symlink():
                target.unlink()
            elif target.exists():
                raise RuntimeError('예상하지 못한 물체 마스크 폴더: ' + str(target))
            (home / '0').rename(target)
            print(f'[bbox 물체 추적] camera {cam + 1}: 보관된 추적 결과 재사용', flush=True)
        else:
            with tempfile.TemporaryDirectory(prefix='.object-track-', dir=candidate) as tmp:
                work = Path(tmp) / 'run'
                live.execute([str(sam_python), str(OBJECT_TRACKER), '--case', str(dest), '--cam', str(cam),
                              '--bbox', *map(str, box), '--text', prompt, '--out', str(work)],
                             dest / '05_object.log', source, env)
                rec = read(work / 'record.json')
                rec['mask_sha256'] = mask_digest(work / '0')
                (work / 'record.json').write_text(json.dumps(rec, indent=2, ensure_ascii=False))
                if home.exists():
                    steps.preserve(home, dest)
                home.parent.mkdir(exist_ok=True)
                work.rename(home)
            if target.is_symlink():
                target.unlink()
            elif target.exists():
                steps.preserve(target, dest)
            (home / '0').rename(target)
        records[str(cam)] = {k: rec[k] for k in ('bbox_px_xyxy', 'seed_attempt_used', 'selected_candidate',
                                                 'nonempty_frames', 'frames', 'wall_s')}
    # Review previews/report must describe the masks that will actually be reviewed.
    live.execute([str(preview_python), str(scripts / 'process_input_worker.py'), 'masks', str(candidate),
                  '--out', str(candidate)], dest / '06_object.log', source, env)
    live.execute([str(preview_python), str(scripts / 'inspect_masks.py'), '--case', str(candidate),
                  '--out', str(candidate)], dest / '06_object.log', source, env)
    seconds = round(time.monotonic() - started, 1)
    index = dest / 'index.html'
    marker = '<!-- bbox_object_track -->'
    text = index.read_text()
    if marker not in text:
        note = (marker + '<p><b>물체 분할 방식: 첫 프레임 bbox + 텍스트 → 단일 물체 추적 (테스트 옵션).</b> '
                '오른쪽 후보의 초록(물체)은 bbox로 고른 초기 마스크 하나만 추적한 결과입니다. 왼쪽 기준선은 기존 텍스트 방식입니다. '
                '초기 후보는 bbox 안 면적 기준으로 자동 선택했습니다. 선택된 초기 마스크: '
                + ' · '.join(f'<a href="roi_candidate/object_track/{c}/seed/selected.jpg">Camera {c + 1}</a>' for c in range(4)) + '. '
                '첫 프레임, 빠른 움직임, 가림 후 복구, 마지막 프레임과 그림자 혼입을 확인하세요.</p>')
        text = text.replace('<p>', note + '<p>', 1)
        index.write_text(text)
    report = read(candidate / 'mask_report.json')
    warnings = sum(len(obj['empty_or_missing_frames']) for cam in report['cameras'].values() for obj in cam.values())
    completion_path = dest / 'completion.json'
    if completion_path.is_file():
        completion = read(completion_path)
        completion.update(object_mode='bbox_track', object_track_seconds=seconds, candidate_warning_count=warnings)
        completion_path.write_text(json.dumps(completion, indent=2, ensure_ascii=False))
    state.update(status='mask_review_pending', object_mode='bbox_track', object_prompt=prompt,
                 object_bboxes=bboxes, object_tracks=records,
                 method=str(state.get('method', '')).split('+bbox_object_track')[0] + '+bbox_object_track_v1')
    state_path.with_suffix('.tmp').write_text(json.dumps(state, indent=2, ensure_ascii=False))
    state_path.with_suffix('.tmp').replace(state_path)
    print(f'[bbox 물체 추적] 4개 카메라 완료 ({seconds}초) · 후보 누락·빈 마스크 {warnings}건 · 사람 검수 대기', flush=True)


def copy_versioned_raw(pipeline, project, source, episode):
    # Native verified copy: the capture folder stays unchanged; no writable links to RAW.
    pipeline.raw.validate(source)
    rec = pipeline.raw.inventory(source)
    rec['raw_episode'] = source.name
    rec['episode'] = episode
    dest = project / '01_raw' / episode
    if dest.exists():
        pipeline.raw.verify(dest, rec)
    else:
        pipeline.raw.stage_copy(source, dest, rec)
    pipeline.save(pipeline.manifest(project, episode), rec)


def execute(stage, episode, run_id, config_path, sam_prompt='', sam_scene='2d', sam_confirmed=False, raw_episode=None,
            object_mode='text', object_bboxes=None):
    if object_mode not in OBJECT_MODES:
        raise ValueError('Unknown object mode')
    bboxes = parse_object_bboxes(object_bboxes) if object_mode == 'bbox_track' else None
    if object_mode == 'text' and object_bboxes:
        raise ValueError('bbox is only used with object mode bbox_track')
    if raw_episode is not None and (not re.fullmatch(r'ep_[A-Za-z0-9_]+', raw_episode) or not re.fullmatch(re.escape(raw_episode)+r'_[1-9][0-9]*',episode)):
        raise ValueError('Invalid RAW/output episode mapping')
    if not re.fullmatch(r'ep_[A-Za-z0-9_]+', episode):
        raise ValueError('Invalid episode id')
    cfg = read(config_path)
    check_project_mount(cfg)
    scripts = Path(cfg['scripts_dir']).resolve()
    project = Path(cfg['project_dir']).resolve()
    expected = Path(cfg['pipeline_root']).resolve()
    if not scripts.is_relative_to(expected.parent.parent):
        raise ValueError('scripts_dir must belong to the configured revising checkout')
    runtime = read(scripts / 'runtime.local.json')
    if Path(runtime['pipeline_root']).resolve() != expected:
        raise ValueError('runtime.local.json points to another pipeline')
    key = hashlib.sha256(run_id.encode()).hexdigest()[:24]
    receipt = project / '_airflow' / episode / key
    receipt.mkdir(parents=True, exist_ok=True)
    code = {str(p): digest(p) for base in [scripts, expected / 'kaist-full/PhysTwin/Mono/data_process']
            for p in sorted(base.rglob('*.py')) if '__pycache__' not in p.parts}
    identity = {'episode': episode, 'run_id': run_id, 'config': cfg, 'code': code,
                'runtime': runtime, 'sam_request': {'prompt': sam_prompt, 'scene': sam_scene, 'confirmed': sam_confirmed}, 'config_env_sha256': digest(expected / 'kaist-full/config.env')}
    if object_mode != 'text':
        # Only the optional mode extends the pinned request, so existing runs keep their identity.
        identity['sam_request'].update(object_mode=object_mode, object_bboxes=bboxes,
                                       object_tracker_sha256=digest(OBJECT_TRACKER))
    if raw_episode:
        identity['raw_episode'] = raw_episode
    pinned = receipt / 'identity.json'
    if raw_episode and not pinned.exists():
        for stage_dir in ('01_raw','02_qc','03_prepared','04_masks/pending','04_masks/approved','05_tracking/pending','05_tracking/approved','06_reconstruction'):
            if (project / stage_dir / episode).exists():
                raise RuntimeError('Output episode already exists without this run identity; refusing reuse: '+episode)
    if stage in ('prepare_raw', 'preflight') and not pinned.exists():
        pinned.write_text(json.dumps(identity, indent=2))
    if not pinned.exists() or read(pinned) != identity:
        raise RuntimeError('Code/config changed during run; restore it or start a new run')
    env = os.environ.copy()
    env.update(PYTHONDONTWRITEBYTECODE='1', PYTHONUNBUFFERED='1',
               PYOPENGL_PLATFORM='egl', CUDA_VISIBLE_DEVICES=cfg['gpu_uuid'])
    env.pop('DISPLAY', None)
    env.pop('XAUTHORITY', None)
    prepared = project / '03_prepared' / episode
    recon = project / '06_reconstruction' / episode
    python = cfg['control_python']
    adapter = str(Path(__file__).resolve())
    os.environ['KAIST_PROGRESS_STAGE'] = stage
    env['KAIST_PROGRESS_STAGE'] = stage
    from pipeline_progress import install
    sys.path.insert(0, str(scripts))
    install()
    progress_cli = str(Path(__file__).with_name('pipeline_progress.py'))
    def cli(name, *args):
        # 원본 스크립트의 mask_review.lock()(프로젝트 전체 잠금)을 에피소드 단위 잠금으로 바꿔 실행합니다.
        # 그래야 MANO·추적·복원이 도는 동안에도 다른 에피소드가 진행됩니다. (episode_cli.py)
        subprocess.run([python, str(Path(__file__).with_name('episode_cli.py')), episode, str(scripts / name),
                        *map(str, args)], env=env, cwd=scripts, check=True)
    if stage == 'prepare_raw':
        pipeline = module(scripts / 'pipeline.py')
        pipeline.init(project)
        if raw_episode:
            copy_versioned_raw(pipeline, project, Path(cfg['raw_root']) / raw_episode, episode)
        else:
            pipeline.copy_raw(project, [Path(cfg['raw_root']) / episode])
        if (project / '02_qc' / episode / 'qc.json').exists():
            pipeline.gate(project, episode)
            pipeline.prepare(project, episode)
        else:
            pipeline.run_qc(project, episode, open_setup=False)
    elif stage == 'sam_settings_ready':
        settings_module = module(scripts / 'sam_settings.py')
        changed = sam_confirmed and sam_settings_changed(project, scripts, episode, sam_prompt, sam_scene)
        if sam_confirmed:
            if not sam_prompt.strip():
                raise ValueError('Confirmed SAM3 request requires an object prompt')
            module(scripts / 'sam_local_setup.py').approve_only(project, episode, sam_prompt, sam_scene, new=changed)
            if changed:
                print('분할 설정 변경: 기존 결과를 이력에 보관하고 새 분할을 시작합니다.', flush=True)
        else:
            print('Waiting for saved operator-confirmed SAM3 settings, or a new run with sam_settings_confirmed=true')
        try:
            settings_module.choose(project, episode, new=changed)
        except (ValueError, FileNotFoundError):
            return False
        return True
    elif stage == 'sam3':
        changed = sam_confirmed and sam_settings_changed(project, scripts, episode, sam_prompt, sam_scene)
        if bboxes:
            park_object_tracks(scripts, project, episode)
        # 원본 05_sam3.py는 프로젝트 전체 잠금을 잡아 다른 에피소드의 SAM3·검수를 막으므로,
        # 같은 pipeline.sam3()를 에피소드 단위 잠금으로 실행합니다(동시 실행 2개 지원).
        subprocess.run([python, str(Path(__file__).with_name('sam3_episode.py')), str(scripts), str(prepared), str(project),
                        *(['--new-attempt'] if changed else [])], env=env, cwd=scripts, check=True)
        if bboxes:
            object_bbox_track(scripts, project, episode, sam_prompt.strip(), bboxes, cfg['gpu_uuid'])
    elif stage == 'mask_approved':
        review = module(scripts / 'mask_review.py')
        try:
            review.approved(project, episode)
        except (ValueError, OSError, KeyError):
            return False
        return True
    elif stage == 'preflight':
        if not prepared.is_dir():
            raise FileNotFoundError('Run existing RAW QC/preprocessing/SAM3 and approve masks first')
        subprocess.run(['nvidia-smi', '-i', cfg['gpu_uuid'], '--query-gpu=uuid,name',
                        '--format=csv,noheader'], check=True)
        masks = module(scripts / 'mask_paths.py').episode_root(project, episode)
        cli('07_track.py', masks, '--check')
    elif stage == 'track':
        masks = module(scripts / 'mask_paths.py').episode_root(project, episode)
        fresh = tracking_needs_new_attempt(project, scripts, episode, masks)
        cli('07_track.py', masks, *(['--new-attempt'] if fresh else []))
    elif stage == 'tracking_approved':
        paths = module(scripts / 'tracking_paths.py')
        review = module(scripts / 'tracking_review.py')
        try:
            review.require_approved(paths.episode_root(project, episode))
        except (ValueError, OSError, KeyError):
            return False  # No auto-approval; existing review UI remains authoritative.
        return True
    elif stage == 'prepare_reconstruction':
        paths = module(scripts / 'tracking_paths.py')
        cli('10_prepare_reconstruction.py', paths.episode_root(project, episode))
    elif stage == 'reconstruct':
        paths = module(scripts / 'tracking_paths.py')
        # 10_reconstruct consumes approved tracking; it creates 06_reconstruction itself.
        cli('10_reconstruct.py', paths.episode_root(project, episode), '--no-view')
    elif stage == 'hands':
        cli('18_fit_hands.py', recon, '--hand-side', 'right', '--no-view')
    elif stage == 'validate':
        module(scripts / '18_fit_hands.py').verify(recon / 'hands')
    elif stage == 'publish':
        result = publish_result(recon, cfg, episode, key, identity, receipt)
        if check_project_mount(cfg):
            # 예전(NAS 작업 폴더) 방식과 같게: 이 에피소드의 단계별 작업 폴더 전체를 NAS work/<pc>에 미러링
            sys.path.insert(0, str(Path(__file__).resolve().parent))
            import sync_work
            sync_work.sync_episode(cfg, episode, lambda m: print(m, flush=True))
        return result
    else:
        raise ValueError(stage)
    return True


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('stage'); p.add_argument('episode'); p.add_argument('run_id')
    p.add_argument('--config', required=True)
    p.add_argument('--raw-episode')
    p.add_argument('--sam-prompt', default='')
    p.add_argument('--sam-scene', choices=['2d'], default='2d')
    p.add_argument('--sam-confirmed', action='store_true')
    p.add_argument('--object-mode', choices=OBJECT_MODES, default='text')
    p.add_argument('--object-bboxes', default=None, help='JSON {"0".."3": [x,y,w,h]} normalized')
    a = p.parse_args()
    result = execute(a.stage, a.episode, a.run_id, a.config, a.sam_prompt, a.sam_scene, a.sam_confirmed, a.raw_episode,
                     a.object_mode, a.object_bboxes)
    print(json.dumps(result))
    if result is False:
        sys.exit(75)
