#!/usr/bin/env python3
"""GitHub(origin/main) 코드를 이 PC에 반영합니다. systemd --user 타이머(kaist-update.timer)가 몇 분마다 실행합니다.

요청과 상태는 NAS control/update/<pc>/ 에 둡니다.
  request.json  관리자 대시보드의 "업데이트" 버튼이 씁니다. 있으면 이 PC는 업데이트 대기 중입니다
                (UI가 새 RAW 실행·MANO 일괄 실행 시작을 막고, 진행 중인 작업은 계속 진행).
  status.json   이 스크립트가 매 실행마다 씁니다(현재 커밋, GitHub 최신 커밋, 진행 상태). 대시보드가 읽습니다.

Docker PC 순서 (한 번에 끝나지 않으면 다음 타이머 실행에서 이어서 진행):
  1) git fetch → origin/main으로 맞춤 (실행 중인 컨테이너에는 영향 없음)
     이 PC에서 직접 고친 파일·GitHub와 갈라진 커밋이 있어도 멈추지 않고, state/updater/discarded/<시각>/ 에
     패치(local.patch)와 백업 브랜치(updater-backup/<시각>)로 보관한 뒤 GitHub 최신 코드로 덮어씁니다.
     GitHub보다 앞선(아직 올리지 않은) 커밋만 있으면 그대로 둡니다.
  2) 새 이미지 빌드 (기존 서비스는 계속 동작)
  3) 이 PC가 쉴 때까지 대기: Worker 단계 작업·NAS 업로드·Airflow 실행 중 태스크·MANO 일괄 실행이 없을 것.
     파이프라인 코드(pipeline/)가 바뀌면 진행 중인 실행이 "Code/config changed during run"으로 실패하므로
     검수·설정 입력 대기를 제외한 진행 중(queued/running) 실행이 끝날 때까지 기다립니다.
  4) GPU 자체 점검. SAM3·CUDA 확장·모델을 실제로 GPU에 올리므로 작업 중에 돌리면 메모리가 모자라 점검이
     실패하고(2026-11-17 PC5 10/12) 돌던 작업까지 망칠 수 있어, 쉴 때만 합니다.
  5) 서비스 재시작 + UI 연결 검사. 실패하면 이전 커밋으로 되돌려 다시 빌드·기동합니다.
  범위 줄이기(update_scope): 바뀐 파일이 UI 코드(code/platform/)뿐이면 UI 이미지만 빌드하고 3)·4) 없이 UI 컨테이너만,
     code/·deploy/뿐이면 3)에서 Airflow 쪽만 기다리고 4) 없이 Airflow·UI만 재시작합니다. 이미지에 안 들어가는 파일
     (업데이터·문서)뿐이면 재시작하지 않습니다. 전체 범위라도 GPU 이미지가 지난 점검 때와 같으면(캐시) 4)를 건너뜁니다.
     업데이터 자신(update_stack.py·start_stack.py)이 바뀌면 받은 직후 새 코드로 다시 실행해 이번 업데이트부터 적용합니다.
  강제 업데이트(관리자 '⚡ 강제 업데이트', request.json의 force): 1)~2)는 같고, 3)에서 기다리지 않습니다.
     stop.json을 써서 이 PC의 UI에 작업 강제 종료를 맡기고(파이프라인 실행·MANO 일괄 실행·GPU 작업, 검수·설정 입력 대기는 유지),
     UI가 stopped.json을 쓰면(또는 FORCE_UI_TIMEOUT 뒤) GPU 점검·재시작합니다. 종료된 실행은 '실패 단계 다시 실행'으로 잇습니다.
  자동 업데이트(--auto)가 실패한 커밋은 GitHub에 새 커밋이 올라오거나 관리자가 요청할 때까지 다시 시도하지 않습니다.
Native PC(Docker 없이 systemd --user kaist-ui/kaist-airflow, 예: PC1)는 fast-forward 후 code/가 바뀌면
kaist-ui만 재시작합니다. GPU Worker·파이프라인 사본(~/Desktop/worker1)은 자동 배포하지 않습니다.

  python3 repro/update_stack.py            # 타이머가 부르는 한 번 실행
  python3 repro/update_stack.py --now      # 요청 없이 지금 업데이트 (쉬는지 확인은 동일)
  python3 repro/update_stack.py --status   # 상태만 출력
"""
import argparse
from collections import deque
import fcntl
import json
import os
from pathlib import Path
import re
import socket
import sqlite3
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from start_stack import read_env  # noqa: E402

BRANCH = 'main'
PINNED = ('pipeline/',)          # 실행이 해시를 고정하는 파이프라인 코드(아래 두 범위로 나눠 판단)
# 실행 시작(prepare_raw) 때 고정되는 코드: 검수·설정 입력 대기를 제외한 실행을 기다립니다.
RUN_PINNED = ('pipeline/current/project/', 'pipeline/current/workspace/kaist-full/PhysTwin/Mono/data_process/',
              'pipeline/current/workspace/kaist-full/config.env',
              'worker/app/dags/sam_object_track.py', 'worker/app/dags/sam_object_point_track.py')
# 그 밖의 pipeline/ 코드(예: PhysTwin/data_process)는 3D 복원 단계에서 고정됩니다. 그 전 단계(마스크·추적 검수 대기 등)의
# 실행은 나중에 새 코드로 복원하므로 기다리지 않고, 3D 복원 이후 단계에 있는 실행만 기다립니다.
WAIT_STAGES = ('sam_settings_ready', 'mask_approved', 'tracking_approved')
LATE_STAGES = ('prepare_reconstruction', 'reconstruct', 'hands', 'validate', 'publish')
# ---- 업데이트 범위: 바뀐 파일만큼만 빌드·대기·점검·재시작합니다 ----
#  none    이미지에 안 들어가는 파일(업데이터·문서·설치 스크립트)만 → 빌드·재시작 없음
#  ui      UI 코드(code/platform/)만 → UI 이미지만 빌드, 기다리지 않고 UI 컨테이너만 재시작(GPU 작업·Airflow는 그대로)
#  airflow code/·deploy/(DAG 등) → UI 이미지만 빌드, Airflow 실행 중 태스크·MANO 일괄 실행만 기다린 뒤 Airflow·UI 재시작
#  full    그 밖(worker/·pipeline/·gpu/·Dockerfile·compose.yaml 등) → 전체 빌드, 지금까지와 같은 대기, GPU 점검, 전체 재시작
NO_IMAGE = ('repro/', 'tests/', 'backups/', 'native/', 'install_docker.sh', 'setup.sh', 'start.sh', 'pack.sh')
SCOPE_SERVICES = {'ui': ['ui'], 'airflow': ['airflow', 'ui']}
UPDATER_FILES = ('repro/update_stack.py', 'repro/start_stack.py')
IDLE_POLL = 15                   # 작업 종료 대기 중 다시 확인하는 간격(초)
IDLE_POLL_FOR = 165              # 한 번 실행에서 이만큼 기다려 보고, 그래도 바쁘면 다음 타이머 실행에서 이어감


def update_scope(changed):
    files = [f for f in changed if not (f.startswith(NO_IMAGE) or f.endswith('.md'))]
    if not files:
        return 'none'
    if all(f.startswith('code/platform/') for f in files):
        return 'ui'
    if all(f.startswith(('code/', 'deploy/')) for f in files):
        return 'airflow'
    return 'full'


def scope_flags(scope):
    return ['--services', *SCOPE_SERVICES[scope]] if scope in SCOPE_SERVICES else []


def gpu_image_id():
    r = subprocess.run(['docker', 'image', 'inspect', '--format', '{{.Id}}', 'kaist-gpu:' + (os.environ.get('KAIST_GPU_IMAGE_TAG') or 'latest')], capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else None


STALE = 120                      # UI 상태 파일이 이보다 오래되면 '쉬는지 모름'으로 봅니다
FORCE_UI_TIMEOUT = 180           # 강제 업데이트: UI가 이 시간 안에 작업 강제 종료를 끝내지 않으면(꺼짐·이전 버전) 그냥 재시작
FORCE_SETTLE = 60                # 강제 종료 뒤 GPU 작업 프로세스가 정리되기를 기다리는 최대 시간
LOCAL = ROOT / 'state' / 'updater'
REPORT_EVERY = 5                 # 빌드·재시작 중 NAS 상태 갱신 간격(초)
# 관리자 대시보드에 보이는 단계. rollback은 실패했을 때만 나타납니다.
PHASES = {'docker': [['fetch', '코드 받기'], ['build', '이미지 빌드'], ['wait', '작업 종료 대기'], ['check', 'GPU 점검'],
                     ['restart', '서비스 재시작'], ['verify', 'UI 확인']],
          'native': [['fetch', '코드 받기'], ['restart', 'UI 재시작']]}


class BuildProgress:
    """start_stack.py 출력(docker compose --progress plain + '==> [phase] x' 표시)에서 진행도를 뽑습니다."""
    STEP = re.compile(r'^#(\d+) \[([^\]]*?)\s*(\d+)/(\d+)\]\s*(.*)')
    END = re.compile(r'^#(\d+) (DONE|CACHED|ERROR|CANCELED)\b')
    OUT = re.compile(r'^#(\d+) \d+(?:\.\d+)? (.*)')
    PHASE = re.compile(r'^==> \[phase\] (\w+)')
    ANSI = re.compile(r'\x1b\[[0-9;?]*[A-Za-z]')

    def __init__(self):
        self.steps, self.totals, self.tail = {}, {}, deque(maxlen=15)

    def feed(self, line):
        """한 줄 처리. 단계 표시 줄이면 단계 이름을 돌려줍니다."""
        line = self.ANSI.sub('', line).rstrip()
        if not line.strip():
            return None
        m = self.PHASE.match(line)
        if m:
            return m.group(1)
        self.tail.append(line[:240])
        now = time.time()
        if m := self.STEP.match(line):
            sid, label, i, n, text = m.groups()
            label = label.strip() or 'build'
            self.totals[label] = int(n)
            self.steps.setdefault(sid, {'label': label, 'i': int(i), 'n': int(n), 'text': text[:160], 'start': now})
        elif m := self.END.match(line):
            st = self.steps.get(m.group(1))
            if st and 'end' not in st:
                st.update(end=now, result=m.group(2).lower())
        elif m := self.OUT.match(line):
            st = self.steps.get(m.group(1))
            if st:
                st['last'] = m.group(2)[:200]
        return None

    def snapshot(self):
        groups = []
        for label, total in self.totals.items():
            mine = [x for x in self.steps.values() if x['label'] == label]
            groups.append({'label': label, 'total': total, 'done': sum('end' in x for x in mine),
                           'cached': sum(x.get('result') == 'cached' for x in mine)})
        now = time.time()
        current = [{'label': x['label'], 'i': x['i'], 'n': x['n'], 'text': x['text'], 'last': x.get('last'),
                    'elapsed': round(now - x['start'])} for x in self.steps.values() if 'end' not in x]
        current.sort(key=lambda x: -x['elapsed'])
        return {'groups': groups, 'current': current[:4], 'tail': list(self.tail)}


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
        self.progress = None
        self.cur = ('idle', '')

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

    # ---------- 진행 기록 (관리자 대시보드용) ----------
    def begin_run(self, req):
        run = self.local.get('run')
        if run and run.get('id') == req.get('id'):
            if req.get('force') and not run.get('force'):   # 대기 중인 요청을 관리자가 강제로 바꿈
                run['force'] = True
                self.save_local(run=run)
            return
        self.save_local(forced=None)   # 이전(취소된) 강제 업데이트 기록은 새 요청에 섞지 않습니다
        self.save_local(run={'id': req.get('id'), 'requested_by': req.get('requested_by'), 'requested_at': req.get('at'), 'force': bool(req.get('force')),
                             'started': time.time(), 'from': git('rev-parse', 'HEAD'), 'phase': None, 'phases': {}})

    def set_phase(self, name):
        run = self.local.get('run')
        if not run or run.get('phase') == name:
            return
        now = time.time()
        if run.get('phase'):
            run['phases'][run['phase']].setdefault('end', now)
        run['phases'].setdefault(name, {'start': now})
        run['phases'][name].pop('end', None)
        run['phase'] = name
        self.save_local(run=run)

    def end_run(self, ok):
        run = self.local.get('run')
        if not run:
            return None
        now = time.time()
        if run.get('phase'):
            run['phases'][run['phase']].setdefault('end', now)
        run.update(finished=now, ok=ok, to=git('rev-parse', 'HEAD'))
        return run

    def finish_request(self, req):
        if not req:
            return
        try:
            cur = read_json(self.dir / 'request.json')
            if cur and cur.get('id') == req.get('id'):
                (self.dir / 'request.json').unlink()
        except OSError:
            pass
        for name in ('stop.json', 'stopped.json'):   # 강제 업데이트 주고받기 파일
            try:
                if (read_json(self.dir / name) or {}).get('id') == req.get('id'):
                    (self.dir / name).unlink()
            except OSError:
                pass

    def force_stop(self, req, reasons):
        """강제 업데이트: 이 PC의 UI에 작업 강제 종료를 맡깁니다. 남은 대기 사유(없으면 [] → 바로 재시작)를 돌려줍니다."""
        rid, now = req.get('id'), time.time()
        order = read_json(self.dir / 'stop.json') or {}
        if order.get('id') == rid and not reasons:
            # 이미 쉬고 있음(UI가 끝냈거나 작업이 스스로 끝남): UI가 남긴 종료 기록만 받아 두고 진행합니다.
            done = read_json(self.dir / 'stopped.json') or {}
            if done.get('id') == rid and not self.local.get('forced'):
                self.save_local(forced={'id': rid, 'stopped': done.get('stopped') or [], 'errors': done.get('errors') or []})
            return []
        if order.get('id') != rid:
            write_json(self.dir / 'stop.json', {'id': rid, 'at': now, 'reasons': reasons})
            log('강제 업데이트: UI에 작업 강제 종료 요청 · ' + ' · '.join(reasons))
            return ['강제 업데이트: 진행 중인 작업 강제 종료 요청함 (' + ' · '.join(reasons) + ')']
        done = read_json(self.dir / 'stopped.json') or {}
        if done.get('id') == rid:
            busy = [b for b in self.worker_busy() if not b.startswith('NAS 업로드')]   # NAS 업로드는 재시작이 끊습니다
            if busy and now - float(done.get('at') or now) < FORCE_SETTLE:
                return [f'강제 종료 후 GPU 작업 {len(busy)}개 정리 대기']
            if not self.local.get('forced'):
                self.save_local(forced={'id': rid, 'stopped': done.get('stopped') or [], 'errors': done.get('errors') or []})
            return []
        waited = now - float(order.get('at') or now)
        if waited >= FORCE_UI_TIMEOUT:
            if not self.local.get('forced'):
                log(f'강제 업데이트: UI가 {FORCE_UI_TIMEOUT}초 안에 응답하지 않아 그대로 재시작합니다')
                self.save_local(forced={'id': rid, 'stopped': [], 'errors': ['UI 응답 없음 · 재시작으로 작업 종료']})
            return []
        return [f'강제 업데이트: UI가 진행 중인 작업을 강제 종료하는 중 ({int(waited)}초)']

    def report(self, state, message='', **extra):
        self.cur = (state, message)
        head = git('rev-parse', 'HEAD')
        remote = git('rev-parse', f'origin/{BRANCH}', check=False) or None
        behind = int(git('rev-list', '--count', f'HEAD..origin/{BRANCH}', check=False) or 0) if remote else None
        req = self.request()
        self.status = dict(pc=self.pc, host=socket.gethostname(), mode=self.mode, state=state, message=message,
                           commit=head, subject=git('log', '-1', '--format=%s'), commit_time=int(git('log', '-1', '--format=%ct')),
                           remote=remote, remote_subject=git('log', '-1', '--format=%s', remote, check=False) if remote else None,
                           behind=behind, dirty=bool(self.dirty()), request=req, built=self.local.get('built'),
                           deployed=self.local.get('deployed'), last=self.local.get('last'), updated=time.time(),
                           phases=PHASES.get(self.mode), run=self.local.get('run'),
                           progress=self.progress.snapshot() if self.progress else None, **extra)
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

    def backup_local(self, head, files, diverged):
        """버리기 전에 직접 고친 내용(패치)과 커밋(백업 브랜치)을 보관합니다. 되살리기:
        git apply state/updater/discarded/<시각>/local.patch  또는  git log updater-backup/<시각>"""
        stamp = time.strftime('%Y%m%d-%H%M%S')
        d = LOCAL / 'discarded' / stamp
        d.mkdir(parents=True, exist_ok=True)
        if files:
            patch = subprocess.run(['git', '-C', str(ROOT), 'diff', '--binary', 'HEAD'], capture_output=True, timeout=120)
            (d / 'local.patch').write_bytes(patch.stdout)
        branch = f'updater-backup/{stamp}'
        git('branch', '--force', branch, head)
        write_json(d / 'info.json', {'at': time.time(), 'head': head, 'branch': branch, 'files': files, 'diverged': diverged})
        log(f'직접 고친 내용 보관: {d} (파일 {len(files)}개{" · 갈라진 커밋 " + branch if diverged else ""})')
        return f'state/updater/discarded/{stamp}'

    def fast_forward(self):
        """origin/main으로 맞춥니다. (이전 커밋, 바뀐 파일 목록)을 돌려줍니다.
        직접 고친 파일·갈라진 커밋은 보관(backup_local)한 뒤 버리고 GitHub 최신 코드로 덮어씁니다."""
        head, remote = git('rev-parse', 'HEAD'), git('rev-parse', f'origin/{BRANCH}')
        dirty = self.dirty()
        files = git('diff', '--name-only', 'HEAD').splitlines() if dirty else []
        ahead = head == remote or is_ancestor(remote, head)
        diverged = not ahead and not is_ancestor(head, remote)
        if files or diverged:
            where = self.backup_local(head, files, diverged)
            git('reset', '--hard', '--quiet', remote if diverged else 'HEAD')
            # 이미지가 고친 파일로 빌드됐을 수 있으므로 다시 빌드합니다.
            self.save_local(built=None, discarded={'at': time.time(), 'path': where, 'files': files, 'diverged': diverged})
        if ahead:
            return head, files
        changed = git('diff', '--name-only', head, remote).splitlines()
        if not diverged:
            git('merge', '--ff-only', '--quiet', remote)
        return head, sorted(set(changed) | set(files))

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
            active = c.execute("SELECT count(*) FROM runs WHERE state IN ('queued','running') "
                               "AND (stage IS NULL OR stage NOT IN (%s))" % ','.join('?' * len(WAIT_STAGES)), WAIT_STAGES).fetchone()[0]
            # 3D 복원 이후 단계(또는 단계를 모르는 실행 중) — 3D 복원 코드만 바뀐 업데이트가 기다릴 실행
            late = c.execute("SELECT count(*) FROM runs WHERE (state='running' AND (stage IS NULL OR stage IN (%s)))"
                             % ','.join('?' * len(LATE_STAGES)), LATE_STAGES).fetchone()[0]
            c.close()
        except (sqlite3.Error, ValueError, OSError):
            return None
        if time.time() - float(meta.get('updated_at') or 0) > STALE or 'update_busy' not in meta:
            return None
        return dict(meta['update_busy'] or {}, inflight=runs, inflight_active=active, inflight_late=late)

    def idle_reasons(self, pinned, scope='full'):
        reasons = []
        if scope == 'ui':
            return reasons   # UI 컨테이너만 재시작: GPU 작업·Airflow 실행에 영향 없음
        busy = self.worker_busy() if scope == 'full' else []   # airflow 범위는 GPU Worker를 재시작하지 않음
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
        if pinned == 'late':
            if ui.get('inflight_late', ui.get('inflight')):
                reasons.append(f"3D 복원 코드가 바뀌어 3D 복원 이후 단계의 실행 {ui.get('inflight_late', ui.get('inflight'))}개가 끝날 때까지 대기")
        elif pinned:
            count = ui.get('inflight') if pinned == 'all' else ui.get('inflight_active', ui.get('inflight'))
            if count:
                reasons.append(f"파이프라인 코드·설정이 바뀌어 진행 중인 실행 {count}개가 끝날 때까지 대기")
        return reasons

    # ---------- 실행 ----------
    def start_stack(self, *flags):
        cmd = [sys.executable, str(ROOT / 'repro/start_stack.py'), '--env-file', str(self.env_file), *flags]
        log('실행:', ' '.join(cmd))
        self.progress = BuildProgress()
        env = dict(os.environ, KAIST_COMPOSE_PROGRESS='plain', PYTHONUNBUFFERED='1')
        with (LOCAL / 'last_run.log').open('a') as out:
            out.write(f'\n===== {time.strftime("%F %T")} {" ".join(flags) or "full"} =====\n')
            out.flush()
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                    env=env, text=True, errors='replace', bufsize=1)
            last, failed = 0, []
            for line in proc.stdout:
                out.write(line)
                if line.startswith('✘'):   # gpu/selfcheck.py의 실패 항목
                    failed.append(line.strip()[:160])
                phase = self.progress.feed(line)
                if phase:
                    self.set_phase(phase)
                if phase or time.time() - last >= REPORT_EVERY:
                    out.flush()
                    self.report(*self.cur)
                    last = time.time()
            returncode = proc.wait()
        run = self.local.get('run')
        if run and self.progress.totals:
            run['build'] = {'groups': self.progress.snapshot()['groups'], 'flags': ' '.join(flags) or 'full', 'ok': returncode == 0}
            self.save_local(run=run)
        if returncode:
            tail = (LOCAL / 'last_run.log').read_text(errors='replace').strip().splitlines()[-3:]
            detail = ' / '.join(failed)[:600] + ' / ' if failed else ''
            raise RuntimeError(f'start_stack {" ".join(flags)} 실패: ' + detail + ' / '.join(tail)[-400:])

    def rollback(self, prev, restart, scope='full'):
        if not prev or prev == git('rev-parse', 'HEAD'):
            return ''
        self.set_phase('rollback')
        self.report('rollback', f'실패 → 이전 커밋 {prev[:7]}로 되돌리는 중')
        git('reset', '--hard', '--quiet', prev)
        self.save_local(built=None)
        flags = scope_flags(scope)
        light = ['--no-gpu-check'] if scope in SCOPE_SERVICES else []   # GPU 이미지는 그대로라 점검 불필요
        try:
            self.start_stack(*flags, *light) if restart else self.start_stack('--build-only', '--no-gpu-check', *flags)
            self.save_local(built=prev, checked=prev, **({'deployed': prev} if restart else {}))
            return f' → 이전 커밋 {prev[:7]}로 되돌림'
        except RuntimeError as e:
            return f' → 이전 커밋 {prev[:7]}로 되돌렸지만 재기동 실패: {e}'

    def done(self, req, message):
        gone = self.local.get('discarded')
        if gone:
            message += f" · 직접 고친 파일 {len(gone['files'])}개{'·갈라진 커밋' if gone.get('diverged') else ''}를 {gone['path']}에 보관하고 GitHub 코드로 덮어씀"
            self.save_local(discarded=None)
        run = self.end_run(True)
        self.save_local(last={'at': time.time(), 'ok': True, 'message': message, 'request': (req or {}).get('id'), 'run': run},
                        prev=None, changed=[], run=None)
        self.finish_request(req)
        self.report('done', message)

    def fail(self, req, message):
        message += self.forced_note()
        run = self.end_run(False)
        self.save_local(last={'at': time.time(), 'ok': False, 'message': message, 'request': (req or {}).get('id'), 'run': run},
                        prev=None, changed=[], run=None)
        self.finish_request(req)   # 실패한 요청은 반복하지 않습니다. 원인 확인 후 다시 요청하세요.
        self.report('failed', message)

    def clear_resolved_failure(self, fetch_error):
        """지난 실패의 원인이 사라졌으면(GitHub 최신을 포함하고 직접 고친 파일 없음) 대시보드에 계속 실패로 보이지 않게 표시합니다."""
        last = self.local.get('last')
        if not last or last.get('ok') or last.get('resolved') or fetch_error:
            return
        remote = git('rev-parse', f'origin/{BRANCH}', check=False)
        if self.dirty() or not remote or not is_ancestor(remote, git('rev-parse', 'HEAD')):
            return
        self.save_local(last=dict(last, resolved=time.time()))
        log('지난 실패 해소됨: 이미 GitHub 최신이고 직접 고친 파일이 없습니다')

    def tick(self, now=False, auto=False):
        fetch_error = self.fetch()
        req = self.request()
        if not req and now:
            req = {'id': 'manual_' + time.strftime('%Y%m%dT%H%M%S'), 'requested_by': 'local'}
        if not req and auto and not fetch_error and git('rev-parse', 'HEAD') != git('rev-parse', f'origin/{BRANCH}'):
            auto_id = 'auto_' + git('rev-parse', '--short', f'origin/{BRANCH}')
            last = self.local.get('last') or {}
            # 같은 커밋으로 자동 업데이트가 이미 실패했으면 타이머마다 빌드·되돌리기를 반복하지 않습니다.
            if self.local.get('prev') is not None or last.get('ok', True) or last.get('request') != auto_id:
                req = {'id': auto_id, 'requested_by': 'auto'}
        in_progress = self.local.get('prev') is not None
        if not req and not in_progress:
            self.clear_resolved_failure(fetch_error)
            return self.report('idle', fetch_error or '')
        if not req:
            # 대기 중에 관리자가 요청을 취소함 → 재시작 보류. 다시 요청하면 받아 둔 코드·이미지로 이어서 진행합니다.
            return self.report('paused', f"요청 취소로 재시작 보류 · 실행 중 {self.local['prev'][:7]} / 받아 둔 코드 {git('rev-parse', '--short', 'HEAD')}")
        self.begin_run(req)
        if fetch_error and not in_progress:
            self.set_phase('fetch')
            return self.report('waiting', fetch_error)
        if not in_progress:
            self.set_phase('fetch')
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
            # 업데이터 자신이 바뀌었으면 이번 업데이트부터 새 코드로 진행합니다(받은 변경 목록은 state.json에 남아 있음).
            if any(f in UPDATER_FILES for f in changed) and not os.environ.get('KAIST_UPDATER_REEXEC'):
                log('업데이터 코드가 바뀌어 새 코드로 다시 실행합니다')
                sys.stdout.flush()
                os.execve(sys.executable, [sys.executable, str(Path(__file__).resolve()), *sys.argv[1:]],
                          dict(os.environ, KAIST_UPDATER_REEXEC='1'))
        head = git('rev-parse', 'HEAD')
        prev = self.local.get('prev')
        changed = self.local.get('changed') or []
        return self.native(req, prev, changed) if self.mode == 'native' else self.docker(req, prev, head, changed)

    def native(self, req, prev, changed):
        notes = []
        self.set_phase('restart')
        if any(f.startswith('code/') for f in changed):
            r = subprocess.run(['systemctl', '--user', 'restart', 'kaist-ui'], capture_output=True, text=True)
            if r.returncode:
                return self.fail(req, 'kaist-ui 재시작 실패: ' + r.stderr.strip()[-200:])
            notes.append('UI 재시작')
        if any(f.startswith(('worker/', 'pipeline/', 'gpu/')) for f in changed):
            notes.append('Worker·파이프라인 변경은 native PC에서 자동 배포하지 않습니다(수동 배포 필요)')
        if 'repro/install_updater.sh' in changed:
            self.reinstall_timer()
        head = git('rev-parse', 'HEAD')
        self.save_local(deployed=head, built=head)
        self.done(req, ' · '.join([f'{prev[:7]} → {head[:7]}' if prev else '이미 최신'] + notes))

    def docker(self, req, prev, head, changed):
        scope = update_scope(changed)
        flags = scope_flags(scope)
        if scope == 'none':
            self.save_local(built=head, deployed=head)
            return self.done(req, (f'{prev[:7]} → ' if prev else '') + f'{head[:7]} 반영 (이미지에 들어가는 파일 변경 없음 · 재시작 안 함)')
        if self.local.get('built') != head:
            self.set_phase('build')
            what = {'ui': 'UI 이미지만', 'airflow': 'UI·Airflow 이미지만'}.get(scope, '이미지')
            self.report('building', f'{head[:7]} {what} 빌드 중 (기존 서비스는 계속 동작)')
            try:
                self.start_stack('--build-only', '--no-gpu-check', *flags)
            except RuntimeError as e:
                return self.fail(req, str(e) + self.rollback(prev, restart=False, scope=scope))
            self.save_local(built=head, checked=None)
        if self.local.get('deployed') == head and not changed:
            return self.done(req, '이미 최신입니다')
        pinned = ('all' if any(f.startswith('pipeline/') and not f.endswith('.py') for f in changed)
                  else True if any(f.startswith(RUN_PINNED) for f in changed)
                  else 'late' if any(f.startswith(PINNED) for f in changed) else False)
        if scope != 'ui':
            self.set_phase('wait')
            # 바쁘면 다음 타이머(몇 분 뒤)까지 기다리지 않고 IDLE_POLL초마다 다시 확인합니다.
            until = time.time() + IDLE_POLL_FOR
            while True:
                reasons = self.idle_reasons(pinned, scope)
                if req.get('force') and (reasons or (read_json(self.dir / 'stop.json') or {}).get('id') == req.get('id')):
                    reasons = self.force_stop(req, reasons)
                if not reasons or time.time() >= until:
                    break
                self.report('waiting', '빌드 완료 · 이 PC가 쉬면 ' + ('GPU 점검 후 ' if scope == 'full' else '') + '재시작합니다: ' + ' · '.join(reasons))
                time.sleep(IDLE_POLL)
                req = self.request() or req
            if reasons:
                return self.report('waiting', '빌드 완료 · 이 PC가 쉬면 ' + ('GPU 점검 후 ' if scope == 'full' else '') + '재시작합니다: ' + ' · '.join(reasons))
        if scope == 'full' and self.local.get('checked') != head:
            # GPU 이미지가 지난번 점검한 이미지와 같으면(층이 모두 캐시) 점검을 다시 하지 않습니다.
            image = gpu_image_id()
            if image and image == self.local.get('checked_gpu'):
                log('GPU 이미지가 지난 점검 때와 같아 GPU 점검을 건너뜁니다')
                self.save_local(checked=head)
            else:
                self.set_phase('check')
                self.report('building', f'{head[:7]} GPU 점검 중 (' + ('강제 종료 후' if self.local.get('forced') else '작업 없음 확인됨') + ')')
                try:
                    self.start_stack('--gpu-check-only')
                except RuntimeError as e:
                    return self.fail(req, str(e) + self.rollback(prev, restart=False, scope=scope))
                self.save_local(checked=head, checked_gpu=image)
        self.set_phase('restart')
        self.report('restarting', f'{head[:7]}로 ' + {'ui': 'UI만', 'airflow': 'Airflow·UI'}.get(scope, '서비스') + ' 재시작 중')
        try:
            self.start_stack('--skip-build', *flags)
        except RuntimeError as e:
            return self.fail(req, str(e) + self.rollback(prev, restart=True, scope=scope))
        self.save_local(deployed=head)
        if 'repro/install_updater.sh' in changed:
            self.reinstall_timer()
        label = {'ui': ' (UI만 재시작 · 작업 대기·GPU 점검 생략)', 'airflow': ' (Airflow·UI만 재시작 · GPU 점검 생략)'}.get(scope, '')
        self.done(req, (f'{prev[:7]} → ' if prev else '') + f'{head[:7]} 반영 완료' + label + self.forced_note())

    def reinstall_timer(self):
        """확인 주기 등 타이머 설정이 바뀌었으면 다시 설치합니다(자동 반영 여부는 유지)."""
        args = [str(ROOT / 'repro/install_updater.sh')] + (['--auto'] if os.environ.get('KAIST_AUTO_UPDATE') == '1' else [])
        r = subprocess.run(args, capture_output=True, text=True)
        log('업데이트 타이머 다시 설치: ' + ('완료' if r.returncode == 0 else '실패 ' + r.stderr.strip()[-200:]))

    def forced_note(self):
        forced = self.local.get('forced')
        if not forced:
            return ''
        self.save_local(forced=None)
        stopped, errors = forced.get('stopped') or [], forced.get('errors') or []
        return (f" · 강제 업데이트: 작업 {len(stopped)}개 강제 종료" + (f" ({', '.join(stopped[:5])}{' …' if len(stopped) > 5 else ''})" if stopped else '')
                + (' · ' + ' / '.join(errors)[:300] if errors else '') + " · 종료된 실행은 '실패 단계 다시 실행'으로 이어서 하세요")


def run_tick_hooks(env_file):
    """NAS control/update/tick_hooks.json 에 적힌 일회성 정비 작업을 타이머 실행마다 돌립니다.

    [{"name": "...", "cmd": ["python3", "{nas}/...", "--work", "{work}"], "until": "2026-11-28T00:00:00+09:00"}]
    {nas}=NAS 마운트, {work}=이 PC 작업 폴더(GPU_PROJECT_DIR), {pc}=PC 이름. until 이 지나면 건너뜁니다.
    작업은 스스로 멱등이어야 합니다(같은 일을 반복해도 결과가 같음). 실패해도 업데이트는 계속합니다.
    끄려면 NAS 파일에서 항목을 지우면 됩니다(다시 push할 필요 없음).
    """
    try:
        env = read_env(env_file)
        pc = (env.get('PC_ID') or 'pc1').lower()
        nas = Path(env.get('NAS_MOUNT') or '/mnt/kaist-data')
        hooks = read_json(nas / 'control' / 'update' / 'tick_hooks.json') or []
    except Exception as e:  # NAS가 안 보이면 다음 실행에서 다시 봅니다.
        log(f'정기 작업 목록을 읽지 못함: {e}')
        return
    work = env.get('GPU_PROJECT_DIR') or str(Path.home() / 'kaist_work' / pc)
    now = time.time()
    for h in hooks if isinstance(hooks, list) else []:
        name = str(h.get('name') or 'hook')
        try:
            until = h.get('until')
            if until and now > __import__('datetime').datetime.fromisoformat(until).timestamp():
                continue
            if pc in [str(x).lower() for x in h.get('skip_pcs') or []]:
                continue
            cmd = [str(a).replace('{nas}', str(nas)).replace('{work}', work).replace('{pc}', pc) for a in h['cmd']]
            if cmd and cmd[0] == 'python3':
                cmd[0] = sys.executable
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=int(h.get('timeout') or 300), stdin=subprocess.DEVNULL)
            out = (r.stdout + r.stderr).strip()
            if out or r.returncode:
                log(f'[정기 작업 {name}] 종료코드 {r.returncode}\n{out[-4000:]}')
                write_json(nas / 'control' / 'update' / pc / f'hook_{name}.json',
                           {'name': name, 'time': time.strftime('%Y-%m-%dT%H:%M:%S%z'), 'returncode': r.returncode, 'output': out[-20000:]})
        except Exception as e:
            log(f'[정기 작업 {name}] 실패: {e}')


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
        run_tick_hooks(args.env_file.resolve())
        try:
            u.tick(now=args.now, auto=args.auto)
        except Exception as e:  # 상태 파일에 남겨 대시보드에서 보이게 합니다.
            u.report('failed', f'업데이트 오류: {e}')
            raise
        finally:
            run_tick_hooks(args.env_file.resolve())


if __name__ == '__main__':
    main()
