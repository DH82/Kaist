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


def execute(stage, episode, run_id, config_path, sam_prompt='', sam_scene='2d', sam_confirmed=False, raw_episode=None):
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
    def cli(name, *args):
        subprocess.run([python, str(scripts / name), *map(str, args)], env=env,
                       cwd=scripts, check=True)
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
        cli('05_sam3.py', prepared, '--project', project, *(['--new-attempt'] if changed else []))
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
        return publish_result(recon, cfg, episode, key, identity, receipt)
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
    a = p.parse_args()
    result = execute(a.stage, a.episode, a.run_id, a.config, a.sam_prompt, a.sam_scene, a.sam_confirmed, a.raw_episode)
    print(json.dumps(result))
    if result is False:
        sys.exit(75)
