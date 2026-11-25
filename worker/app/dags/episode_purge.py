"""에피소드 작업 데이터 삭제 (UI 3D 복원 일괄 실행 대기 목록의 '⊘ 제거').

이 PC 작업 폴더(project_dir)와 NAS 작업 사본(work/<pc>) 안의 이 에피소드 폴더·파일만 바로 지웁니다.
- 지우는 것: 01_raw 복사본, 02_qc, 03_prepared, 04_masks, 05_tracking, 06_reconstruction, 07_deliverables, _airflow,
  99_history 아래 이 에피소드의 보관본(초기화 보관 포함)·매니페스트·잠금·업로드 기록.
- 건드리지 않는 것: NAS의 RAW 원본(rgbd_raw), NAS 최종 결과(results/…)와 단계 초기화 때 보관한 예전 최종 결과
  (99_history/reset_results), 다른 에피소드, Worker 작업 기록(jobs/).
되돌릴 수 없습니다. 호출하는 쪽(runner.py purge)이 실행 중인 단계·업로드가 없는지 먼저 확인합니다.
"""
import re
import shutil
from pathlib import Path

import sync_work

EPISODE = re.compile(r'ep_[A-Za-z0-9_]+')
# 에피소드 이름이 붙은 단일 파일(작업 폴더·NAS 작업 사본 공통)
FILES = ['99_history/manifests/{ep}.json', '99_history/locks/{ep}.lock', '99_history/nas_sync/{ep}.json']
KEEP_HISTORY = {'reset_results'}   # 예전 최종 결과 보관본은 최종 결과와 같이 남깁니다.


def _size(path):
    try:
        if path.is_symlink() or path.is_file():
            return path.lstat().st_size
        return sum(f.lstat().st_size for f in path.rglob('*') if f.is_file() and not f.is_symlink())
    except OSError:
        return 0


def targets(root, ep):
    """root(작업 폴더 또는 NAS 작업 사본) 안에서 이 에피소드에 속한 경로들."""
    root = Path(root)
    dirs = list(sync_work.STAGE_DIRS)
    history = root / '99_history'
    if history.is_dir():     # 단계 이름이 고정되지 않은 보관본(05_tracking, hands, reset, reset_results …)
        dirs += ['99_history/' + d.name for d in sorted(history.iterdir()) if d.is_dir() and not d.is_symlink() and d.name not in KEEP_HISTORY]
    found = []
    for rel in dict.fromkeys(dirs):
        p = root / rel / ep
        if p.is_symlink() or p.exists():
            found.append(p)
    for pattern in FILES:
        p = root / pattern.format(ep=ep)
        if p.is_symlink() or p.exists():
            found.append(p)
    return found


def _remove(root, path):
    root = Path(root).resolve()
    parent = path.parent.resolve()
    if path.name == '' or not (parent == root or root in parent.parents):
        raise RuntimeError(f'작업 폴더 밖 경로는 지우지 않습니다: {path}')
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink()


def purge(cfg, ep, log=print):
    if not EPISODE.fullmatch(ep or ''):
        raise ValueError('Invalid episode')
    project = Path(cfg['project_dir'])
    roots = [('이 PC 작업 폴더', project)]
    nas_mounted = Path(cfg['nas_mount']).is_mount()
    if nas_mounted:
        roots.append(('NAS 작업 사본', sync_work.work_root(cfg)))
    else:
        log('NAS가 마운트되어 있지 않아 NAS 작업 사본은 지우지 못했습니다: ' + cfg['nas_mount'])
    removed, total = [], 0
    for label, root in roots:
        if not root.is_dir():
            continue
        for path in targets(root, ep):
            size = _size(path)
            _remove(root, path)
            removed.append(str(path)); total += size
            log(f'삭제 ({label}): {path} · {size/1e6:.1f} MB')
    left = [str(p) for _, root in roots if root.is_dir() for p in targets(root, ep)]
    if left:
        raise RuntimeError('지우지 못한 경로가 남았습니다: ' + '; '.join(left[:5]))
    if not nas_mounted:      # 로컬은 지웠지만 NAS 사본이 남았을 수 있으므로 실패로 알려 다시 시도하게 합니다.
        raise RuntimeError('NAS가 연결되지 않아 NAS 작업 사본은 지우지 못했습니다. NAS 연결을 확인한 뒤 다시 시도하세요.')
    log(f'작업 데이터 삭제 완료: {ep} · {len(removed)}개 경로 · {total/1e9:.2f} GB')
    return {'episode': ep, 'removed': removed, 'bytes': total, 'nas': nas_mounted}
