"""단계 초기화(재실행): 끝난 실행의 한 단계부터 결과를 보관하고 처음 상태로 되돌립니다.

UI '단계 초기화'가 Worker runner.py reset 으로 호출합니다. 원본 단계 스크립트는 기존 결과를 이어서(재개) 쓰므로,
단계 작업 상태만 되돌리면 같은 결과가 재사용됩니다. 그래서 선택한 단계와 그 뒤 단계의 에피소드 폴더를
99_history/reset/<ep>/<시각>/ 아래로 옮겨(삭제하지 않음) 새로 계산하게 합니다.

- 최종 저장 후 로컬 작업 폴더가 지워진 실행은 NAS work/<이 PC>에서 먼저 되돌려 받습니다.
- 실행 시작 후 파이프라인 코드가 바뀌었으면, 처음(RAW 준비)부터 초기화할 때만 새 코드로 다시 고정합니다.
  중간 단계부터는 앞 단계 결과가 예전 코드로 만들어졌으므로 거부합니다(새 실행으로 시작).
- NAS에 저장된 최종 결과는 NAS work/<이 PC>/99_history/reset_results/<ep>/<시각>/ 로 옮깁니다.
  다시 저장(publish)할 때 '기존 NAS 결과와 다름'으로 실패하지 않게 하고, 이전 결과도 남깁니다.
"""
import datetime
import hashlib
import json
import shutil
import sys
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import kaist_runtime as rt  # noqa: E402  (digest/read와 SAM 추적 파일 경로를 실행 고정과 같게 씁니다)
import sync_work  # noqa: E402

# 초기화 시작점. 검수 확인 단계(mask_approved·tracking_approved)는 결과를 만들지 않으므로 시작점이 아닙니다.
STARTS = ('prepare_raw', 'sam3', 'preflight', 'prepare_reconstruction', 'hands', 'validate', 'publish')
# 시작점별로 보관할 에피소드 폴더(뒤 단계 폴더도 함께 보관합니다).
OUTPUTS = {
    'prepare_raw': ['01_raw', '02_qc', '03_prepared'],
    'sam3': ['04_masks/pending', '04_masks/approved', '04_masks'],
    'preflight': ['05_tracking/pending', '05_tracking/approved', '05_tracking'],
    'prepare_reconstruction': ['06_reconstruction'],
}


def stamp():
    return datetime.datetime.now().strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:6]


def receipt_dir(cfg, episode, run_id):
    return Path(cfg['project_dir']).resolve() / '_airflow' / episode / hashlib.sha256(run_id.encode()).hexdigest()[:24]


def code_drift(cfg, pin):
    """실행 고정(identity.json) 중 코드·설정 해시가 지금과 다른 항목 → ({항목: 새 값}, [표시 이름])."""
    scripts = Path(cfg['scripts_dir']).resolve()
    expected = Path(cfg['pipeline_root']).resolve()
    code = {str(p): rt.digest(p) for base in [scripts, expected / 'kaist-full/PhysTwin/Mono/data_process']
            for p in sorted(base.rglob('*.py')) if '__pycache__' not in p.parts}
    new, names = {}, []
    if code != pin.get('code'):
        new['code'] = code
        names += sorted(Path(k).name for k in set(code) | set(pin.get('code') or {})
                        if code.get(k) != (pin.get('code') or {}).get(k))
    runtime = rt.read(scripts / 'runtime.local.json')
    if runtime != pin.get('runtime'):
        new['runtime'] = runtime; names.append('runtime.local.json')
    env = rt.digest(expected / 'kaist-full/config.env')
    if env != pin.get('config_env_sha256'):
        new['config_env_sha256'] = env; names.append('config.env')
    sam = dict(pin.get('sam_request') or {})
    for key, path in (('object_tracker_sha256', rt.OBJECT_TRACKER), ('object_point_tracker_sha256', rt.OBJECT_POINT_TRACKER)):
        if key in sam and sam[key] != rt.digest(path):
            sam[key] = rt.digest(path); new['sam_request'] = sam; names.append(path.name)
    return new, names


def move_aside(src, dst, log):
    """에피소드 폴더를 보관 위치로 옮깁니다. 파이프라인 stage_refs.move를 써서 바깥을 가리키는 링크를 유지합니다."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    from stage_refs import move
    move(src, dst)
    log(f'보관: {src} → {dst}')


def reset_outputs(cfg, episode, run_id, start, log=print):
    """start 단계부터의 결과를 보관합니다. 반환: 기록 dict. 실패 시 예외(아무것도 옮기기 전 확인 실패면 변경 없음)."""
    if start not in STARTS:
        raise ValueError('초기화할 수 없는 단계입니다: ' + str(start))
    project = Path(cfg['project_dir']).resolve()
    sys.path.insert(0, str(Path(cfg['scripts_dir']).resolve()))

    restored = sync_work.restore_local(cfg, episode, log)
    receipt = receipt_dir(cfg, episode, run_id)
    pinned = receipt / 'identity.json'
    if not pinned.is_file():
        raise RuntimeError('이 PC에 이 실행의 기록(identity.json)이 없습니다. 다른 PC에서 실행했거나 새 실행이 필요합니다.')
    pin = rt.read(pinned)
    if pin.get('run_id') != run_id or pin.get('episode') != episode:
        raise RuntimeError('실행 기록이 요청과 다릅니다: ' + str(pinned))
    new, changed = code_drift(cfg, pin)
    if changed and start != 'prepare_raw':
        raise RuntimeError('이 실행을 시작한 뒤 파이프라인 코드·설정이 바뀌었습니다(' + ', '.join(changed[:8]) +
                           (' 외' if len(changed) > 8 else '') + '). 앞 단계 결과가 예전 코드로 만들어져 중간부터 다시 할 수 없습니다. '
                           '처음(RAW 준비)부터 초기화하거나 새 실행으로 시작하세요.')

    when = stamp()
    archive = project / '99_history/reset' / episode / when
    moved = []
    # 뒤 단계부터 옮깁니다(앞 단계를 가리키는 링크는 옮길 때 다시 맞춰집니다).
    for stage in reversed(STARTS[STARTS.index(start):]):
        for rel in OUTPUTS.get(stage, []):
            src = project / rel / episode
            if src.exists() or src.is_symlink():
                move_aside(src, archive / rel / episode, log); moved.append(f'{rel}/{episode}')
    hands = project / '06_reconstruction' / episode / 'hands'
    if start == 'hands' and (hands.exists() or hands.is_symlink()):
        move_aside(hands, archive / 'hands' / episode, log); moved.append(f'06_reconstruction/{episode}/hands')
    manifest = project / '99_history/manifests' / f'{episode}.json'
    if start == 'prepare_raw' and manifest.is_file():
        (archive / 'manifests').mkdir(parents=True, exist_ok=True)
        shutil.move(str(manifest), str(archive / 'manifests' / manifest.name)); moved.append(str(manifest.relative_to(project)))

    # 최종 저장 결과: 어느 단계부터 초기화하든 다시 저장하므로 이전 결과를 옮겨 둡니다.
    published = receipt / 'published.json'
    result = None
    if published.is_file():
        rec = rt.read(published)
        dest = Path(rec.get('destination') or '')
        if rec.get('storage') == 'nas':
            target = sync_work.work_root(cfg) / '99_history/reset_results' / episode / when / dest.name
        else:
            target = archive / 'local_results' / dest.name
        if dest.name and dest.is_dir():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(dest), str(target))
            log(f'이전 최종 결과 보관: {dest} → {target}')
            try:
                dest.parent.rmdir()  # 결과 폴더(<ep>)가 비면 지웁니다.
            except OSError:
                pass
            result = {'from': str(dest), 'to': str(target)}
        published.rename(receipt / f'published.json.reset-{when}')

    if changed:
        shutil.copy2(pinned, receipt / f'identity.json.reset-{when}')
        pin.update(new)
        pinned.write_text(json.dumps(pin, indent=2))
        log('처음부터 다시 실행: 바뀐 코드로 실행 기록을 다시 고정했습니다 (' + ', '.join(changed[:8]) + ')')

    record = {'episode': episode, 'run_id': run_id, 'start': start, 'at': when, 'archive': str(archive),
              'moved': moved, 'restored_from_nas': restored, 'published_result': result, 'repinned': changed}
    archive.mkdir(parents=True, exist_ok=True)
    (archive / 'reset.json').write_text(json.dumps(record, ensure_ascii=False, indent=2))
    log(f'초기화 완료: {episode} · {start}부터 · 보관 {len(moved)}개 → {archive}')
    return record
