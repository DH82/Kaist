"""Worker-local persistent output folder registrations; no data files are moved."""
import fcntl,json,os,uuid
from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path
from fastapi import HTTPException
ROOT=Path(__file__).resolve().parent
REGISTRY=ROOT/'output_roots.json'

def protected(path):
 p=Path(path)
 if any(part.startswith('.') for part in p.parts if part not in ('.','..')):return True
 excluded=[ROOT,ROOT.parent/'state',ROOT.parent/'config',ROOT.parent/'venv']
 if any(p==x or p.is_relative_to(x) for x in excluded):return True
 return p.name in ('id_rsa','id_ed25519','config.env','simple_auth_manager_passwords.json.generated') or p.suffix.lower() in ('.pem','.key')

def work_bases():
 return [p for p in [Path.home(),Path('/mnt'),Path('/media'),Path('/data'),Path('/srv'),Path('/volume1'),Path('/volume2')] if p.is_dir()]

def validate_folder(value):
 if not isinstance(value,str) or not value.strip() or len(value)>4096:raise HTTPException(400,'폴더 경로를 입력하세요.')
 raw=Path(value.strip()).expanduser()
 if not raw.is_absolute():raise HTTPException(400,'작업자 PC의 전체 폴더 경로를 입력하세요.')
 p=raw.resolve()
 if not any(p==b or p.is_relative_to(b) for b in work_bases()) or protected(p):raise HTTPException(403,'작업 폴더를 선택하세요. 시스템·설정 폴더는 등록할 수 없습니다.')
 if not p.is_dir():raise HTTPException(404,'이 PC에 해당 폴더가 없습니다.')
 if not os.access(p,os.R_OK|os.X_OK):raise HTTPException(403,'이 폴더를 읽을 권한이 없습니다.')
 return p

def read_registry():
 if not REGISTRY.exists():return {'schema_version':1,'selected':'project','roots':[]}
 try:
  data=json.loads(REGISTRY.read_text())
  if data.get('schema_version')!=1 or not isinstance(data.get('roots'),list):raise ValueError()
  return data
 except (ValueError,OSError):raise HTTPException(409,'등록 폴더 설정을 읽지 못했습니다. 관리자에게 확인하세요.')

def save_registry(data):
 temp=REGISTRY.with_name(REGISTRY.name+'.'+uuid.uuid4().hex+'.tmp')
 with temp.open('x') as f:
  json.dump(data,f,ensure_ascii=False,indent=2);f.flush();os.fsync(f.fileno())
 temp.chmod(0o600);os.replace(temp,REGISTRY)

def register_folder(path,label):
 p=validate_folder(path);label=str(label).strip() or p.name
 if len(label)>80:raise HTTPException(400,'표시 이름은 80자 이내로 입력하세요.')
 with REGISTRY.with_suffix('.lock').open('a') as lock:
  fcntl.flock(lock,fcntl.LOCK_EX)
  data=read_registry();entry=next((x for x in data['roots'] if x['path']==str(p)),None)
  if entry is None:
   if len(data['roots'])>=50:raise HTTPException(400,'등록 가능한 폴더는 50개입니다.')
   entry={'id':'custom_'+uuid.uuid4().hex[:16],'path':str(p),'created_at':datetime.now(ZoneInfo('Asia/Seoul')).isoformat()};data['roots'].append(entry)
  entry['label']=label;data['selected']=entry['id'];save_registry(data)
 return entry

def select_folder(root,known):
 if root not in known:raise HTTPException(404,'등록되지 않은 폴더입니다.')
 with REGISTRY.with_suffix('.lock').open('a') as lock:
  fcntl.flock(lock,fcntl.LOCK_EX);data=read_registry();data['selected']=root;save_registry(data)
 return {'selected':root}

def browse_folders(path=''):
 p=validate_folder(path or str(Path.home()));entries=[]
 try:
  for child in p.iterdir():
   if child.name.startswith('.'):continue
   try:
    target=child.resolve()
    if not child.is_dir() or protected(target) or not any(target==base or target.is_relative_to(base) for base in work_bases()):continue
    if not os.access(target,os.R_OK|os.X_OK):continue
    entries.append({'name':child.name,'path':str(target)})
   except OSError:continue
 except OSError:raise HTTPException(403,'이 폴더의 하위 목록을 읽을 수 없습니다.')
 parent=p.parent
 if parent==p or not any(parent==base or parent.is_relative_to(base) for base in work_bases()) or protected(parent):parent=None
 return {'path':str(p),'parent':str(parent) if parent else None,'folders':sorted(entries,key=lambda x:x['name'].casefold())[:1000],
         'home':str(Path.home()),'pc':'PC 1 · RTX 4090','server':'192.168.200.17'}
