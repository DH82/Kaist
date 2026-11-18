"""NAS 관제(PC1~PC6): PC별 상태 SQLite + 원격 명령 수신함.

NAS는 NFS v3(local_lock=none)라 여러 PC가 SQLite 파일 하나에 동시에 쓰면 잠금이 깨질 수 있습니다. 그래서
- 상태: 각 PC가 자기 파일 control/status/pcN.sqlite 만 씁니다. 로컬 임시 파일에 만든 뒤 NAS로 복사하고
  os.replace로 바꿔 끼우므로, 읽는 쪽은 항상 완성된 파일만 봅니다(읽기는 immutable 모드, 잠금 없음).
- 명령: 관리자가 대상 PC의 수신함 control/inbox/pcN/<id>.json 에 파일 하나를 씁니다(임시 파일 → rename).
  대상 PC는 rename으로 claimed/ 로 옮겨 한 번만 가져갑니다. 처리 결과는 그 PC의 상태 DB(commands)에 남습니다.

NAS의 control/status/*.sqlite 는 sqlite3 등으로 바로 열어 조회할 수 있습니다(읽기 전용으로 여세요).
"""
import json, os, re, secrets, shutil, sqlite3, tempfile, time
from pathlib import Path

PCS = [f'pc{i}' for i in range(1, 7)]
PC_ID = os.environ.get('PC_ID', 'pc1').strip().lower()
CONTROL = Path(os.environ.get('KAIST_CONTROL_DIR', os.environ.get('NAS_MOUNT', '/mnt/kaist-data') + '/control'))
STALE_SECONDS = 90          # 이 시간 넘게 상태 갱신이 없으면 연결 끊김
IDLE_UTIL = 10              # GPU 사용률(%) 미만
IDLE_FREE_MB = 16 * 1024    # 여유 GPU 메모리 이상

SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE gpu (idx INTEGER, name TEXT, uuid TEXT, used_mb INTEGER, total_mb INTEGER, util INTEGER, processes INTEGER);
CREATE TABLE runs (run_id TEXT, dag TEXT, mode TEXT, episode TEXT, raw_episode TEXT, operator TEXT, sam_prompt TEXT,
                   state TEXT, stage TEXT, started TEXT, ended TEXT);
CREATE TABLE mano_waiting (run_id TEXT, episode TEXT, raw_episode TEXT, operator TEXT, sam_prompt TEXT, status TEXT);
CREATE TABLE raw_started (raw_episode TEXT PRIMARY KEY);
CREATE TABLE published (sam_target TEXT, episode TEXT, run_key TEXT, at REAL);
CREATE TABLE commands (id TEXT PRIMARY KEY, type TEXT, episode TEXT, run_id TEXT, source_pc TEXT, target_pc TEXT, state TEXT,
                       step TEXT, message TEXT, requested_by TEXT, created REAL, updated REAL);
CREATE TABLE assignments (episode TEXT PRIMARY KEY, operator TEXT);
CREATE TABLE jobs (run_id TEXT, episode TEXT, stage TEXT, state TEXT, operator TEXT, sam_prompt TEXT,
                   created REAL, started REAL, finished REAL, attempt INTEGER);
CREATE TABLE frames (episode TEXT, raw_episode TEXT, synced INTEGER, prepared INTEGER, masks INTEGER, object_frames INTEGER, at REAL);
"""
TABLES = ('gpu', 'runs', 'mano_waiting', 'published', 'commands', 'assignments', 'jobs', 'frames')


def available():
    try:
        return CONTROL.parent.is_mount() or CONTROL.is_dir()
    except OSError:
        return False


def _replace_atomic(src, dest):
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name('.' + dest.name + '.' + secrets.token_hex(4) + '.tmp')
    shutil.copyfile(src, tmp)
    os.replace(tmp, dest)


def publish_status(snapshot):
    """snapshot(dict of table → rows, meta) → NAS control/status/<PC_ID>.sqlite"""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / 'status.sqlite'
        c = sqlite3.connect(path)
        c.executescript(SCHEMA)
        meta = dict(snapshot.get('meta', {}), pc=PC_ID, updated_at=time.time())
        c.executemany('INSERT INTO meta VALUES(?,?)', [(k, json.dumps(v, ensure_ascii=False)) for k, v in meta.items()])
        for table in TABLES:
            rows = snapshot.get(table) or []
            if rows:
                cols = list(rows[0])
                c.executemany(f'INSERT INTO {table} ({",".join(cols)}) VALUES ({",".join("?" * len(cols))})',
                              [tuple(r.get(k) for k in cols) for r in rows])
        c.executemany('INSERT OR IGNORE INTO raw_started VALUES(?)', [(e,) for e in snapshot.get('raw_started') or []])
        c.commit(); c.close()
        _replace_atomic(path, CONTROL / 'status' / f'{PC_ID}.sqlite')


def read_pc(pc):
    path = CONTROL / 'status' / f'{pc}.sqlite'
    if not path.is_file():
        return None
    try:
        c = sqlite3.connect(f'file:{path}?mode=ro&immutable=1', uri=True)
        c.row_factory = sqlite3.Row
        out = {'pc': pc, 'meta': {r['key']: json.loads(r['value']) for r in c.execute('SELECT * FROM meta')}}
        tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        # 이전 버전 PC의 상태 파일에는 없는 표가 있을 수 있습니다(예: assignments) → 빈 목록.
        for table in TABLES:
            out[table] = [dict(r) for r in c.execute(f'SELECT * FROM {table}')] if table in tables else []
        out['raw_started'] = [r[0] for r in c.execute('SELECT raw_episode FROM raw_started')]
        out['has_jobs'] = 'jobs' in tables      # 단계별 작업 시간 기록을 올리는 버전인지
        c.close()
    except (sqlite3.Error, ValueError) as e:
        return {'pc': pc, 'error': str(e)}
    age = time.time() - float(out['meta'].get('updated_at') or 0)
    out['age'] = age
    out['online'] = age < STALE_SECONDS
    out['busy_runs'] = sum(r['state'] in ('queued', 'running') for r in out['runs'])
    out['busy_commands'] = sum(c['state'] in ('received', 'running') and c['target_pc'] == pc for c in out['commands'])
    g = out['gpu'][0] if out['gpu'] else None
    out['idle'] = bool(out['online'] and g and out['busy_runs'] == 0 and out['busy_commands'] == 0
                       and (g['util'] or 0) < IDLE_UTIL and (g['total_mb'] or 0) - (g['used_mb'] or 0) >= IDLE_FREE_MB)
    return out


def read_all():
    return [read_pc(pc) or {'pc': pc, 'missing': True, 'online': False} for pc in PCS]


def started_elsewhere():
    """다른 PC에서 이미 시작한 RAW → {raw_episode: pc}. 같은 RAW를 두 PC가 따로 배정·실행하지 않게 합니다."""
    out = {}
    for pc in PCS:
        if pc == PC_ID:
            continue
        st = read_pc(pc)
        for ep in (st or {}).get('raw_started') or []:
            out.setdefault(ep, pc)
    return out


def send(target, command):
    if target not in PCS:
        raise ValueError('대상 PC가 잘못되었습니다.')
    command = dict(command, id=command.get('id') or time.strftime('%Y%m%dT%H%M%S_') + secrets.token_hex(4),
                   sent_at=time.time(), sent_from=PC_ID)
    if not re.fullmatch(r'[A-Za-z0-9_]+', command['id']):
        raise ValueError('명령 ID가 잘못되었습니다.')
    inbox = CONTROL / 'inbox' / target
    inbox.mkdir(parents=True, exist_ok=True)
    tmp = inbox / ('.' + command['id'] + '.tmp')
    tmp.write_text(json.dumps(command, ensure_ascii=False, indent=2))
    os.replace(tmp, inbox / (command['id'] + '.json'))
    return command


def claim():
    """이 PC 수신함의 새 명령을 가져갑니다(claimed/로 rename → 한 번만 처리)."""
    inbox = CONTROL / 'inbox' / PC_ID
    if not inbox.is_dir():
        return []
    claimed = inbox / 'claimed'
    claimed.mkdir(exist_ok=True)
    out = []
    for f in sorted(inbox.glob('*.json')):
        dest = claimed / f.name
        try:
            os.rename(f, dest)
            out.append(json.loads(dest.read_text()))
        except (OSError, ValueError):
            continue
    return out


# ---- 로그인 기준 PC 연결: 어느 PC 화면에서든 PCn으로 로그인하면 PCn의 UI로 넘겨줍니다 ----
# 이 UI가 돌아가는 PC. PC_ID 환경변수가 pc1~pc6일 때만 로그인 이름과 PC를 맞춥니다(미설정 = 단독 사용).
MACHINE_PC = PC_ID if os.environ.get('PC_ID', '').strip().lower() in PCS else None
HANDOFF_SECONDS = 60


def ui_url(allowed_hosts):
    """다른 PC가 이 UI로 보낼 주소. KAIST_UI_PUBLIC_URL, 없으면 허용 주소 중 LAN IP."""
    url = os.environ.get('KAIST_UI_PUBLIC_URL', '').strip().rstrip('/')
    if url:
        return url
    for host in sorted(allowed_hosts):
        name = host.rsplit(':', 1)[0]
        if re.fullmatch(r'\d+\.\d+\.\d+\.\d+', name) and not name.startswith('127.'):
            return 'http://' + host
    return None


def _handoff_path(pc, token):
    import hashlib
    return CONTROL / 'handoff' / pc / (hashlib.sha256(token.encode()).hexdigest() + '.json')


def handoff_create(pc, name, role):
    """1회용 로그인 넘김 토큰(60초). NAS에는 토큰의 해시 이름으로만 저장합니다."""
    if pc not in PCS:
        raise ValueError('PC 이름이 잘못되었습니다.')
    token = secrets.token_urlsafe(32)
    path = _handoff_path(pc, token)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name('.' + path.name + '.tmp')
    tmp.write_text(json.dumps({'pc': pc, 'name': name, 'role': role, 'from': PC_ID, 'expires': time.time() + HANDOFF_SECONDS}))
    os.replace(tmp, path)
    # 오래된 토큰 파일 정리
    for old in path.parent.glob('*.json'):
        try:
            if old.stat().st_mtime < time.time() - 10 * HANDOFF_SECONDS:
                old.unlink()
        except OSError:
            pass
    return token


def handoff_take(token):
    """이 PC로 넘어온 토큰을 한 번만 사용합니다(rename으로 가져가고 바로 지움)."""
    if not MACHINE_PC or not re.fullmatch(r'[A-Za-z0-9_-]{20,100}', token or ''):
        return None
    path = _handoff_path(MACHINE_PC, token)
    taken = path.with_name(path.name + '.taken.' + secrets.token_hex(4))
    try:
        os.rename(path, taken)
        data = json.loads(taken.read_text())
        taken.unlink()
    except (OSError, ValueError):
        return None
    if data.get('pc') != MACHINE_PC or float(data.get('expires') or 0) < time.time():
        return None
    return data


# ---- RAW 선점(PC 간 중복 방지): control/claims/<RAW> = 선점한 PC 이름을 가리키는 symlink ----
# symlink 생성은 NFS에서도 원자적이라(이미 있으면 실패) 두 PC가 동시에 배정해도 한 PC만 가져갑니다.
# 배정하거나 실행을 시작할 때 선점하고, 시작 전 배정이 풀리면 해제합니다. 실행을 시작한 RAW는 계속 유지합니다.
RAW_NAME = re.compile(r'ep_[0-9]{8}_[0-9]{6}')


def _claims():
    return CONTROL / 'claims'


def raw_owner(ep):
    try:
        return os.readlink(_claims() / ep)
    except OSError:
        return None


def raw_claim(ep):
    """이 PC가 RAW를 선점합니다. 이미 이 PC 것이면 True, 다른 PC 것이면 False. NAS를 못 쓰면 True(단독 사용)."""
    if not RAW_NAME.fullmatch(ep or ''):
        return True
    if not available():
        return True
    try:
        # 연속 모드로 다른 PC에 분배한 RAW는 선점이 비어 있어도 가져가지 않습니다.
        if os.readlink(_distributed() / ep) != PC_ID:
            return False
    except OSError:
        pass
    try:
        _claims().mkdir(parents=True, exist_ok=True)
        os.symlink(PC_ID, _claims() / ep)
        return True
    except FileExistsError:
        return raw_owner(ep) == PC_ID
    except OSError:
        return True


def raw_release(ep):
    if RAW_NAME.fullmatch(ep or '') and raw_owner(ep) == PC_ID:
        try:
            os.unlink(_claims() / ep)
        except OSError:
            pass


# ---- '내 작업에서 제외'한 RAW: control/excluded/<RAW> = 제외한 PC 이름을 가리키는 symlink ----
# 한 작업자가 제외한 RAW는 다른 작업자·다른 PC에도 배정·표시하지 않습니다. 제외 취소·대기열 초기화 때 그 PC가 지웁니다.
def _excluded():
    return CONTROL / 'excluded'


def raw_exclude(ep):
    if not RAW_NAME.fullmatch(ep or '') or not available():
        return
    try:
        _excluded().mkdir(parents=True, exist_ok=True)
        os.symlink(PC_ID, _excluded() / ep)
    except OSError:
        pass  # 이미 있음(다른 PC가 먼저 제외) 등


def raw_unexclude(ep):
    if not RAW_NAME.fullmatch(ep or ''):
        return
    try:
        if os.readlink(_excluded() / ep) == PC_ID:
            os.unlink(_excluded() / ep)
    except OSError:
        pass


def raw_excluded():
    """{RAW: 제외한 PC}"""
    out = {}
    try:
        for p in _excluded().iterdir():
            if RAW_NAME.fullmatch(p.name):
                try:
                    out[p.name] = os.readlink(p)
                except OSError:
                    pass
    except OSError:
        pass
    return out


def raw_claims():
    """{RAW: 선점한 PC}"""
    out = {}
    try:
        for p in _claims().iterdir():
            if RAW_NAME.fullmatch(p.name):
                try:
                    out[p.name] = os.readlink(p)
                except OSError:
                    pass
    except OSError:
        pass
    return out


# ---- SAM3 연속 모드 분배: control/distributed/<RAW> = 분배받은 PC 이름 symlink ----
# 관리자가 남은 RAW를 PC(작업자)별로 균등 분배할 때 씁니다. 같은 RAW의 선점(claims)도 그 PC로 만들어 다른 PC의
# 자동 배정과 겹치지 않게 하고, 받은 PC는 배정 행이 없어도 이 표시가 있는 동안 선점을 풀지 않습니다.
def _distributed():
    return CONTROL / 'distributed'


def raw_distribute(ep, pc):
    """RAW를 pc에 분배합니다. 다른 PC가 선점·분배한 RAW면 False."""
    if not RAW_NAME.fullmatch(ep or '') or pc not in PCS:
        return False
    for d in (_claims(), _distributed()):
        d.mkdir(parents=True, exist_ok=True)
        try:
            os.symlink(pc, d / ep)
        except FileExistsError:
            try:
                if os.readlink(d / ep) != pc:
                    return False
            except OSError:
                return False
    return True


def raw_undistribute(ep, pc):
    """pc에 분배한 RAW 표시와 그 PC의 선점을 풉니다(시작 전 RAW만 호출하세요)."""
    if not RAW_NAME.fullmatch(ep or ''):
        return
    for d in (_distributed(), _claims()):
        try:
            if os.readlink(d / ep) == pc:
                os.unlink(d / ep)
        except OSError:
            pass


def raw_distributed():
    """{RAW: 분배받은 PC}"""
    out = {}
    try:
        for p in _distributed().iterdir():
            if RAW_NAME.fullmatch(p.name):
                try:
                    out[p.name] = os.readlink(p)
                except OSError:
                    pass
    except OSError:
        pass
    return out


# ---- 작업자별 RAW 큐 크기: control/queue_targets/<이름>.json {"target": n, "by": ..., "at": ...} ----
# 작업자(PC1~PC6 로그인 이름)가 직접 정합니다. 배정할 때마다 NAS 파일을 읽으므로 어느 PC 화면에서 바꿔도 바로 반영됩니다.
QUEUE_MIN, QUEUE_MAX = 0, 30
QUEUE_NAME = re.compile(r'PC[1-6]')


def _queue_dir():
    return CONTROL / 'queue_targets'


def queue_target_get(name):
    """작업자가 정한 큐 크기. 정한 적 없거나 읽을 수 없으면 None."""
    if not QUEUE_NAME.fullmatch(name or '') or not available():
        return None
    try:
        v = json.loads((_queue_dir() / f'{name}.json').read_text()).get('target')
    except (OSError, ValueError, AttributeError):
        return None
    return v if isinstance(v, int) and not isinstance(v, bool) and QUEUE_MIN <= v <= QUEUE_MAX else None


def queue_target_set(name, target, by):
    """원자적으로 씁니다(임시 파일 → rename, NFS에서도 읽는 쪽은 완성된 파일만 봄)."""
    if not QUEUE_NAME.fullmatch(name or ''):
        raise ValueError('작업자 이름이 잘못되었습니다.')
    if not isinstance(target, int) or isinstance(target, bool) or not QUEUE_MIN <= target <= QUEUE_MAX:
        raise ValueError(f'큐 크기는 {QUEUE_MIN}~{QUEUE_MAX}개입니다.')
    d = _queue_dir()
    d.mkdir(parents=True, exist_ok=True)
    data = {'target': target, 'by': by, 'from_pc': PC_ID, 'at': time.time()}
    tmp = d / f'.{name}.{secrets.token_hex(4)}.tmp'
    tmp.write_text(json.dumps(data, ensure_ascii=False))
    os.replace(tmp, d / f'{name}.json')
    return data


def queue_targets():
    """{이름: {target, by, from_pc, at}} — 관리자 화면용"""
    out = {}
    try:
        for p in _queue_dir().glob('PC*.json'):
            try:
                out[p.stem] = json.loads(p.read_text())
            except (OSError, ValueError):
                pass
    except OSError:
        pass
    return out


def raw_taken_elsewhere():
    """다른 PC가 선점했거나 이미 시작한 RAW → {RAW: pc}"""
    out = {ep: pc for ep, pc in raw_claims().items() if pc != PC_ID}
    for ep, pc in raw_distributed().items():  # 연속 모드로 다른 PC에 분배한 RAW
        if pc != PC_ID:
            out.setdefault(ep, pc)
    for ep, pc in started_elsewhere().items():
        out.setdefault(ep, pc)
    return out


def overlaps(pcs):
    """PC 간 겹치는 RAW: 두 PC 이상이 시작 전 배정(assigned)했거나 실행(started)한 RAW.
    정리 기준(keep): 실행한 PC → NAS 선점 PC → 번호가 낮은 PC. 두 PC 이상이 실행했으면 manual(자동 정리 안 함)."""
    holders = {}
    for st in pcs:
        if not st or not st.get('meta'):
            continue
        started = set(st.get('raw_started') or [])
        for ep in started:
            holders.setdefault(ep, {})[st['pc']] = 'started'
        for a in st.get('assignments') or []:
            holders.setdefault(a['episode'], {}).setdefault(st['pc'], 'assigned')
    claims = raw_claims()
    out = []
    for ep, by in sorted(holders.items()):
        if len(by) < 2:
            continue
        ran = sorted(pc for pc, k in by.items() if k == 'started')
        if len(ran) > 1:
            keep = None                      # 두 PC 이상이 실행 → 결과가 있으므로 수동 확인
        elif ran:
            keep = ran[0]                    # 실행한 PC
        else:
            keep = claims.get(ep) if claims.get(ep) in by else sorted(by)[0]  # NAS 선점 PC → 번호 낮은 PC
        out.append({'episode': ep, 'holders': [{'pc': pc, 'kind': by[pc]} for pc in sorted(by)], 'claim': claims.get(ep),
                    'keep': keep, 'manual': len(ran) > 1,
                    'release': [pc for pc in sorted(by) if keep and pc != keep and by[pc] == 'assigned']})
    return out


# ---- GitHub 업데이트: control/update/<pc>/{request,status}.json ----
# request.json = 관리자가 요청(있는 동안 이 PC는 새 실행 시작을 막음), status.json = 호스트의 repro/update_stack.py가 기록.
def _update_dir(pc):
    if pc not in PCS:
        raise ValueError('대상 PC가 잘못되었습니다.')
    return CONTROL / 'update' / pc


def _read(path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def update_request(pc, by, force=False):
    """force: 강제 업데이트. 빌드가 끝나면 그 PC의 UI가 진행 중인 작업을 모두 강제 종료하고 바로 재시작합니다."""
    req = {'id': time.strftime('%Y%m%dT%H%M%S_') + secrets.token_hex(3), 'requested_by': by, 'at': time.time(), 'sent_from': PC_ID}
    if force:
        req['force'] = True
    d = _update_dir(pc)
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / ('.request.' + secrets.token_hex(4) + '.tmp')
    tmp.write_text(json.dumps(req, ensure_ascii=False, indent=2))
    os.replace(tmp, d / 'request.json')
    return req


def update_force(pc, by):
    """걸려 있는 요청을 강제 업데이트로 바꿉니다(같은 요청 id 유지 → 받아 둔 코드·빌드를 그대로 이어감). 요청이 없으면 새로 겁니다."""
    d = _update_dir(pc)
    req = _read(d / 'request.json')
    if not req:
        return update_request(pc, by, force=True)
    req.update(force=True, forced_by=by, forced_at=time.time())
    tmp = d / ('.request.' + secrets.token_hex(4) + '.tmp')
    tmp.write_text(json.dumps(req, ensure_ascii=False, indent=2))
    os.replace(tmp, d / 'request.json')
    return req


def update_cancel(pc):
    try:
        (_update_dir(pc) / 'request.json').unlink()
        return True
    except FileNotFoundError:
        return False


def update_info(pc):
    d = _update_dir(pc)
    return {'request': _read(d / 'request.json'), 'status': _read(d / 'status.json')}


# 강제 업데이트: updater가 빌드를 마치면 stop.json({id})을 쓰고, 그 PC의 UI가 작업을 강제 종료한 뒤 stopped.json({id, stopped, errors})을 씁니다.
def force_stop_order():
    """이 PC에 걸린 강제 업데이트의 '작업 강제 종료' 지시(요청 id가 같을 때만). 없으면 None."""
    if not MACHINE_PC:
        return None
    d = _update_dir(MACHINE_PC)
    req, order, done = _read(d / 'request.json'), _read(d / 'stop.json'), _read(d / 'stopped.json')
    if not (req and req.get('force') and order and order.get('id') == req.get('id')):
        return None
    if done and done.get('id') == order['id']:
        return None
    return req


def force_stop_done(req_id, result):
    d = _update_dir(MACHINE_PC)
    tmp = d / ('.stopped.' + secrets.token_hex(4) + '.tmp')
    tmp.write_text(json.dumps({'id': req_id, 'at': time.time(), **result}, ensure_ascii=False, indent=2))
    os.replace(tmp, d / 'stopped.json')


def update_pending():
    """이 PC에 업데이트 요청이 걸려 있으면 그 요청(dict), 없으면 None."""
    if not MACHINE_PC:
        return None
    return _read(_update_dir(MACHINE_PC) / 'request.json')
