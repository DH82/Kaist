#!/usr/bin/env python3
"""GitHub(origin/main) 코드를 이 PC에 반영합니다. systemd --user 타이머(kaist-update.timer)가 몇 분마다 실행합니다.

요청과 상태는 NAS control/update/<pc>/ 에 둡니다.
  request.json  관리자 대시보드의 "업데이트" 버튼이 씁니다. 있으면 이 PC는 업데이트 대기 중입니다
                (UI가 새 RAW 실행·MANO 일괄 실행 시작을 막고, 진행 중인 작업은 계속 진행).
  status.json   이 스크립트가 매 실행마다 씁니다(현재 커밋, GitHub 최신 커밋, 진행 상태). 대시보드가 읽습니다.

Docker PC 순서 (한 번에 끝나지 않으면 다음 타이머 실행에서 이어서 진행):
  1) git fetch → 작업 트리에 수정이 없을 때만 fast-forward (실행 중인 컨테이너에는 영향 없음)
  2) 새 이미지 빌드 + GPU 자체 점검 (기존 서비스는 계속 동작)
  3) 이 PC가 쉴 때까지 대기: Worker 단계 작업·NAS 업로드·Airflow 실행 중 태스크·MANO 일괄 실행이 없을 것.
     파이프라인 코드(pipeline/)가 바뀌면 진행 중인 실행이 "Code/config changed during run"으로 실패하므로
     진행 중(queued/running) 실행이 모두 끝날 때까지 기다립니다.
  4) 서비스 재시작 + UI 연결 검사. 실패하면 이전 커밋으로 되돌려 다시 빌드·기동합니다.
Native PC(Docker 없이 systemd --user kaist-ui/kaist-airflow, 예: PC1)는 fast-forward 후 code/가 바뀌면
kaist-ui만 재시작합니다. GPU Worker·파이프라인 사본(~/Desktop/worker1)은 자동 배포하지 않습니다.

  python3 repro/update_stack.py            # 타이머가 부르는 한 번 실행
  python3 repro/update_stack.py --now      # 요청 없이 지금 업데이트 (쉬는지 확인은 동일)
  python3 repro/update_stack.py --status   # 상태만 출력
"""
import argparse
import fcntl
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from start_stack import read_env  # noqa: E402

BRANCH = 'main'
PINNED = ('pipeline/',)          # 실행 시작 때 해시를 고정하는 파이프라인 코드
STALE = 120                      # UI 상태 파일이 이보다 오래되면 '쉬는지 모름'으로 봅니다
LOCAL = ROOT / 'state' / 'updater'


def log(*a):
    print(time.strftime('%Y-%m-%d %H:%M:%S'), *a, flush=True)


def git(*args, check=True, timeout=120):
    r = subprocess.run(['git', '-C', str(ROOT), *args], capture_output=True, text=True, timeout=timeout)
    if check and r.returncode:
        raise RuntimeError(f'git {" ".join(args)}: {(r.stderr or r.stdout).strip()[-300:]}')
    return r.stdout.strip()


def is_ancestor(a, b):
    return subprocess.run(['git', '-C', str(ROOT), 'merge-base', '--is-ancestor', a, b]).returncode == 0


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name('.' + path.name + f'.{os.getpid()}.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    os.replace(tmp, path)


def read_json(path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


class Updater:
    def __init__(self, env_file):
        self.env_file = env_file
        self.env = read_env(env_file)
        self.pc = (self.env.get('PC_ID') or 'pc1').lower()
        nas = Path(self.env.get('NAS_MOUNT') or '/mnt/kaist-data')
        self.control = nas / 'control'
        self.dir = self.control / 'update' / self.pc
        self.local = read_json(LOCAL / 'state.json') or {}
        self.mode = os.environ.get('KAIST_UPDATE_MODE') or self.detect_mode()
        self.status = {}

    # ---------- 공통 ----------
    def detect_mode(self):
        # 실행 상태(is-active)는 재시작 중에 바뀌므로 보지 않습니다. native 설치(kaist-ui 사용자 서비스)가 있거나
        # ./start.sh(Docker)로 만든 .env가 아니면(COMPOSE_PROJECT_NAME 없음) native로 봅니다.
        r = subprocess.run(['systemctl', '--user', 'cat', 'kaist-ui.service'], capture_output=True, text=True)
        if r.returncode == 0 or 'COMPOSE_PROJECT_NAME' not in self.env:
            return 'native'
        return 'docker'

    def save_local(self, **kw):
        self.local.update(kw)
        write_json(LOCAL / 'state.json', self.local)

    def request(self):
        return read_json(self.dir / 'request.json')

    def finish_request(self, req):
        if not req:
            return
        try:
            cur = read_json(self.dir / 'request.json')
            if cur and cur.get('id') == req.get('id'):
                (self.dir / 'request.json').unlink()
        except OSError:
            pass

    def report(self, state, message='', **extra):
        head = git('rev-parse', 'HEAD')
        remote = git('rev-parse', f'origin/{BRANCH}', check=False) or None
        behind = int(git('rev-list', '--count', f'HEAD..origin/{BRANCH}', check=False) or 0) if remote else None
        req = self.request()
        self.status = dict(pc=self.pc, host=socket.gethostname(), mode=self.mode, state=state, message=message,
                           commit=head, subject=git('log', '-1', '--format=%s'), commit_time=int(git('log', '-1', '--format=%ct')),
                           remote=remote, remote_subject=git('log', '-1', '--format=%s', remote, check=False) if remote else None,
                           behind=behind, dirty=bool(self.dirty()), request=req, built=self.local.get('built'),
                           deployed=self.local.get('deployed'), last=self.local.get('last'), updated=time.time(), **extra)
        if self.control.is_dir():
            try:
                write_json(self.dir / 'status.json', self.status)
            except OSError as e:
                log('NAS 상태 기록 실패:', e)
        log(f'[{state}]', message)

    def dirty(self):
        # 추적 중인 파일 수정만 봅니다(.env·state 등은 .gitignore).
        return git('status', '--porcelain', '--untracked-files=no')

    def fetch(self):
        try:
            git('fetch', '--quiet', 'origin', BRANCH, timeout=180)
            return None
        except (RuntimeError, subprocess.TimeoutExpired) as e:
            return f'GitHub 확인 실패: {e}'

    def fast_forward(self):
        """origin/main으로 fast-forward. (이전 커밋, 바뀐 파일 목록)을 돌려줍니다."""
        head, remote = git('rev-parse', 'HEAD'), git('rev-parse', f'origin/{BRANCH}')
        if head == remote or is_ancestor(remote, head):
            return head, []
        if not is_ancestor(head, remote):
            raise RuntimeError('이 PC의 커밋이 GitHub와 갈라졌습니다. 직접 확인이 필요합니다.')
        if self.dirty():
            raise RuntimeError('이 PC에서 직접 고친 파일이 있어 업데이트하지 않습니다: ' + ', '.join(
                l.split(None, 1)[-1] for l in self.dirty().splitlines()[:5]))
        changed = git('diff', '--name-only', head, remote).splitlines()
        git('merge', '--ff-only', '--quiet', remote)
        return head, changed

    # ---------- 쉬는지 확인 ----------
    def worker_busy(self):
        """Worker 단계 작업(queued/running)과 NAS 업로드(running) 수. 컨테이너가 쓰는 jobs 폴더를 직접 읽습니다."""
        jobs = Path(self.env.get('GPU_STATE_DIR') or ROOT / 'gpu_state') / 'worker' / 'jobs'
        busy = []
        for d in jobs.iterdir() if jobs.is_dir() else []:
            st = read_json(d / 'status.json') if d.is_dir() else None
            if d.name == '_nas_sync':
                for s in d.iterdir():
                    x = read_json(s / 'status.json') if s.is_dir() else None
                    if x and x.get('state') == 'running':
                        busy.append('NAS 업로드 ' + s.name)
                continue
            if st and st.get('state') in ('queued', 'running'):
                req = read_json(d / 'request.json') or {}
                busy.append(f"{req.get('episode', d.name)} {req.get('stage', '')}".strip())
        return busy

    def ui_busy(self):
        """이 PC UI가 NAS에 올린 상태(20초마다)에서 Airflow 쪽 바쁨 정보를 읽습니다. 모르면 None."""
        path = self.control / 'status' / f'{self.pc}.sqlite'
        try:
            c = sqlite3.connect(f'file:{path}?mode=ro&immutable=1', uri=True)
            meta = {k: json.loads(v) for k, v in c.execute('SELECT key,value FROM meta')}
            runs = c.execute("SELECT count(*) FROM runs WHERE state IN ('queued','running')").fetchone()[0]
            c.close()
        except (sqlite3.Error, ValueError, OSError):
            return None
        if time.time() - float(meta.get('updated_at') or 0) > STALE or 'update_busy' not in meta:
            return None
        return dict(meta['update_busy'] or {}, inflight=runs)

    def idle_reasons(self, pinned):
        reasons = []
        busy = self.worker_busy()
        if busy:
            reasons.append(f'GPU 작업 {len(busy)}개 진행 중 (' + ', '.join(busy[:3]) + (' …' if len(busy) > 3 else '') + ')')
        ui = self.ui_busy()
        if ui is None:
            reasons.append('UI 상태를 확인할 수 없습니다 (UI가 꺼져 있거나 이전 버전)')
            return reasons
        if ui.get('error'):
            reasons.append('Airflow 상태 확인 실패: ' + str(ui['error'])[:120])
        if ui.get('running_tis'):
            reasons.append(f"Airflow 태스크 {ui['running_tis']}개 실행 중")
        if ui.get('batch'):
            reasons.append('MANO 일괄 실행 진행 중')
        if pinned and ui.get('inflight'):
            reasons.append(f"파이프라인 코드가 바뀌어 진행 중인 실행 {ui['inflight']}개가 끝날 때까지 대기")
        return reasons

    # ---------- 실행 ----------
    def start_stack(self, *flags):
        cmd = [sys.executable, str(ROOT / 'repro/start_stack.py'), '--env-file', str(self.env_file), *flags]
        log('실행:', ' '.join(cmd))
        with (LOCAL / 'last_run.log').open('a') as out:
            out.write(f'\n===== {time.strftime("%F %T")} {" ".join(flags) or "full"} =====\n')
            out.flush()
            r = subprocess.run(cmd, stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
        if r.returncode:
            tail = (LOCAL / 'last_run.log').read_text(errors='replace').strip().splitlines()[-3:]
            raise RuntimeError(f'start_stack {" ".join(flags)} 실패: ' + ' / '.join(tail)[-400:])

    def rollback(self, prev, restart):
        if not prev or prev == git('rev-parse', 'HEAD'):
            return ''
        git('reset', '--hard', '--quiet', prev)
        self.save_local(built=None)
        try:
            self.start_stack() if restart else self.start_stack('--build-only')
            self.save_local(built=prev, **({'deployed': prev} if restart else {}))
            return f' → 이전 커밋 {prev[:7]}로 되돌림'
        except RuntimeError as e:
            return f' → 이전 커밋 {prev[:7]}로 되돌렸지만 재기동 실패: {e}'

    def done(self, req, message):
        self.save_local(last={'at': time.time(), 'ok': True, 'message': message, 'request': (req or {}).get('id')}, prev=None, changed=[])
        self.finish_request(req)
        self.report('done', message)

    def fail(self, req, message):
        self.save_local(last={'at': time.time(), 'ok': False, 'message': message, 'request': (req or {}).get('id')}, prev=None, changed=[])
        self.finish_request(req)   # 실패한 요청은 반복하지 않습니다. 원인 확인 후 다시 요청하세요.
        self.report('failed', message)

    def tick(self, now=False, auto=False):
        fetch_error = self.fetch()
        req = self.request()
        if not req and now:
            req = {'id': 'manual_' + time.strftime('%Y%m%dT%H%M%S'), 'requested_by': 'local'}
        if not req and auto and not fetch_error and git('rev-parse', 'HEAD') != git('rev-parse', f'origin/{BRANCH}'):
            req = {'id': 'auto_' + git('rev-parse', '--short', f'origin/{BRANCH}'), 'requested_by': 'auto'}
        in_progress = self.local.get('prev') is not None
        if not req and not in_progress:
            return self.report('idle', fetch_error or '')
        if not req:
            # 대기 중에 관리자가 요청을 취소함 → 재시작 보류. 다시 요청하면 받아 둔 코드·이미지로 이어서 진행합니다.
            return self.report('paused', f"요청 취소로 재시작 보류 · 실행 중 {self.local['prev'][:7]} / 받아 둔 코드 {git('rev-parse', '--short', 'HEAD')}")
        if fetch_error and not in_progress:
            return self.report('waiting', fetch_error)
        try:
            prev, changed = self.fast_forward()
        except RuntimeError as e:
            return self.fail(req, str(e))
        if changed:
            # 이번 요청의 시작 커밋은 처음 한 번만 기록(빌드·대기 중 GitHub가 또 바뀌어도 되돌릴 곳은 같음).
            if self.local.get('prev') is None:
                self.save_local(prev=prev, changed=[])
            self.save_local(changed=sorted(set(self.local.get('changed') or []) | set(changed)))
            log(f'{prev[:7]} → {git("rev-parse", "--short", "HEAD")}: 파일 {len(changed)}개 변경')
        head = git('rev-parse', 'HEAD')
        prev = self.local.get('prev')
        changed = self.local.get('changed') or []
        return self.native(req, prev, changed) if self.mode == 'native' else self.docker(req, prev, head, changed)

    def native(self, req, prev, changed):
        notes = []
        if any(f.startswith('code/') for f in changed):
            r = subprocess.run(['systemctl', '--user', 'restart', 'kaist-ui'], capture_output=True, text=True)
            if r.returncode:
                return self.fail(req, 'kaist-ui 재시작 실패: ' + r.stderr.strip()[-200:])
            notes.append('UI 재시작')
        if any(f.startswith(('worker/', 'pipeline/', 'gpu/')) for f in changed):
            notes.append('Worker·파이프라인 변경은 native PC에서 자동 배포하지 않습니다(수동 배포 필요)')
        head = git('rev-parse', 'HEAD')
        self.save_local(deployed=head, built=head)
        self.done(req, ' · '.join([f'{prev[:7]} → {head[:7]}' if prev else '이미 최신'] + notes))

    def docker(self, req, prev, head, changed):
        if self.local.get('built') != head:
            self.report('building', f'{head[:7]} 이미지 빌드·GPU 점검 중 (기존 서비스는 계속 동작)')
            try:
                self.start_stack('--build-only')
            except RuntimeError as e:
                return self.fail(req, str(e) + self.rollback(prev, restart=False))
            self.save_local(built=head)
        if self.local.get('deployed') == head and not changed:
            return self.done(req, '이미 최신입니다')
        pinned = any(f.startswith(PINNED) for f in changed)
        reasons = self.idle_reasons(pinned)
        if reasons:
            return self.report('waiting', '빌드 완료 · 이 PC가 쉬면 재시작합니다: ' + ' · '.join(reasons))
        self.report('restarting', f'{head[:7]}로 서비스 재시작 중')
        try:
            self.start_stack('--skip-build')
        except RuntimeError as e:
            return self.fail(req, str(e) + self.rollback(prev, restart=True))
        self.save_local(deployed=head)
        self.done(req, (f'{prev[:7]} → ' if prev else '') + f'{head[:7]} 반영 완료')


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--env-file', type=Path, default=ROOT / '.env')
    ap.add_argument('--now', action='store_true', help='관리자 요청 없이 지금 업데이트')
    ap.add_argument('--auto', action='store_true', default=os.environ.get('KAIST_AUTO_UPDATE') == '1',
                    help='GitHub에 새 커밋이 있으면 요청 없이 업데이트 (환경변수 KAIST_AUTO_UPDATE=1과 같음)')
    ap.add_argument('--status', action='store_true', help='상태만 출력')
    args = ap.parse_args()
    LOCAL.mkdir(parents=True, exist_ok=True)
    with (LOCAL / 'lock').open('w') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log('다른 업데이트가 진행 중입니다.')
            return
        u = Updater(args.env_file.resolve())
        if args.status:
            u.fetch()
            u.report((read_json(u.dir / 'status.json') or {}).get('state', 'idle'), '상태 조회')
            print(json.dumps(u.status, ensure_ascii=False, indent=2))
            return
        try:
            u.tick(now=args.now, auto=args.auto)
        except Exception as e:  # 상태 파일에 남겨 대시보드에서 보이게 합니다.
            u.report('failed', f'업데이트 오류: {e}')
            raise


if __name__ == '__main__':
    main()
