"""NAS intake catalogue. Assignment is metadata; it never moves or deletes RAW."""
import hashlib,json,os,re,sqlite3,threading
import finder_raw
from pathlib import Path
from datetime import datetime,timezone

def now():return datetime.now(timezone.utc).isoformat()
def db():
 p=Path(os.environ.get('KAIST_INTAKE_DB',str(Path(__file__).resolve().parents[1]/'intake_state/catalog.sqlite')));p.parent.mkdir(parents=True,exist_ok=True)
 c=sqlite3.connect(p,timeout=20);c.row_factory=sqlite3.Row
 c.execute('CREATE TABLE IF NOT EXISTS episodes (episode TEXT PRIMARY KEY, metadata TEXT NOT NULL, state TEXT NOT NULL, progress REAL NOT NULL, message TEXT NOT NULL, operator TEXT, worker TEXT, run_id TEXT, dag_id TEXT, updated TEXT NOT NULL)')
 return c

def rows():
 with db() as c:return [dict(r)|{'metadata':json.loads(r['metadata'])} for r in c.execute('SELECT * FROM episodes ORDER BY updated DESC')]
_scan_lock = threading.Lock()

def scan(include_manual=True):
 with _scan_lock:
  return _scan(include_manual)

def _scan(include_manual):
 raw=os.environ.get('KAIST_NAS_RAW_ROOT','')
 if not raw:return {'configured':False,'message':'NAS RAW 폴더가 아직 설정되지 않았습니다.','episodes':rows()}
 root=Path(raw)
 if not root.is_dir():raise ValueError('설정된 NAS RAW 폴더에 접근할 수 없습니다.')
 for folder in sorted(root.glob('ep_*')):
  manifest=folder/'upload_manifest.json'
  if folder.is_symlink() or not folder.is_dir() or not re.fullmatch(r'ep_[A-Za-z0-9_]+',folder.name):continue
  if not manifest.exists() and not include_manual:continue
  try:
   manual = not manifest.exists()
   if manual:
    m=finder_raw.manifest(folder)
   else:
    m=json.loads(manifest.read_text())
   ep=m['episode'];files=m['files']
   if ep!=folder.name or not re.fullmatch(r'ep_[A-Za-z0-9_]+',ep):raise ValueError('에피소드 이름 불일치')
   if not files or len(files)>100000:raise ValueError('파일 목록 오류')
   cal=m['calibration_file']
   if not m.get('calibration_id') or cal not in files:raise ValueError('캘리브레이션 연결 누락')
   expected=received=0;complete=True
   for name,rec in files.items():
    p=folder/name
    if Path(name).is_absolute() or '..' in Path(name).parts or not p.resolve().is_relative_to(folder.resolve()):raise ValueError('허용되지 않은 파일 경로')
    size=int(rec['size'])
    if size<0 or not re.fullmatch('[0-9a-f]{64}',rec['sha256']):raise ValueError('파일 명세 오류')
    expected+=size;actual=p.stat().st_size if p.is_file() else 0;received+=min(actual,size)
    complete=complete and p.is_file() and actual==size
   state='uploading';message='노트북 → NAS 전송 중'
   with db() as c:old=c.execute('SELECT * FROM episodes WHERE episode=?',(ep,)).fetchone()
   encoded=json.dumps(m,sort_keys=True)
   if old and old['metadata']!='{}' and old['metadata']!=encoded:
    state='invalid';message='등록 후 업로드 명세가 변경됐습니다. 새 에피소드로 등록하세요.'
   elif manual and complete:
    state='ready';message='NAS 직접 복사본 · 파일 구성과 체크섬 검사 완료'
   elif complete and (folder/'upload_complete.json').is_file():
    # Hash only once after transfer completion. RAW must remain immutable after receipt.
    if old and old['state']=='ready':state='ready';message=old['message']
    else:
     for name,rec in files.items():
      h=hashlib.sha256()
      with (folder/name).open('rb') as f:
       for chunk in iter(lambda:f.read(1024*1024),b''):h.update(chunk)
      if h.hexdigest()!=rec['sha256']:raise ValueError('파일 검증 실패: '+name)
     state='ready';message='파일·캘리브레이션 전송 검증 완료'
   with db() as c:
    c.execute('INSERT INTO episodes(episode,metadata,state,progress,message,updated) VALUES(?,?,?,?,?,?) ON CONFLICT(episode) DO UPDATE SET metadata=CASE WHEN episodes.metadata="{}" THEN excluded.metadata ELSE episodes.metadata END,state=excluded.state,progress=excluded.progress,message=excluded.message,updated=excluded.updated', (ep,encoded,state,round(100*received/max(expected,1),1),message,now()))
  except (ValueError,KeyError,OSError,TypeError) as e:
   ep=folder.name
   if re.fullmatch(r'ep_[A-Za-z0-9_]+',ep):
    with db() as c:c.execute('INSERT INTO episodes(episode,metadata,state,progress,message,updated) VALUES(?,?,?,?,?,?) ON CONFLICT(episode) DO UPDATE SET state=excluded.state,message=excluded.message,updated=excluded.updated',(ep,'{}','invalid',0,str(e),now()))
 return {'configured':True,'message':'NAS 입고 목록','episodes':rows()}

def assign(ep,operator,worker):
 if worker!='gpu_4090_a':raise ValueError('현재 등록된 실행 PC는 PC1뿐입니다.')
 if not operator.strip() or len(operator)>80:raise ValueError('작업자 이름을 입력하세요.')
 with db() as c:
  c.execute('BEGIN IMMEDIATE');r=c.execute('SELECT * FROM episodes WHERE episode=?',(ep,)).fetchone()
  if not r or r['state']!='ready':raise ValueError('업로드 검증 완료 후 배정할 수 있습니다.')
  if r['run_id']:raise ValueError('실행에 연결된 배정은 변경할 수 없습니다.')
  c.execute('UPDATE episodes SET operator=?,worker=?,updated=? WHERE episode=?',(operator.strip(),worker,now(),ep))

def for_launch(ep,operator=None):
 with db() as c:r=c.execute('SELECT * FROM episodes WHERE episode=?',(ep,)).fetchone()
 if not r:
  if not operator and os.environ.get('KAIST_NAS_RAW_ROOT'):raise ValueError('관리자 화면의 데이터 입고에서 입고 검사를 먼저 실행하세요.')
  return None
 if r['state']!='ready' or (not operator and (not r['operator'] or r['worker']!='gpu_4090_a')):raise ValueError('NAS 업로드 검증과 작업자 배정을 먼저 완료하세요.')
 return dict(r)
def bind(ep,run,dag):
 with db() as c:c.execute('UPDATE episodes SET run_id=?,dag_id=?,updated=? WHERE episode=?',(run,dag,now(),ep))
