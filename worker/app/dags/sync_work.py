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
import argparse, datetime, json, re, shutil, subprocess, sys
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


# ---- 업로드 속도 제한 (낮 시간대에만) ----
# NAS 회선이 느린 PC(예: 100Mbps로 연결된 PC1)는 작업 폴더 업로드가 회선을 다 써서, 낮에는 RAW 목록 같은
# 화면 조회까지 몇 분씩 밀립니다. worker/config/nas_upload_limit.json이 있으면 그 시간대에만 rsync 속도를 묶습니다.
#   {"mb_per_s": 8, "from": "09:30", "to": "18:30", "weekdays_only": true}
#   (예전 형식 "from_hour": 8, "to_hour": 19도 읽습니다)
# 파일이 없으면 제한하지 않습니다. 올리는 도중 시간대가 바뀌면 rsync를 새 속도로 다시 시작합니다
# (끝난 파일은 건너뛰고, 올리던 파일 하나만 처음부터 다시 올림).
LIMIT_TZ = 'Asia/Seoul'


def _minute(value):
    """'09:30' 또는 9 또는 9.5 → 하루 중 분(0~1440)."""
    if isinstance(value, str) and ':' in value:
        h, m = value.split(':', 1)
        h, m = int(h), int(m)
    else:
        h, m = divmod(round(float(value) * 60), 60)
    if not (0 <= h <= 24 and 0 <= m < 60 and h * 60 + m <= 1440):
        raise ValueError(value)
    return h * 60 + m


def limit_window(spec):
    """제한 시간대 (시작 분, 끝 분). "from"/"to"('HH:MM')가 우선이고, 없으면 예전 형식 from_hour/to_hour."""
    return _minute(spec.get('from', spec.get('from_hour', 8))), _minute(spec.get('to', spec.get('to_hour', 19)))


def upload_limit(spec, now=None):
    """지금 적용할 rsync --bwlimit 값(KiB/s). 0이면 제한 없음. 설정이 잘못됐으면 제한하지 않습니다."""
    try:
        rate = int(float(spec['mb_per_s']) * 1024)
        start, end = limit_window(spec)
        if rate <= 0 or start == end:
            return 0
        if now is None:
            from zoneinfo import ZoneInfo
            now = datetime.datetime.now(ZoneInfo(spec.get('tz') or LIMIT_TZ))
        if spec.get('weekdays_only', True) and now.weekday() >= 5:
            return 0
        minute = now.hour * 60 + now.minute
        inside = start <= minute < end if start < end else (minute >= start or minute < end)
        return rate if inside else 0
    except (TypeError, KeyError, ValueError, AttributeError):
        return 0


def window_text(spec):
    try:
        start, end = limit_window(spec)
        return f"{start // 60:02d}:{start % 60:02d}~{end // 60:02d}:{end % 60:02d}{' 평일' if spec.get('weekdays_only', True) else ''}에 {spec.get('mb_per_s')} MB/s"
    except (TypeError, ValueError, AttributeError):
        return '설정을 읽을 수 없음'


def limit_text(rate):
    return f'속도 제한 {rate / 1024:g} MB/s' if rate else '속도 제한 없음'


def _rsync_limited(cmd, paths, limit, log, poll):
    """속도 제한을 걸고 올립니다. 시간대가 바뀌어 적용할 속도가 달라지면 멈추고 새 속도로 다시 시작합니다."""
    while True:
        rate = upload_limit(limit)
        proc = subprocess.Popen(cmd + ([f'--bwlimit={rate}'] if rate else []) + paths,
                                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        while True:
            try:
                err = proc.communicate(timeout=poll)[1]
                return proc.returncode, err
            except subprocess.TimeoutExpired:
                if upload_limit(limit) == rate:
                    continue
            proc.terminate()
            try:
                proc.communicate(timeout=60)
            except subprocess.TimeoutExpired:
                proc.kill(); proc.communicate()
            log(f'업로드 시간대가 바뀌어 다시 시작합니다: {limit_text(rate)} → {limit_text(upload_limit(limit))}')
            break


def rsync(src, dst, dry=False, limit=None, log=print, poll=30):
    dst.parent.mkdir(parents=True, exist_ok=True)
    paths = [str(src) + ('/' if src.is_dir() else ''), str(dst) + ('/' if src.is_dir() else '')]
    if limit and not dry:
        code, err = _rsync_limited(RSYNC, paths, limit, log, poll)
        if code not in (0, 24):
            raise RuntimeError(f'rsync 실패 ({code}): {src} → {dst}\n{(err or "")[-1500:]}')
        return []
    cmd = RSYNC + (['--dry-run', '--itemize-changes'] if dry else []) + paths
    r = subprocess.run(cmd, capture_output=True, text=True)
    # 24 = 전송 중 원본 파일이 사라짐(로그 회전 등). 다시 비교 단계에서 걸러집니다.
    if r.returncode not in (0, 24):
        raise RuntimeError(f'rsync 실패 ({r.returncode}): {src} → {dst}\n{r.stderr[-1500:]}')
    return [l for l in r.stdout.splitlines() if l.strip() and not l.startswith('.d')] if dry else []


def size(path):
    return sum(f.stat().st_size for f in path.rglob('*') if f.is_file() and not f.is_symlink()) if path.is_dir() else path.stat().st_size


def history_dirs(project):
    """99_history/<단계>/<ep> 보관본 폴더들. 원본 스크립트가 단계 이름 그대로(05_tracking, 06_reconstruction,
    hands, approved …) 이전 시도를 옮겨 두므로, 이름을 고정하지 않고 찾아서 함께 올립니다."""
    base = project / '99_history'
    if not base.is_dir():
        return []
    return ['99_history/' + d.name for d in sorted(base.iterdir())
            if d.is_dir() and not d.is_symlink() and d.name not in HISTORY_SKIP]


# 에피소드별 보관본이 아닌 99_history 하위 폴더(잠금·임시 작업·매니페스트·업로드 기록)
HISTORY_SKIP = {'locks', 'staging', 'manifests', 'nas_sync'}


def sync_episode(cfg, ep, log=print, limit=None):
    project = Path(cfg['project_dir'])
    nas = work_root(cfg)
    if not Path(cfg['nas_mount']).is_mount():
        raise RuntimeError('NAS가 마운트되어 있지 않습니다: ' + cfg['nas_mount'])
    copied, removed, total = [], [], 0
    if limit:
        log(f"업로드 {limit_text(upload_limit(limit))} (설정: {window_text(limit)})")
    for d in list(dict.fromkeys(STAGE_DIRS + history_dirs(project))):
        src, dst = project / d / ep, nas / d / ep
        if src.is_dir() and not src.is_symlink():
            rsync(src, dst, limit=limit, log=log)
            copied.append(f'{d}/{ep}'); total += size(src)
        elif dst.exists() and d in STATE_GROUPS and any((project / o / ep).is_dir() for o in STATE_GROUPS[d]):
            # 로컬에서는 다른 상태 폴더로 옮겨진 에피소드 → NAS의 이전 위치 사본 정리
            (shutil.rmtree(dst) if dst.is_dir() and not dst.is_symlink() else dst.unlink())
            removed.append(f'{d}/{ep}')
    for pattern in STAGE_FILES:
        rel = pattern.format(ep=ep)
        if (project / rel).is_file():
            rsync(project / rel, nas / rel, limit=limit, log=log); copied.append(rel); total += size(project / rel)
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


def import_episode(cfg, source_pc, ep, log=print, allow_existing=False):
    """MANO 이관: 다른 PC가 NAS work/<source_pc>에 검증 업로드한 에피소드 작업 폴더를 이 PC 작업 폴더로 가져옵니다.

    - 원본 PC의 업로드 기록(99_history/nas_sync/<ep>.json)에 있는 경로만 가져옵니다.
    - 이 PC에 같은 에피소드 폴더가 이미 있으면(다른 실행과 충돌) 거부합니다. allow_existing은 같은 이관의 재시도용.
    - 가져온 뒤 rsync로 다시 비교해 차이가 0건이어야 성공입니다."""
    if not re.fullmatch(r'pc[1-6]', source_pc or ''):
        raise ValueError('원본 PC 이름이 잘못되었습니다: ' + str(source_pc))
    if not Path(cfg['nas_mount']).is_mount():
        raise RuntimeError('NAS가 마운트되어 있지 않습니다: ' + cfg['nas_mount'])
    project = Path(cfg['project_dir'])
    src_root = Path(cfg['nas_mount']) / 'work' / source_pc
    record = json.loads((src_root / '99_history/nas_sync' / f'{ep}.json').read_text())
    paths = record.get('paths') or []
    if not any(p.startswith('06_reconstruction/') for p in paths):
        raise RuntimeError('원본 PC의 NAS 업로드에 3D 복원 결과가 없습니다: ' + ep)
    for rel in paths:
        if Path(rel).name not in (ep, ep + '.json') or '..' in Path(rel).parts:
            raise RuntimeError('업로드 기록의 경로가 에피소드 경로가 아닙니다: ' + rel)
        if not allow_existing and (project / rel).exists():
            raise RuntimeError('이 PC에 같은 에피소드 작업 폴더가 이미 있습니다: ' + rel)
    total = 0
    for rel in paths:
        src = src_root / rel
        if not src.exists():
            raise RuntimeError('NAS 원본이 없습니다: ' + str(src))
        rsync(src, project / rel); total += size(src)
    diff = []
    for rel in paths:
        diff += rsync(src_root / rel, project / rel, dry=True)
    if diff:
        raise RuntimeError(f'가져온 사본 검증 실패 ({len(diff)}건): ' + '; '.join(diff[:5]))
    log(f'NAS → 이 PC 가져오기 완료: {ep} ← {src_root} ({len(paths)}개 경로, {total/1e9:.2f} GB, 검증 통과)')
    return {'paths': paths, 'bytes': total, 'source': str(src_root), 'source_record': record}


def restore_local(cfg, ep, log=print):
    """단계 초기화(재실행) 전: 최종 저장 후 로컬에서 지운 이 에피소드 작업 폴더를 NAS work/<이 PC>에서 되돌려 받습니다.

    - 이 PC의 업로드 기록(99_history/nas_sync/<ep>.json)의 local_deleted 경로 중 로컬에 없는 것만 가져옵니다.
    - 가져온 뒤 rsync로 다시 비교해 차이가 0건이어야 성공이고, 기록에 restored_local로 남깁니다."""
    project = Path(cfg['project_dir'])
    nas = work_root(cfg)
    rec_path = nas / '99_history/nas_sync' / f'{ep}.json'
    if not rec_path.is_file():
        return []
    rec = json.loads(rec_path.read_text())
    todo = [rel for rel in rec.get('local_deleted') or [] if not (project / rel).exists()]
    if not todo:
        return []
    if not Path(cfg['nas_mount']).is_mount():
        raise RuntimeError('NAS가 마운트되어 있지 않아 지운 로컬 작업 폴더를 되돌릴 수 없습니다: ' + cfg['nas_mount'])
    total = 0
    for rel in todo:
        src = nas / rel
        if Path(rel).name not in (ep, ep + '.json') or '..' in Path(rel).parts:
            raise RuntimeError('업로드 기록의 경로가 에피소드 경로가 아닙니다: ' + rel)
        if not src.exists():
            raise RuntimeError('NAS 사본이 없습니다: ' + str(src))
        log(f'NAS → 로컬 되돌리기: {rel}')
        rsync(src, project / rel); total += size(src)
    diff = []
    for rel in todo:
        diff += rsync(nas / rel, project / rel, dry=True)
    if diff:
        raise RuntimeError(f'되돌린 사본 검증 실패 ({len(diff)}건): ' + '; '.join(diff[:5]))
    rec['restored_local'] = todo
    rec['restored_local_at'] = datetime.datetime.now().astimezone().isoformat()
    rec['local_deleted'] = [rel for rel in rec.get('local_deleted') or [] if rel not in todo]
    rec_path.write_text(json.dumps(rec, ensure_ascii=False, indent=2))
    log(f'NAS → 로컬 되돌리기 완료: {ep} ({len(todo)}개 경로, {total/1e9:.2f} GB, 검증 통과)')
    return todo


def published_to_nas(project, ep):
    """최종 저장(publish)이 NAS results에 끝났는지. NAS가 없어 로컬(07_deliverables/local_results)에
    저장된 경우는 False — 그때는 로컬이 유일한 사본이므로 지우면 안 됩니다."""
    for f in sorted((Path(project) / '_airflow' / ep).glob('*/published.json')):
        try:
            rec = json.loads(f.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if rec.get('storage') == 'nas' and Path(rec.get('destination') or '/nonexistent').is_dir():
            return True
    return False


def purge_local(cfg, ep, record, log=print):
    """최종 결과가 NAS results에 저장되고 작업 폴더도 NAS에 검증 업로드된 에피소드의 로컬 사본을 지웁니다.

    - 최종 저장이 NAS에 된 경우에만 (published_to_nas)
    - 지우기 직전에 rsync로 다시 비교해 NAS 사본과 차이가 0건일 때만
    - 이번 업로드에서 검증한 경로(record['paths'])만 지웁니다. 심볼릭 링크는 따라가지 않습니다.
    - NAS 99_history/nas_sync/<ep>.json에 삭제 기록을 남깁니다(복원은 NAS work/<pc>에서 같은 경로로 복사).
    """
    project = Path(cfg['project_dir']).resolve()
    if not published_to_nas(project, ep):
        log(f'로컬 작업 파일 유지: {ep} — 최종 결과가 NAS results에 저장되지 않았습니다.')
        return []
    nas = work_root(cfg)
    diff = []
    for rel in record['paths']:
        diff += rsync(project / rel, nas / rel, dry=True)
    if diff:
        raise RuntimeError(f'로컬 삭제 중단 — NAS 사본과 다릅니다 ({len(diff)}건): ' + '; '.join(diff[:5]))
    deleted, freed = [], 0
    for rel in record['paths']:
        p = project / rel
        # 이 에피소드 이름의 폴더/파일이고 작업 폴더 안에 있을 때만 지웁니다.
        if p.name not in (ep, ep + '.json') or not p.parent.resolve().is_relative_to(project):
            raise RuntimeError('로컬 삭제 대상이 에피소드 경로가 아닙니다: ' + str(p))
        if p.is_symlink():
            p.unlink()
        elif p.is_dir():
            freed += size(p); shutil.rmtree(p)
        elif p.is_file():
            freed += size(p); p.unlink()
        else:
            continue
        deleted.append(rel)
    out = nas / '99_history/nas_sync' / f'{ep}.json'
    rec = json.loads(out.read_text()) if out.is_file() else dict(record)
    rec.update(local_deleted=deleted, local_deleted_at=datetime.datetime.now().astimezone().isoformat(), local_freed_bytes=freed)
    out.write_text(json.dumps(rec, ensure_ascii=False, indent=2))
    log(f'로컬 작업 파일 삭제 완료: {ep} ({len(deleted)}개 경로, {freed/1e9:.2f} GB 확보) — NAS 사본: {nas}')
    return deleted


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
