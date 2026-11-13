#!/usr/bin/env python3
"""Prepare portable RAW workspaces. Python standard library; no GPU needed."""
import argparse
import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import socket
import sys
import uuid

EP = re.compile(r'ep_[0-9_]+$')
SKIP = {'.git', '__pycache__', '10_tasks', '20_results', '_internal'}


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.tmp')
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    tmp.replace(path)


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def validate(source):
    required = ['extrinsics.json']
    for cam in range(1, 5):
        required += [f'cam_{cam}{s}' for s in ['.mp4', '_intrinsics.json', '_timestamps.csv']]
        d = source / f'cam_{cam}_depth'
        if not d.is_dir() or not any(p.is_file() for p in d.iterdir()):
            raise ValueError(f'깊이 폴더 없음/비어 있음: {d}')
    for name in required:
        p = source / name
        if not p.is_file() or p.stat().st_size == 0:
            raise ValueError(f'필수 파일 없음/비어 있음: {p}')
    for p in source.rglob('*'):
        if p.is_symlink():
            raise ValueError(f'RAW 내부 심볼릭 링크는 지원하지 않습니다: {p}')
    if any((source / n).exists() for n in ['metadata.json', 'final_data.pkl', 'color', 'mask']):
        raise ValueError(f'처리 결과가 섞인 폴더입니다. RAW 원본을 지정하세요: {source}')


def discover(source, work):
    if EP.fullmatch(source.name):
        validate(source)
        return [source]
    found = {}
    for current, dirs, _ in os.walk(source, followlinks=False):
        dirs[:] = sorted(d for d in dirs if d not in SKIP and not d.startswith('.')
                         and not (Path(current)/d).is_symlink()
                         and (Path(current)/d).resolve() != work)
        p = Path(current)
        marker = p/'run.json'
        if marker.is_file():
            try:
                managed = json.loads(marker.read_text())
                if managed.get('schema_version') == 1 and managed.get('input_fingerprint') and managed.get('layout', {}).get('data') == 'data':
                    dirs[:] = []
                    continue
            except (ValueError, OSError):
                pass
        if EP.fullmatch(p.name):
            validate(p)
            if p.name in found:
                raise ValueError(f'동일 에피소드 이름 충돌:\n{found[p.name]}\n{p}\n입력 폴더를 좁혀 주세요.')
            found[p.name] = p
            dirs[:] = []
    if not found:
        raise ValueError(f'RAW ep_... 에피소드를 찾지 못했습니다: {source}')
    return [found[k] for k in sorted(found)]


def inventory(source):
    records = []
    for p in sorted(source.rglob('*')):
        if p.is_file():
            records.append({'path': p.relative_to(source).as_posix(), 'bytes': p.stat().st_size, 'sha256': digest(p)})
    fingerprint = hashlib.sha256(json.dumps(records, sort_keys=True).encode()).hexdigest()
    return {'episode': source.name, 'fingerprint': fingerprint, 'files': records,
            'bytes': sum(p['bytes'] for p in records), 'source_at_import': str(source)}


def verify(folder, record):
    if folder.is_symlink():raise ValueError('RAW root must not be a symlink')
    entries=list(folder.rglob('*'))
    if any(p.is_symlink() for p in entries):raise ValueError('RAW symlink found')
    expected={r['path'] for r in record['files']}
    if len(expected)!=len(record['files']) or any(Path(n).is_absolute() or '..' in Path(n).parts for n in expected):raise ValueError('Unsafe RAW manifest')
    actual={p.relative_to(folder).as_posix() for p in entries if p.is_file()}
    if actual!=expected:raise ValueError('RAW file set changed: missing='+str(sorted(expected-actual)[:3])+' extra='+str(sorted(actual-expected)[:3]))
    for item in record['files']:
        p = folder / item['path']
        if not p.is_file() or p.stat().st_size != item['bytes'] or digest(p) != item['sha256']:
            raise ValueError(f'복사 무결성 불일치: {p}')


def stage_copy(source, target, record):
    partial = target.with_name('.' + target.name + '.partial')
    if target.exists():
        raise ValueError(f'미등록 출력이 이미 있습니다. 덮어쓰지 않습니다: {target}')
    if partial.is_symlink():
        raise ValueError(f'임시 경로가 심볼릭 링크입니다: {partial}')
    if partial.exists():
        # Only a private, content-addressed run's unfinished copy is resumed.
        expected = {x['path'] for x in record['files']}
        extra = [str(p) for p in partial.rglob('*') if p.is_symlink() or (p.is_file() and p.relative_to(partial).as_posix() not in expected)]
        if extra:
            raise ValueError('부분 복사에 예상하지 못한 파일이 있습니다: ' + ', '.join(extra[:3]))
    partial.mkdir(parents=True, exist_ok=True)
    for item in record['files']:
        src, dst = source/item['path'], partial/item['path']
        if dst.is_file() and dst.stat().st_size == item['bytes'] and digest(dst) == item['sha256']:
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
    verify(partial, record)
    partial.rename(target)


def prepare(source, work, fresh=False):
    source = source.expanduser().resolve()
    work = work.expanduser().resolve()
    if not source.is_dir():
        raise ValueError(f'입력 폴더가 없습니다: {source}')
    episodes = discover(source, work)
    if any(work.is_relative_to(ep) for ep in episodes):
        raise ValueError('작업 루트를 RAW 에피소드 내부에 둘 수 없습니다.')
    records = []
    for ep in episodes:
        print(f'RAW 검사·SHA256: {ep.name}', flush=True)
        records.append(inventory(ep))
    group = hashlib.sha256(json.dumps([(r['episode'],r['fingerprint']) for r in records]).encode()).hexdigest()
    run_id = 'run-' + group[:16]
    if fresh:
        run_id += '-' + datetime.datetime.now().strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:6]
    run = work/'runs'/run_id
    run.mkdir(parents=True, exist_ok=True)
    with (run/'.prepare.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError(f'동일 입력의 준비 작업이 실행 중입니다: {run}')
        manifest = run/'run.json'
        if manifest.exists():
            state = json.loads(manifest.read_text())
            if state.get('input_fingerprint') != group:
                raise ValueError('기존 실행의 입력 지문이 다릅니다.')
        else:
            if any((run/'data'/r['episode']).exists() for r in records):
                raise ValueError('manifest 없는 기존 작업 데이터는 재사용하지 않습니다.')
            state = {'schema_version': 1, 'run_id': run_id, 'input_fingerprint': group,
                     'created_at': datetime.datetime.now().astimezone().isoformat(),
                     'import_host': socket.gethostname(), 'status': 'preparing',
                     'layout': {'data': 'data', 'reviews': 'stepper', 'logs': 'logs', 'deliverables': 'deliverables', 'manifests': 'manifests'},
                     'episodes': {}, 'runtime': 'external; reconnect environments/models on each PC'}
            write_json(manifest, state)
        for name in state['layout'].values():
            (run/name).mkdir(exist_ok=True)
        for src, rec in zip(episodes, records):
            ep = rec['episode']; target = run/'data'/ep
            receipt = run/'manifests'/f'{ep}.raw.json'
            if state['episodes'].get(ep, {}).get('status') == 'prepared':
                if not receipt.is_file() or not target.is_dir():
                    raise ValueError(f'준비 기록과 파일이 일치하지 않습니다: {ep}')
                if json.loads(receipt.read_text()).get('fingerprint') != rec['fingerprint']:
                    raise ValueError(f'RAW 기록이 일치하지 않습니다: {ep}')
                print(f'이미 준비됨: {ep} (처리 중인 작업 데이터는 덮어쓰지 않음)', flush=True)
                continue
            if target.exists():
                # Recover a crash between the verified rename and manifest update.
                verify(target, rec)
            else:
                free = shutil.disk_usage(run).free
                partial = target.with_name('.' + target.name + '.partial')
                remaining = sum(item['bytes'] for item in rec['files']
                                if not ((partial/item['path']).is_file()
                                        and (partial/item['path']).stat().st_size == item['bytes']
                                        and digest(partial/item['path']) == item['sha256']))
                if free < remaining + 64*1024*1024:
                    raise ValueError(f'복사 여유 공간 부족: {run}')
                print(f'복사·검증: {ep} ({rec["bytes"]/1024**2:.1f} MiB)', flush=True)
                stage_copy(src, target, rec)
            write_json(receipt, rec)
            (run/'stepper'/ep).mkdir(exist_ok=True)
            state['episodes'][ep] = {'status': 'prepared', 'data': f'data/{ep}', 'raw_manifest': f'manifests/{ep}.raw.json', 'review': f'stepper/{ep}'}
            write_json(manifest, state)
        state['status'] = 'prepared'
        write_json(manifest, state)
        write_json(work/'latest_run.json', {'run': run.relative_to(work).as_posix()})
        (run/'README.txt').write_text('WFM 작업 묶음\n\nrun.json의 모든 작업 경로는 이 폴더 기준 상대 경로입니다.\ndata/: 처리 작업 사본\nmanifests/: RAW 파일별 크기·SHA256 (초기 복사 검증용)\nstepper/: 후속 실행·휴먼 검수 기록\nlogs/: 실행 로그\ndeliverables/: 최종 ZIP·검수 문서·해시 저장 위치\n\n이 run 폴더 전체를 옮기면 데이터 경로를 유지할 수 있습니다. 원본 RAW는 입력 위치에 보존됩니다.\n가상환경·모델·CUDA·토큰은 포함하지 않습니다. 다른 PC에서는 환경을 별도로 연결해야 합니다.\n기존 22단계 run.py는 환경 절대 경로를 사용하므로 이 폴더를 --root로 넘기는 것만으로 실행되지는 않습니다.\n01 준비 완료는 02 QC 통과나 품질 승인을 뜻하지 않습니다.\nNAS는 RAW 입력 및 완료본 보관용으로 사용하고 계산은 로컬 SSD에서 진행하는 구성을 권장합니다.\n동일 입력 재실행은 기존 작업을 보존하며, 처리 결과를 초기 RAW와 다시 비교하지 않습니다. 새 실험은 --new-run.\n')
    print(f'\n01단계 준비 완료: {len(records)}개 에피소드\n작업 묶음: {run}\n산출물 저장 위치: {run / "deliverables"}\n다음 단계: 02 수집 품질 검사 (자동 실행하지 않음)')
    return run


def main():
    p = argparse.ArgumentParser(description='RAW 에피소드 또는 상위 폴더 → 자동 작업 디렉터리 구성')
    p.add_argument('input', type=Path, help='로컬 또는 마운트된 NAS의 RAW 폴더')
    p.add_argument('--work-root', type=Path, default=Path(os.environ.get('WFM_WORK_ROOT', str(Path(__file__).resolve().parent/'work'))), help='로컬 작업 루트. 기본: 스크립트 옆 work/ (WFM_WORK_ROOT로 변경 가능)')
    p.add_argument('--new-run', action='store_true', help='동일 RAW로 독립적인 새 실험 생성')
    p.add_argument('--check-only', action='store_true', help='RAW 구조와 발견 목록만 확인; 복사·폴더 생성 없음')
    a = p.parse_args()
    try:
        if a.check_only:
            source = a.input.expanduser().resolve()
            if not source.is_dir():raise ValueError(f'입력 폴더 없음: {source}')
            for ep in discover(source, a.work_root.expanduser().resolve()):print(ep)
        else:
            prepare(a.input, a.work_root, a.new_run)
    except (ValueError, OSError, shutil.Error) as exc:
        print(f'중단: {exc}\n원본은 변경하지 않았습니다. 부분 복사는 같은 명령으로 재개할 수 있습니다.', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
