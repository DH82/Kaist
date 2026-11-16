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
"""


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
        for table in ('gpu', 'runs', 'mano_waiting', 'published', 'commands'):
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
        for table in ('gpu', 'runs', 'mano_waiting', 'published', 'commands'):
            out[table] = [dict(r) for r in c.execute(f'SELECT * FROM {table}')]
        out['raw_started'] = [r[0] for r in c.execute('SELECT raw_episode FROM raw_started')]
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
