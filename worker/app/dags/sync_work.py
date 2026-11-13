"""GPU PC 로컬 작업 폴더(project_dir)의 에피소드 전체를 NAS work/<pc>에 같은 구조로 올립니다.

예전에는 작업 폴더 자체가 NAS(/mnt/kaist-data/work/pc1)였으므로 01_raw~07, _airflow, 99_history가
처리되는 즉시 NAS에 있었습니다. 지금은 로컬에서 처리하므로 이 스크립트가 같은 경로 구조로 미러링합니다.

  python3 sync_work.py --config <gpu_4090_a.json> <episode> [<episode> ...]
  python3 sync_work.py --config <gpu_4090_a.json> --all

- 에피소드 폴더 단위로 rsync -a --delete (그 에피소드 안에서만 NAS를 로컬과 같게 맞춤)
- 상대 심볼릭 링크는 그대로 보존(같은 구조이므로 NAS에서도 유효)
- pending → approved처럼 로컬에서 상태 폴더가 바뀐 경우, NAS의 이전 상태 폴더 사본은 지웁니다
- 실행 중 잠금 파일(*.lock)은 올리지 않습니다
- 올린 뒤 크기·시간 기준으로 다시 비교해 차이가 없을 때만 성공 기록(99_history/nas_sync/<ep>.json)을 남깁니다
"""
import argparse, datetime, json, shutil, subprocess, sys
from pathlib import Path

# 에피소드 폴더가 놓이는 단계 폴더들 (pipeline/mask_paths 구조와 동일)
STAGE_DIRS = ['01_raw', '02_qc', '03_prepared', '04_masks/pending', '04_masks/approved', '04_masks',
              '05_tracking/pending', '05_tracking/approved', '05_tracking', '06_reconstruction',
              '07_deliverables', '_airflow', '99_history/hands', '99_history/tracking', '99_history/masks']
# 상태에 따라 에피소드가 옮겨 다니는 폴더 묶음. 같은 묶음 안에서만 NAS의 이전 위치 사본을 정리합니다.
_MASKS = ['04_masks/pending', '04_masks/approved', '04_masks']
_TRACK = ['05_tracking/pending', '05_tracking/approved', '05_tracking']
STATE_GROUPS = {d: [o for o in g if o != d] for g in (_MASKS, _TRACK) for d in g}
# 에피소드 이름이 붙은 단일 파일
STAGE_FILES = ['99_history/manifests/{ep}.json']
RSYNC = ['rsync', '-a', '--delete', '--no-owner', '--no-group', '--omit-dir-times',
         '--exclude', '*.lock', '--exclude', '*.tmp', '--exclude', '__pycache__/']


def work_root(cfg):
    sub = cfg.get('nas_work_subdir') or cfg['nas_results_subdir'].replace('results', 'work', 1)
    return Path(cfg['nas_mount']) / sub


def episodes(project):
    found = set()
    for d in STAGE_DIRS:
        base = project / d
        if base.is_dir():
            found.update(p.name for p in base.iterdir() if p.name.startswith('ep_') and p.is_dir() and not p.is_symlink())
    return sorted(found)


def rsync(src, dst, dry=False):
    dst.parent.mkdir(parents=True, exist_ok=True)
    cmd = RSYNC + (['--dry-run', '--itemize-changes'] if dry else []) + [str(src) + ('/' if src.is_dir() else ''), str(dst) + ('/' if src.is_dir() else '')]
    r = subprocess.run(cmd, capture_output=True, text=True)
    # 24 = 전송 중 원본 파일이 사라짐(로그 회전 등). 다시 비교 단계에서 걸러집니다.
    if r.returncode not in (0, 24):
        raise RuntimeError(f'rsync 실패 ({r.returncode}): {src} → {dst}\n{r.stderr[-1500:]}')
    return [l for l in r.stdout.splitlines() if l.strip() and not l.startswith('.d')] if dry else []


def size(path):
    return sum(f.stat().st_size for f in path.rglob('*') if f.is_file() and not f.is_symlink()) if path.is_dir() else path.stat().st_size


def sync_episode(cfg, ep, log=print):
    project = Path(cfg['project_dir'])
    nas = work_root(cfg)
    if not Path(cfg['nas_mount']).is_mount():
        raise RuntimeError('NAS가 마운트되어 있지 않습니다: ' + cfg['nas_mount'])
    copied, removed, total = [], [], 0
    for d in STAGE_DIRS:
        src, dst = project / d / ep, nas / d / ep
        if src.is_dir() and not src.is_symlink():
            rsync(src, dst)
            copied.append(f'{d}/{ep}'); total += size(src)
        elif dst.exists() and d in STATE_GROUPS and any((project / o / ep).is_dir() for o in STATE_GROUPS[d]):
            # 로컬에서는 다른 상태 폴더로 옮겨진 에피소드 → NAS의 이전 위치 사본 정리
            (shutil.rmtree(dst) if dst.is_dir() and not dst.is_symlink() else dst.unlink())
            removed.append(f'{d}/{ep}')
    for pattern in STAGE_FILES:
        rel = pattern.format(ep=ep)
        if (project / rel).is_file():
            rsync(project / rel, nas / rel); copied.append(rel); total += size(project / rel)
    if not copied:
        raise RuntimeError('로컬 작업 폴더에 이 에피소드가 없습니다: ' + ep)
    # 검증: 다시 비교했을 때 바뀔 파일이 없어야 합니다.
    diff = []
    for rel in copied:
        diff += rsync(project / rel, nas / rel, dry=True)
    if diff:
        raise RuntimeError(f'NAS 사본 검증 실패 ({len(diff)}건): ' + '; '.join(diff[:5]))
    record = {'episode': ep, 'synced_at': datetime.datetime.now().astimezone().isoformat(),
              'source': str(project), 'destination': str(nas), 'paths': copied, 'removed_stale': removed,
              'bytes': total, 'verified': 'rsync size+mtime re-compare: 0 differences'}
    out = nas / '99_history/nas_sync' / f'{ep}.json'
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(record, ensure_ascii=False, indent=2))
    log(f'NAS 작업 폴더 업로드 완료: {ep} → {nas} ({len(copied)}개 경로, {total/1e9:.2f} GB, 검증 통과)')
    return record


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--config', required=True)
    p.add_argument('episodes', nargs='*')
    p.add_argument('--all', action='store_true')
    a = p.parse_args()
    cfg = json.loads(Path(a.config).read_text())
    eps = episodes(Path(cfg['project_dir'])) if a.all else a.episodes
    if not eps:
        p.error('에피소드를 지정하거나 --all을 쓰세요.')
    failed = 0
    for ep in eps:
        try:
            sync_episode(cfg, ep, lambda m: print(m, flush=True))
        except Exception as e:
            failed += 1
            print(f'실패: {ep}: {e}', file=sys.stderr, flush=True)
    sys.exit(1 if failed else 0)
