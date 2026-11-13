import base64,hashlib,json,os,secrets,subprocess,sys,threading,time
from contextlib import asynccontextmanager
import asyncio
from functools import lru_cache
from pathlib import Path
from datetime import datetime,timezone
from urllib.parse import urlsplit,quote
import httpx
from fastapi import FastAPI,Request,HTTPException
from fastapi.responses import HTMLResponse,Response,StreamingResponse,FileResponse
from starlette.background import BackgroundTask
from pydantic import BaseModel,Field
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(Path(__file__).parent))
import intake,access,re
sys.path.insert(0,str(ROOT/'dags'))
from direct_worker import call,token,request_for,MANO_STAGES
from workers import workers,worker,dag_entries,dag_worker,DEFAULT_WORKER,DAY_DAG,BATCH_DAG,MAX_PARALLEL_RUNS
DIRECT_DAGS={"test":"kaist_mac_to_4090_test","pipeline":"kaist_direct_pipeline_4090","raw":"kaist_direct_raw_4090"}
if os.environ.get('KAIST_GPU_TEST_ENABLED','true').lower()=='false':DIRECT_DAGS.pop('test')
@asynccontextmanager
async def lifespan(app):
 task=None
 if os.environ.get('KAIST_AIRFLOW_API_URL'):
  task=asyncio.create_task(prepare_worker_pools())
 try:
  yield
 finally:
  if task:
   task.cancel()
   await asyncio.gather(task,return_exceptions=True)

app=FastAPI(docs_url=None,redoc_url=None,lifespan=lifespan)
CSRF=secrets.token_urlsafe(32)
ALLOWED_HOSTS=set(os.environ.get('KAIST_UI_ALLOWED_HOSTS','127.0.0.1:18083,localhost:18083').split(','))
AIRFLOW_API=os.environ.get('KAIST_AIRFLOW_API_URL','http://127.0.0.1:18082').rstrip('/')
AIRFLOW_PUBLIC=os.environ.get('KAIST_AIRFLOW_PUBLIC_URL','http://localhost:18082').rstrip('/')
AIRFLOW_USERS=Path(os.environ.get('KAIST_AIRFLOW_USERS_FILE',str(ROOT/'verified_state/simple_auth_manager_passwords.json.generated')))
UI_PASSWORD_FILE=os.environ.get('KAIST_UI_PASSWORD_FILE','')
lock=threading.Lock()

def selected_worker(req):
 wid=req.query_params.get('worker_id',DEFAULT_WORKER)
 try:worker(wid)
 except ValueError as e:raise HTTPException(400,str(e))
 return wid

def active_dags():
 return [(wid,mode,dag) for wid,mode,dag in dag_entries() if worker(wid,False)['enabled']]

@lru_cache(maxsize=32)
def review_queue_lock(worker_id):
 return threading.Lock()

review_queue_cache = {}

def worker_review_queue(pc):
 worker_id=pc['id']
 with review_queue_lock(worker_id):
  cached=review_queue_cache.get(worker_id)
  if cached and time.monotonic()<cached[0]:
   return cached[1]
  try:
   result=call('GET','/kaist-review/dags/'+pc['review_dag']+'/queue',worker_id=worker_id,timeout=15)
   items=[dict(row,worker_id=worker_id,worker_label=pc['label']) for row in result['reviews']
          if access.execution_worker(row['episode'])==worker_id]
  except (httpx.HTTPError,OSError,ValueError,subprocess.SubprocessError):
   return [{'worker_id':worker_id,'unavailable':True}]
  review_queue_cache[worker_id]=(time.monotonic()+15,items)
  return items

def all_review_items(worker_id=None):
 items=[]
 for pc in workers():
  if pc['enabled'] and (worker_id is None or pc['id']==worker_id):
   items.extend(worker_review_queue(pc))
 return items

@app.get('/api/workers')
def worker_list(req:Request):
 guard(req)
 return {'workers':[{'id':w['id'],'label':w['label'],'enabled':w['enabled']} for w in workers()]}

airflow_client = httpx.Client(base_url=AIRFLOW_API, trust_env=False, timeout=20)
airflow_auth_lock = threading.Lock()
airflow_token = None

def local_api(method, path, **kwargs):
 global airflow_token
 rejected = None
 for attempt in range(2):
  with airflow_auth_lock:
   if airflow_token is None or airflow_token == rejected:
    users = json.loads(AIRFLOW_USERS.read_text())
    name = os.environ.get('KAIST_AIRFLOW_USER') or next(iter(users))
    auth = airflow_client.post('/auth/token', json={'username': name, 'password': users[name]})
    auth.raise_for_status()
    airflow_token = auth.json()['access_token']
   current = airflow_token
  response = airflow_client.request(method, path, headers={'Authorization': 'Bearer ' + current}, **kwargs)
  if response.status_code == 401 and attempt == 0:
   rejected = current
   continue
  response.raise_for_status()
  return response.json()

def ensure_worker_pool(pc):
 path='/api/v2/pools/'+quote(pc['pool'],safe='')
 try:
  current=local_api('GET',path)
  # 예전 1슬롯 풀을 동시 실행 수에 맞춰 늘립니다.
  if current.get('slots',0)<MAX_PARALLEL_RUNS:
   # 실패해도 실행은 막지 않습니다(슬롯 1개로 순서 대기). Airflow 화면 Admin→Pools에서 직접 늘릴 수 있습니다.
   try:local_api('PATCH',path,params={'update_mask':'slots'},json={'name':pc['pool'],'slots':MAX_PARALLEL_RUNS,'include_deferred':current.get('include_deferred',False)})
   except httpx.HTTPError as exc:print('GPU 풀 슬롯 변경 실패:',pc['pool'],exc,flush=True)
  return
 except httpx.HTTPStatusError as exc:
  if exc.response.status_code!=404:raise
 try:
  local_api('POST','/api/v2/pools',json={'name':pc['pool'],'slots':MAX_PARALLEL_RUNS,'description':pc['label']})
 except httpx.HTTPStatusError as exc:
  if exc.response.status_code!=409:raise
  local_api('GET',path)

async def prepare_worker_pools():
 # NAS에서 UI가 Airflow보다 먼저 시작될 수 있습니다.
 while True:
  try:
   for pc in workers():
    if pc['enabled']:await asyncio.to_thread(ensure_worker_pool,pc)
   return
  except (OSError,httpx.HTTPError,ValueError) as exc:
   print('GPU 슬롯 초기화 재시도:',type(exc).__name__,flush=True)
   await asyncio.sleep(30)

def guard(req,nonce=True):
 if req.headers.get('host') not in ALLOWED_HOSTS:raise HTTPException(403,'허용되지 않은 주소입니다.')
 if req.method=='POST':
  if urlsplit(req.headers.get('origin','')).netloc!=req.headers.get('host'):raise HTTPException(403,'같은 화면에서 요청하세요.')
  if nonce and not secrets.compare_digest(req.headers.get('x-platform-csrf',''),CSRF):raise HTTPException(403,'화면을 새로고침하세요.')
 if req.url.path.startswith(('/api/','/kaist-review/')) and req.url.path not in ('/api/session','/api/login'):
  access.user(req)

@app.middleware('http')
async def errors(req,call_next):
 if UI_PASSWORD_FILE:
  expected_user=os.environ.get('KAIST_UI_USER','operator')
  try:
   scheme,encoded=req.headers.get('authorization','').split(' ',1)
   username,password=base64.b64decode(encoded,validate=True).decode().split(':',1)
   valid=scheme.lower()=='basic' and secrets.compare_digest(username.encode(),expected_user.encode()) and secrets.compare_digest(password.encode(),Path(UI_PASSWORD_FILE).read_text().strip().encode())
  except (ValueError,UnicodeError):valid=False
  if not valid:return Response('로그인이 필요합니다.',status_code=401,headers={'WWW-Authenticate':'Basic realm="KAIST"'},media_type='text/plain')
 try:return await call_next(req)
 except Exception as e:
  print(type(e).__name__,str(e)[:500],flush=True)
  return Response(json.dumps({'detail':'서버 연결 또는 처리에 실패했습니다. 연결 상태와 작업 로그를 확인하세요.'},ensure_ascii=False),status_code=502,media_type='application/json')

@app.get('/')
def home(req:Request):
 guard(req)
 return HTMLResponse((Path(__file__).parent/'index.html').read_text().replace('__CSRF__',CSRF),headers={'Cache-Control':'no-store','Content-Security-Policy':"default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; frame-src 'self'; img-src 'self' data: blob:; frame-ancestors 'none'"})

class Login(BaseModel):
 name:str=Field(min_length=1,max_length=80)
 role:str='operator'
 password:str=''

@app.get('/api/session')
def session(req:Request):
 guard(req)
 try:return access.user(req)
 except HTTPException:return {'name':None,'role':None}

@app.post('/api/login')
def login(data:Login,req:Request):
 guard(req)
 name=data.name.strip()
 if not name or data.role not in ('operator','admin'):raise HTTPException(400,'이름과 역할을 확인하세요.')
 if data.role=='admin':
  users=json.loads(AIRFLOW_USERS.read_text())
  expected=users.get(os.environ.get('KAIST_AIRFLOW_USER','admin'),'')
  if not expected or not secrets.compare_digest(data.password.encode(),expected.encode()):raise HTTPException(403,'관리자 비밀번호가 일치하지 않습니다.')
 token=access.login(name,data.role)
 response=Response(json.dumps({'name':name,'role':data.role}),media_type='application/json')
 response.set_cookie('kaist_session',token,httponly=True,samesite='strict',secure=req.url.scheme=='https',max_age=43200)
 return response

@app.post('/api/logout')
def logout(req:Request):
 guard(req)
 with access.db() as c:c.execute('DELETE FROM ui_sessions WHERE token=?',(req.cookies.get('kaist_session',''),))
 response=Response('{}',media_type='application/json');response.delete_cookie('kaist_session');return response

@app.get('/api/operators')
def operators(req:Request):
 guard(req);access.admin(req)
 with access.db() as c:names=[r[0] for r in c.execute("SELECT DISTINCT name FROM ui_sessions WHERE role='operator' ORDER BY name")]
 own=access.owners()
 return {'names':names,'episodes':[{'episode':ep,'operator':own.get(ep)} for ep in call('GET','/worker/info')['episodes']]}

@app.post('/api/operators/assign')
def operator_assign(data:dict,req:Request):
 guard(req);access.admin(req)
 ep=data.get('episode');name=str(data.get('operator','')).strip()
 if ep not in call('GET','/worker/info')['episodes'] or not name or len(name)>80:raise HTTPException(400,'에피소드와 이름을 확인하세요.')
 if any(r['episode']==ep for r in intake.rows()):
  try:intake.assign(ep,name,'gpu_4090_a')
  except ValueError as e:raise HTTPException(409,str(e))
 else:
  # Existing local episodes can be assigned without inventing NAS upload records.
  with access.db() as c:
   old=c.execute('SELECT operator FROM episode_owners WHERE episode=?',(ep,)).fetchone()
   if old and old[0]!=name:raise HTTPException(409,'기존 배정 변경은 아직 지원하지 않습니다.')
   c.execute('INSERT OR REPLACE INTO episode_owners VALUES(?,?)',(ep,name))
 access.audit(req,'assign '+ep+' '+name)
 return {'ok':True}

def raw_row(ep,owner,user):
 can_run=not owner or owner==user['name']
 visible=can_run or user['role']=='admin'
 return {'episode':ep,'operator':owner if visible else None,'can_run':can_run}

@app.get('/api/info')
def info(req:Request):
 guard(req)
 wid=selected_worker(req)
 d=call('GET','/worker/info',worker_id=wid)
 d['central_ready']=local_api('GET','/api/v2/monitor/health')['scheduler']['status']=='healthy'
 d['server_ready']=True
 d['airflow_public_url']=AIRFLOW_PUBLIC
 d['gpu_test_enabled']='test' in DIRECT_DAGS and access.user(req)['role']=='admin'
 owners=access.owners();user=access.user(req)
 d['raw_episodes']=[raw_row(ep,owners.get(ep),user) for ep in d['episodes']]
 d['episodes']=[ep for ep in d['episodes'] if access.selectable(req,ep)]
 return d

class Launch(BaseModel):
 worker_id:str=DEFAULT_WORKER
 mode:str
 defer_mano:bool=False
 episode_id:str=Field(default='',pattern=r'^(ep_[A-Za-z0-9_]+)?$')
 sam_prompt:str=Field(default='',max_length=200)
 confirmed:bool=False
 preview_episode:str=''
 sam_object_mode:str='text'
 sam_object_bboxes:dict|None=None

def object_bboxes(value):
 # 선택 옵션: 첫 프레임 bbox(카메라 1~4 → 0~3, 정규화 x,y,w,h). Worker에서도 같은 기준으로 다시 검사합니다.
 if not isinstance(value,dict) or sorted(value)!=['0','1','2','3']:raise HTTPException(400,'bbox 물체 추적은 카메라 4대 모두 첫 프레임에서 물체 bbox를 그려야 합니다.')
 result={}
 for cam,box in value.items():
  if not isinstance(box,list) or len(box)!=4 or any(isinstance(v,bool) or not isinstance(v,(int,float)) for v in box):raise HTTPException(400,'bbox 형식이 잘못되었습니다.')
  x,y,w,h=(round(float(v),4) for v in box)
  if not (0<=x<1 and 0<=y<1 and w>=0.01 and h>=0.01 and x+w<=1.0001 and y+h<=1.0001):raise HTTPException(400,f'카메라 {int(cam)+1} bbox가 화면 밖이거나 너무 작습니다.')
  result[cam]=[x,y,w,h]
 return result

@app.post('/api/run')
def launch(data:Launch,req:Request):
 guard(req)
 if data.mode=='test':access.admin(req)
 elif access.owners().get(data.episode_id) not in (None,access.user(req)['name']):raise HTTPException(403,'다른 작업자가 선택한 에피소드입니다.')
 if data.mode not in DIRECT_DAGS:raise HTTPException(400,'실행 모드를 선택하세요.')
 try:pc=worker(data.worker_id)
 except ValueError as e:raise HTTPException(400,str(e))
 if data.mode=='test' and data.worker_id!=DEFAULT_WORKER:raise HTTPException(400,'GPU test is PC1 only')
 dag=pc['dag_id'] if data.mode=='pipeline' else DIRECT_DAGS[data.mode]
 if data.mode=='raw' and data.worker_id!=DEFAULT_WORKER:raise HTTPException(400,'RAW-only compatibility mode is PC1 only')
 if data.defer_mano:
  if data.worker_id!=DEFAULT_WORKER or data.mode!='pipeline':raise HTTPException(400,'PC1 전체 파이프라인에서만 선택할 수 있습니다.')
  dag=DAY_DAG
 with lock:
  active=local_api('GET',f'/api/v2/dags/{dag}/dagRuns',params={'limit':100,'order_by':'-logical_date'})
  # MANO 일괄 실행도 PC1의 동시 실행 1개로 셉니다(예전에는 일괄 실행 중이면 모든 실행을 막았습니다).
  used_names=set();running_here=batch_active() if data.worker_id==DEFAULT_WORKER else 0
  for wid,other_mode,other_dag in active_dags():
   if wid!=data.worker_id:continue
   rows=active if other_dag==dag else local_api('GET',f'/api/v2/dags/{other_dag}/dagRuns',params={'limit':100,'order_by':'-logical_date'})
   for row in rows['dag_runs']:
    conf=row.get('conf') or {}
    used_names.add(conf.get('episode_id'))
    if row['state'] not in ('queued','running'):continue
    # 같은 RAW를 같은 PC에서 동시에 두 번 돌리지 않습니다.
    if data.mode in ('pipeline','raw') and (conf.get('raw_episode_id') or conf.get('episode_id'))==data.episode_id:raise HTTPException(409,'이 RAW는 선택한 PC에서 이미 실행 중입니다.')
    running_here+=1
  # PC 한 대에서 최대 MAX_PARALLEL_RUNS개까지 동시에 진행합니다.
  if running_here>=MAX_PARALLEL_RUNS:raise HTTPException(409,f'선택한 PC에서 이미 {running_here}개 작업이 실행 중입니다. (최대 {MAX_PARALLEL_RUNS}개 동시 실행)')
  if data.mode=='pipeline':
   for other_wid,_,other_dag in active_dags():
    if other_wid==data.worker_id:continue
    rows=local_api('GET',f'/api/v2/dags/{other_dag}/dagRuns',params={'limit':100,'order_by':'-logical_date'})['dag_runs']
    if any(r['state'] in ('queued','running') and (r.get('conf',{}).get('raw_episode_id') or r.get('conf',{}).get('episode_id'))==data.episode_id for r in rows):raise HTTPException(409,'이 RAW가 다른 PC에서 실행 중입니다.')
  conf={}
  if data.mode in ('raw','pipeline'):
   if data.mode=='pipeline' and data.preview_episode!=data.episode_id:raise HTTPException(400,'선택한 에피소드 이미지를 먼저 확인하세요.')
   if data.mode=='pipeline' and (not data.episode_id or not data.sam_prompt.strip() or not data.confirmed):raise HTTPException(400,'에피소드와 SAM3 대상을 입력하고 설정을 확인하세요.')
   if data.sam_object_mode not in ('text','bbox_track'):raise HTTPException(400,'물체 분할 방식을 확인하세요.')
   bboxes=object_bboxes(data.sam_object_bboxes) if data.mode=='pipeline' and data.sam_object_mode=='bbox_track' else None
   # Validate against live server episode list; no arbitrary path input.
   captures=call('GET','/worker/info',worker_id=data.worker_id)['episodes']
   used_names.update(ep for ep in captures if ep!=data.episode_id)
   if data.episode_id not in captures:raise HTTPException(400,'서버 RAW 목록에 없는 에피소드입니다.')
   catalog=call('GET','/worker/outputs/catalog',params={'root':'project'},worker_id=data.worker_id)
   used_names.update(ep['episode'] for group in catalog['groups'] for ep in group['episodes'])
   try:assignment=intake.for_launch(data.episode_id,operator=access.user(req)['name'])
   except ValueError as e:raise HTTPException(409,str(e))
   if assignment:
    worker_info=call('GET','/worker/info',worker_id=data.worker_id)
    expected=hashlib.sha256(assignment['metadata'].encode()).hexdigest()
    actual=worker_info.get('intake_manifests',{}).get(data.episode_id)
    if json.loads(assignment['metadata']).get('source')=='manual_nas_copy':
     actual=call('GET','/worker/intake/fingerprint',params={'episode':data.episode_id},worker_id=data.worker_id,timeout=600)['fingerprint']
    if actual!=expected:raise HTTPException(409,'GPU PC가 같은 NAS 에피소드를 읽는지 확인되지 않았습니다. Worker 업데이트와 NAS 마운트를 확인하세요.')
   conf={'episode_id':data.episode_id,'operator_id':access.user(req)['name'],'worker_id':data.worker_id}
   if data.mode=='pipeline':conf.update(sam_prompt=data.sam_prompt.strip(),sam_scene='2d',sam_settings_confirmed=True)
   if bboxes:conf.update(sam_object_mode='bbox_track',sam_object_bboxes=bboxes)
  ensure_worker_pool(pc)
  local_api('PATCH',f'/api/v2/dags/{dag}',json={'is_paused':False})
  now=datetime.now(timezone.utc);run='ui_'+now.strftime('%Y%m%dT%H%M%S_')+secrets.token_hex(3)
  with access.db() as ownership:
   ownership.execute('BEGIN IMMEDIATE')
   if data.mode in ('raw','pipeline'):
    old=ownership.execute('SELECT operator FROM episode_owners WHERE episode=?',(data.episode_id,)).fetchone()
    if old and old[0]!=access.user(req)['name']:raise HTTPException(409,'다른 작업자가 먼저 실행했습니다.')
    if not old:ownership.execute('INSERT INTO episode_owners VALUES(?,?)',(data.episode_id,access.user(req)['name']))
    ownership.execute('UPDATE episodes SET operator=COALESCE(operator,?),worker=COALESCE(worker,?) WHERE episode=?',(access.user(req)['name'],data.worker_id,data.episode_id))
   if data.mode in ('raw','pipeline'):
    number=ownership.execute('SELECT COALESCE(MAX(number),-1)+1 FROM execution_versions WHERE raw_episode=?',(data.episode_id,)).fetchone()[0]
    # 처음은 RAW와 같은 이름이고, 이미 사용된 작업 폴더는 재사용하지 않습니다.
    while True:
     output_episode=data.episode_id if number==0 else f'{data.episode_id}_{number}'
     if output_episode not in used_names and not ownership.execute('SELECT 1 FROM execution_versions WHERE episode=?',(output_episode,)).fetchone():break
     number+=1
    ownership.execute('INSERT INTO execution_versions VALUES(?,?,?,?,?)',(run,data.episode_id,output_episode,number,access.user(req)['name']))
    ownership.execute('INSERT INTO execution_workers VALUES(?,?,?)',(run,output_episode,data.worker_id))
    conf.update(raw_episode_id=data.episode_id,episode_id=output_episode,execution_number=number)
   result=local_api('POST',f'/api/v2/dags/{dag}/dagRuns',json={'dag_run_id':run,'logical_date':now.isoformat(),'conf':conf})
  if data.mode in ('raw','pipeline') and assignment:intake.bind(data.episode_id,run,dag)
  return {'run_id':run,'dag_id':dag,'state':result['state'],'episode':conf.get('episode_id'),'raw_episode':data.episode_id}


def airflow_runs(dag, **filters):
 offset=0
 while True:
  page=local_api('GET',f'/api/v2/dags/{dag}/dagRuns',params={'order_by':'-logical_date','limit':100,'offset':offset,**filters})
  rows=page['dag_runs']
  yield from rows
  offset+=len(rows)
  if not rows or offset>=page['total_entries']:return

def batch_active():
 return sum(r['state'] in ('running','queued') for r in airflow_runs(BATCH_DAG))

def check_batch_idle():
 if any(r['state'] in ('running','queued') for r in airflow_runs(BATCH_DAG)):
  raise HTTPException(409,'PC1 MANO 일괄 실행이 진행 중입니다.')

def batch_history():
 history={}
 for batch in airflow_runs(BATCH_DAG):
  for source in batch['conf'].get('sources',[]):
   history.setdefault(source['run_id'],batch['state'])
 return history

def mano_progress(run,history):
 run_id=run['dag_run_id']
 state=history.get(run_id)
 if not state:return 'MANO 대기',True
 if state=='success':return '전체 완료',False
 for stage,label in zip(reversed(MANO_STAGES),('최종 저장','결과 검사','MANO 피팅')):
  status=call('POST','/worker/jobs/status',json=request_for(stage,run_id,run['conf']))['state']
  if status in ('failed','interrupted'):return label+' · 실패',state=='failed'
  if status=='success':
   if stage=='publish':return '전체 완료',False
   break
  if status in ('queued','running'):return label+' · 진행 중',False
 return ('MANO 재개 대기',True) if state=='failed' else ('MANO 실행 대기',False)

class ManoSelection(BaseModel):
 runs:list[str]=Field(min_length=1,max_length=100)

@app.get('/api/pc1/mano')
def mano_candidates(req:Request):
 guard(req)
 history=batch_history()
 rows=[]
 for run in airflow_runs(DAY_DAG,state='success'):
  conf=run['conf']
  if not access.allowed(req,conf['episode_id']):continue
  label,ready=mano_progress(run,history)
  if ready:rows.append({'run':run['dag_run_id'],'episode':conf['episode_id'],'status':label})
 return list(reversed(rows))

@app.post('/api/pc1/mano')
def launch_mano(data:ManoSelection,req:Request):
 guard(req)
 if len(set(data.runs))!=len(data.runs):raise HTTPException(400,'중복된 실행입니다.')
 with lock:
  check_batch_idle()
  # PC1이 완전히 빌 때까지 기다리지 않고, 동시 실행 한도(MAX_PARALLEL_RUNS) 안에서 함께 실행합니다.
  busy=sum(r['state'] in ('running','queued') for wid,mode,dag in active_dags() if wid==DEFAULT_WORKER for r in airflow_runs(dag))
  if busy>=MAX_PARALLEL_RUNS:raise HTTPException(409,f'PC1에서 이미 {busy}개 작업이 실행 중입니다. 하나가 끝난 뒤 MANO를 실행하세요. (최대 {MAX_PARALLEL_RUNS}개 동시 실행)')
  sources=[]
  history=batch_history()
  for run_id in data.runs:
   run=local_api('GET',f'/api/v2/dags/{DAY_DAG}/dagRuns/'+quote(run_id,safe=''))
   conf=run['conf']
   access.require(req,conf['episode_id'])
   if run['state']!='success' or not mano_progress(run,history)[1]:
    raise HTTPException(409,'복원 완료 후 MANO 대기 중인 실행만 선택하세요.')
   sources.append({'run_id':run_id,'conf':conf})
  for source in sources:
   if history.get(source['run_id'])=='failed':
    for stage in MANO_STAGES:
     payload=request_for(stage,source['run_id'],source['conf'])
     if call('POST','/worker/jobs/status',json=payload)['state'] in ('failed','interrupted'):
      call('POST','/worker/jobs/retry',json=payload)
  ensure_worker_pool(worker(DEFAULT_WORKER))
  local_api('PATCH',f'/api/v2/dags/{BATCH_DAG}',json={'is_paused':False})
  now=datetime.now(timezone.utc)
  result=local_api('POST',f'/api/v2/dags/{BATCH_DAG}/dagRuns',json={'dag_run_id':'mano_'+secrets.token_hex(10),'logical_date':now.isoformat(),'conf':{'sources':sources}})
  return {'run_id':result['dag_run_id'],'count':len(sources)}

def needs_hc(state,predecessor_state,approval,worker_state=None):
 return bool(state not in ('success','failed','skipped') and predecessor_state=='success' and
             ((approval.get('ready') and approval.get('status')!='approved') or worker_state=='waiting'))

def run_tasks(dag,run_id):
 return local_api('GET',f'/api/v2/dags/{dag}/dagRuns/'+quote(run_id,safe='')+'/taskInstances')['task_instances']

@lru_cache(maxsize=256)
def finished_tasks(dag,run_id,state,end_date,refresh_window):
 # Clearing/retrying a run changes state; also bound the lifetime of terminal snapshots.
 return run_tasks(dag,run_id)

@app.get('/api/jobs')
def jobs(req:Request):
 guard(req);out=[]
 user=access.user(req)
 owners=access.owners()
 summary=req.query_params.get('summary')=='1'
 expanded=set(req.query_params.getlist('expanded'))
 reviews={}
 def approval_for(wid,episode,stage):
  if wid not in reviews:
   reviews[wid]={(x['episode'],x['stage']):x for x in all_review_items(wid) if not x.get('unavailable')}
  return reviews[wid].get((episode,stage),{})
 history=None
 entries=active_dags()+([(DEFAULT_WORKER,'test',DIRECT_DAGS['test'])] if 'test' in DIRECT_DAGS else [])
 for wid,mode,dag in entries:
  d=local_api('GET',f'/api/v2/dags/{dag}/dagRuns',params={'limit':12,'order_by':'-logical_date'})
  for r in d['dag_runs']:
   if user['role']!='admin' and owners.get(r.get('conf',{}).get('episode_id',''))!=user['name']:continue
   item={'dag_id':dag,'run_id':r['dag_run_id'],'state':r['state'],'start':r.get('start_date') or r.get('queued_at'),'mode':mode,'episode':r.get('conf',{}).get('episode_id','GPU 연결 테스트')}
   item.update(worker_id=wid,worker_label=worker(wid)['label'])
   if mode=='test':
    safe=''.join(c if c.isalnum() or c in '-_.' else '_' for c in r['dag_run_id'])
    p=ROOT/'results'/safe/'result.json'
    if p.exists():item['result']=json.loads(p.read_text())
   else:
    from direct_worker import request_for
    detailed=not summary or json.dumps([dag,r['dag_run_id']],separators=(',',':'),ensure_ascii=False) in expanded
    if not detailed and r['state']=='success':
     # Reopened approvals remain visible even for a finished run.
     tasks=[{'task_id':stage+'_approved','state':'success'} for stage in ('mask','tracking')
            if mode!='raw' and approval_for(wid,item['episode'],stage).get('status')=='needs_approval']
    elif r['state'] in ('success','failed'):
     tasks=finished_tasks(dag,r['dag_run_id'],r['state'],r.get('end_date'),int(time.monotonic()//30))
    else:
     tasks=run_tasks(dag,r['dag_run_id'])
    item['tasks']=[]
    for t in tasks:
     entry={'task':t['task_id'],'state':t['state']}
     if t['state'] in ('up_for_reschedule','running'):
      try:
       st=call('POST','/worker/jobs/status',json=request_for(t['task_id'],r['dag_run_id'],r.get('conf',{})),worker_id=wid,timeout=15)
       entry['worker_state']=st['state']
       if st.get('progress'):entry['progress']=st['progress']
       if st['state']=='waiting':entry['state']='review_waiting'
      except (httpx.HTTPError,OSError,ValueError,subprocess.SubprocessError):
       entry['worker_state']='unavailable';item['connection_error']=True
     if t['task_id'] in ('mask_approved','tracking_approved'):
      review_stage='mask' if t['task_id']=='mask_approved' else 'tracking'
      predecessor='sam3' if review_stage=='mask' else 'track'
      prior=next((x for x in tasks if x['task_id']==predecessor),{})
      approval={}
      if (t['state']=='success') or (prior.get('state')=='success' and t['state'] not in ('success','failed','skipped')):
       approval=approval_for(wid,item['episode'],review_stage)
      entry['review_stage']=review_stage
      entry['review_invalidated']=t['state']=='success' and approval.get('status')=='needs_approval'
      entry['review_required']=entry['review_invalidated'] or needs_hc(t['state'],prior.get('state'),approval,entry.get('worker_state'))
     item['tasks'].append(entry)
    if mode=='raw' and r['state']=='success':item['prepared_path']='03_prepared/'+item['episode']
   if mode=='day' and r['state']=='success':
     if history is None:history=batch_history()
     item['mano_progress']=mano_progress(r,history)[0]
   item['review_required']=next((t['review_stage'] for t in item.get('tasks',[]) if t.get('review_required')),None)
   item['can_retry']=bool(r['state']=='failed' and (r.get('conf',{}).get('operator_id') or owners.get(item['episode']))==user['name'] and any(t.get('state')=='failed' for t in item.get('tasks',[])))
   if mode!='test' and summary and not detailed:
    item['tasks']=[t for t in item['tasks'] if t['state'] not in ('success','skipped')]
    item['summary_only']=True
   item['operator']=r.get('conf',{}).get('operator_id') or '—';out.append(item)
 legacy=ROOT/'legacy_jobs.json'
 if legacy.exists():
  for item in json.loads(legacy.read_text()):
   if item['dag_id']!=DIRECT_DAGS.get('test') and access.allowed(req,item.get('episode','')):
    item['legacy']=True;out.append(item)
 return sorted(out,key=lambda x:x['start'] or '',reverse=True)

@app.api_route('/kaist-review/{path:path}',methods=['GET','POST'])
async def review_proxy(path:str,req:Request):
 guard(req,nonce=False)
 if path=='dags/kaist_revising_gpu_4090_a/queue':
  items=await asyncio.to_thread(all_review_items)
  return {'reviews':[x for x in items if not x.get('unavailable') and access.allowed(req,x['episode'])],'unavailable_workers':[x['worker_id'] for x in items if x.get('unavailable')]}
 match_worker=re.search(r'/episodes/(ep_[A-Za-z0-9_]+)(?:/|$)',path)
 session_episode=match_worker.group(1) if match_worker else req.query_params.get('episode_id','')
 if session_episode:access.require(req,session_episode)
 wid=access.execution_worker(session_episode) if session_episode else DEFAULT_WORKER
 pc=worker(wid);remote=pc['url']
 if access.user(req)['role']!='admin':
  match=re.search(r'/episodes/(ep_[A-Za-z0-9_]+)(?:/|$)',path)
  if match:access.require(req,match.group(1))
  elif path not in ('','dags/kaist_revising_gpu_4090_a/queue'):raise HTTPException(403,'허용되지 않은 검수 경로입니다.')
 # Keep original review CSRF check, operator identity and permission checks upstream.
 headers={'Authorization':'Bearer '+token(wid),'Origin':remote,'Host':urlsplit(remote).netloc}
 for k in ['cookie','content-type','x-csrf-token','range']:
  if req.headers.get(k):headers[k]=req.headers[k]
 client=httpx.AsyncClient(base_url=remote,trust_env=False,timeout=120)
 target='/kaist-review/'+path.replace('kaist_revising_gpu_4090_a',pc['review_dag'],1)
 upstream=client.build_request(req.method,target,params=req.query_params,headers=headers,content=await req.body())
 r=await client.send(upstream,stream=True)
 if req.method=='POST' and r.status_code<400:
  def record_change():
   with review_queue_lock(wid):
    review_queue_cache.pop(wid,None)
   access.audit(req,'review '+path)
  await asyncio.to_thread(record_change)
 if access.user(req)['role']!='admin' and (path.endswith('/queue') or path==''):
  body=await r.aread();headers_out={k:v for k,v in r.headers.items() if k.lower() in ('set-cookie',)}
  await r.aclose();await client.aclose()
  if r.status_code!=200:return Response(body,status_code=r.status_code,media_type=r.headers.get('content-type'))
  if path.endswith('/queue'):
   d=json.loads(body);d['reviews']=[x for x in d['reviews'] if access.allowed(req,x['episode'])];return Response(json.dumps(d),media_type='application/json',headers=headers_out)
  match=re.search(r"const token='([^']+)'",body.decode())
  return HTMLResponse("<script>const token='"+(match.group(1) if match else '')+"'</script>",headers=headers_out)
 response_headers={k:v for k,v in r.headers.items() if k.lower() in ('content-type','content-length','content-encoding','vary','content-range','accept-ranges','set-cookie','cache-control','content-security-policy')}
 async def close():await r.aclose();await client.aclose()
 return StreamingResponse(r.aiter_raw(),status_code=r.status_code,headers=response_headers,background=BackgroundTask(close))



def nas_preview(action, params, raw):
 episode=params.get("episode", "")
 if not re.fullmatch(r"ep_[A-Za-z0-9_]+", episode):
  raise HTTPException(400, "에피소드 이름을 확인하세요.")
 folder=Path(raw)/episode
 if folder.is_symlink() or not folder.is_dir():
  raise HTTPException(404, "NAS에 해당 RAW 에피소드가 없습니다.")
 if action=="preview-info":
  return dict(episode=episode,cameras=[1,2,3,4],source="raw",label="NAS RAW RGB",frames=None)
 try:
  cam=int(params["cam"])
  position=float(params.get("position","0.5"))
 except (KeyError,ValueError):
  raise HTTPException(400, "카메라와 프레임 위치를 확인하세요.")
 if cam not in (1,2,3,4) or not 0<=position<=1:
  raise HTTPException(400, "카메라와 프레임 위치를 확인하세요.")
 video=folder/f"cam_{cam}.mp4"
 if video.is_symlink() or not video.is_file():
  raise HTTPException(404, "NAS에서 해당 카메라 영상을 찾을 수 없습니다.")
 try:
  duration=float(subprocess.check_output(["ffprobe","-v","error","-show_entries","format=duration","-of","csv=p=0",str(video)],timeout=15))
  timestamp=max(0,min(duration-.1,duration*position))
  jpeg=subprocess.check_output(["ffmpeg","-nostdin","-v","error","-ss",str(timestamp),"-i",str(video),"-frames:v","1","-vf","scale=848:-2","-f","image2pipe","-vcodec","mjpeg","pipe:1"],timeout=25)
 except FileNotFoundError:
  raise HTTPException(503, "NAS UI에 영상 디코더가 설치되지 않았습니다.")
 except (subprocess.SubprocessError,ValueError):
  raise HTTPException(422, "NAS RAW 영상에서 사진을 추출할 수 없습니다.")
 if not jpeg:
  raise HTTPException(422, "해당 시점에 읽을 수 있는 프레임이 없습니다.")
 return Response(jpeg,media_type="image/jpeg",headers={"Cache-Control":"no-store"})

@app.get('/api/outputs/{action}')
async def output_proxy(action:str,req:Request):
 guard(req)
 raw=os.environ.get('KAIST_NAS_RAW_ROOT')
 if raw and action in ('preview','preview-info'):
  return await asyncio.to_thread(nas_preview,action,req.query_params,raw)
 wid=selected_worker(req);remote=worker(wid)['url']
 if access.user(req)['role']!='admin':
  return operator_output(action,req)
 if action not in ('catalog','list','text','file','preview-info','preview','roots','picker'):raise HTTPException(404)
 headers={'Authorization':'Bearer '+token(wid)}
 if req.headers.get('range'):headers['Range']=req.headers['range']
 client=httpx.AsyncClient(base_url=remote,trust_env=False,timeout=90)
 upstream=client.build_request('GET','/worker/outputs/'+action,params=req.query_params,headers=headers)
 r=await client.send(upstream,stream=True)
 forwarded={k:v for k,v in r.headers.items() if k.lower() in ('content-type','content-length','content-encoding','vary','content-range','accept-ranges','content-disposition','x-content-type-options','cache-control','x-frame-index')}
 async def close():await r.aclose();await client.aclose()
 return StreamingResponse(r.aiter_raw(),status_code=r.status_code,headers=forwarded,background=BackgroundTask(close))


def operator_output(action,req):
 wid=selected_worker(req);remote=worker(wid)['url']
 if action in ('preview','preview-info'):
  if req.query_params.get('episode','') not in call('GET','/worker/info',worker_id=wid)['episodes']:raise HTTPException(404,'RAW 목록에 없는 에피소드입니다.')
 else:
  if req.query_params.get('root','project')!='project':raise HTTPException(403,'배정된 파이프라인 결과만 조회할 수 있습니다.')
  if action=='roots':return {'selected':'project','roots':[{'id':'project','label':'내 작업 결과','path':'배정된 에피소드','available':True}]}
  if action not in ('catalog','list','text','file'):raise HTTPException(403,'관리자 전용 기능입니다.')
  catalog=call('GET','/worker/outputs/catalog',params={'root':'project'},worker_id=wid)
  catalog['groups']=[dict(g,episodes=[e for e in g['episodes'] if access.allowed(req,e['episode']) and access.execution_worker(e['episode'])==wid]) for g in catalog['groups']]
  catalog['groups']=[g for g in catalog['groups'] if g['episodes']];catalog.pop('roots',None)
  if action=='catalog':return catalog
  path=req.query_params.get('path','')
  prefixes=[f['path'] for g in catalog['groups'] for e in g['episodes'] for f in e['folders'] if f['root']=='project']
  if '..' in Path(path).parts or Path(path).is_absolute():raise HTTPException(403,'허용되지 않은 경로입니다.')
  if not any(path==p or path.startswith(p+'/') for p in prefixes):
   if action=='list' and (not path or any(p.startswith(path+'/') for p in prefixes)):return {'root':'project','path':path,'entries':[],'total':0,'truncated':False}
   raise HTTPException(403,'본인의 결과 폴더만 조회할 수 있습니다.')
 # Preserve range and binary responses, using the same trusted Worker endpoint.
 headers={'Authorization':'Bearer '+token(wid)}
 if req.headers.get('range'):headers['Range']=req.headers['range']
 r=httpx.get(remote+'/worker/outputs/'+action,params=req.query_params,headers=headers,timeout=90,trust_env=False)
 return Response(r.content,status_code=r.status_code,headers={k:v for k,v in r.headers.items() if k.lower() in ('content-type','content-range','accept-ranges','content-disposition','x-frame-index')})

@app.get('/assets/outputs.js')
def output_script(req:Request):
 guard(req)
 return FileResponse(Path(__file__).parent/'outputs.js',media_type='application/javascript',headers={'Cache-Control':'no-store'})


@app.get('/assets/reviews.js')
def review_script(req:Request):
 guard(req)
 return FileResponse(Path(__file__).parent/'reviews.js',media_type='application/javascript',headers={'Cache-Control':'no-store'})


@app.get('/assets/mask-editor.js')
def mask_editor_script(req:Request):
 guard(req)
 return FileResponse(Path(__file__).parent/'mask-editor.js',media_type='application/javascript',headers={'Cache-Control':'no-store'})


class OutputFolderRegistration(BaseModel):
 path:str=Field(min_length=1,max_length=4096)
 label:str=Field(default='',max_length=80)
class OutputFolderSelection(BaseModel):
 root:str

@app.post('/api/output-roots')
def output_folder_register(data:OutputFolderRegistration,req:Request):
 guard(req);access.admin(req)
 try:return call('POST','/worker/outputs/roots',json=data.model_dump(),worker_id=selected_worker(req))
 except httpx.HTTPStatusError as e:
  detail=e.response.json().get('detail','폴더를 등록하지 못했습니다.')
  raise HTTPException(e.response.status_code,detail)

@app.post('/api/output-roots/select')
def output_folder_select(data:OutputFolderSelection,req:Request):
 guard(req);access.admin(req)
 try:return call('POST','/worker/outputs/roots/select',json=data.model_dump(),worker_id=selected_worker(req))
 except httpx.HTTPStatusError as e:raise HTTPException(e.response.status_code,e.response.json().get('detail','폴더 선택 실패'))

@app.get('/api/intake')
def intake_list(req:Request):
 guard(req)
 records=[r for r in intake.rows() if access.allowed(req,r["episode"])]
 for r in records:
  r['execution_state']=None;r['stage']=None
  if r['run_id']:
   try:
    base=f"/api/v2/dags/{r['dag_id']}/dagRuns/"+quote(r['run_id'],safe='')
    run=local_api('GET',base);r['execution_state']=run['state']
    tasks=local_api('GET',base+'/taskInstances')['task_instances']
    active=next((t for t in tasks if t['state'] in ('running','up_for_reschedule','failed')),None)
    if active:r['stage']=active['task_id']
   except Exception:r['execution_state']='unavailable'
 return {'configured':bool(os.environ.get('KAIST_NAS_RAW_ROOT')),'episodes':records}

# NAS 검사는 여러 분 걸릴 수 있으므로 브라우저의 프록시 연결과 분리합니다.
_intake_job = {'running':False,'error':''}
_intake_job_lock = threading.Lock()

def _run_intake_scan():
 try:
  intake.scan()
 except Exception as error:
  with _intake_job_lock:_intake_job['error']=str(error)
 finally:
  with _intake_job_lock:_intake_job['running']=False

@app.get('/api/intake/scan-status')
def intake_scan_status(req:Request):
 guard(req);access.admin(req)
 with _intake_job_lock:return dict(_intake_job)

@app.post('/api/intake/scan')
def intake_scan(req:Request,include_manual:bool=True,background:bool=False):
 guard(req);access.admin(req)
 if background and include_manual:
  with _intake_job_lock:
   if not _intake_job['running']:
    _intake_job.update(running=True,error='')
    threading.Thread(target=_run_intake_scan,daemon=True).start()
   return dict(_intake_job)
 try:return intake.scan(include_manual)
 except ValueError as e:raise HTTPException(400,str(e))

class Assignment(BaseModel):
 episode:str=Field(pattern=r'^ep_[A-Za-z0-9_]+$')
 operator:str=Field(min_length=1,max_length=80)
 worker:str='gpu_4090_a'

@app.post('/api/intake/assign')
def intake_assign(data:Assignment,req:Request):
 guard(req);access.admin(req)
 try:intake.assign(data.episode,data.operator,data.worker)
 except ValueError as e:raise HTTPException(409,str(e))
 return {'ok':True}

@app.get('/assets/intake.js')
def intake_script(req:Request):
 guard(req)
 return FileResponse(Path(__file__).parent/'intake.js',media_type='application/javascript',headers={'Cache-Control':'no-store'})

@app.get('/assets/session.js')
def session_script(req:Request):
 guard(req)
 return FileResponse(Path(__file__).parent/'session.js',media_type='application/javascript',headers={'Cache-Control':'no-store'})

@app.get('/api/raw-episodes')
def raw_episodes(req:Request):
 guard(req)
 episodes=call('GET','/worker/info',worker_id=selected_worker(req))['episodes'];owners=access.owners();user=access.user(req);latest={}
 for wid,mode,dag in active_dags():
  runs=local_api('GET',f'/api/v2/dags/{dag}/dagRuns',params={'limit':100,'order_by':'-logical_date'})['dag_runs']
  for r in runs:
   ep=r.get('conf',{}).get('raw_episode_id') or r.get('conf',{}).get('episode_id');stamp=r.get('logical_date') or r.get('queued_at') or ''
   if ep in episodes and (ep not in latest or stamp>latest[ep][0]):latest[ep]=(stamp,dag,r,mode)
 batch_states=batch_history() if any(v[3]=='day' and v[2]['state']=='success' for v in latest.values()) else {}
 labels={'prepare_raw':'RAW 전처리','sam_settings_ready':'분할 설정 확인','sam3':'SAM3','mask_approved':'마스크 검수','preflight':'추적 사전 검사','track':'추적','tracking_approved':'추적 검수','prepare_reconstruction':'복원 준비','reconstruct':'3D 복원','hands':'MANO 피팅','validate':'결과 검사','publish':'최종 저장'}
 out=[]
 for ep in episodes:
  row=raw_row(ep,owners.get(ep),user)
  if not row['can_run'] and user['role']!='admin':
   out.append(dict(row,progress='다른 작업자 담당'))
   continue
  progress='실행 전' if not owners.get(ep) else '실행 기록 확인 필요'
  if ep in latest:
   _,dag,r,mode=latest[ep]
   if mode=='day' and r['state']=='success':progress=mano_progress(r,batch_states)[0]
   elif r['state']=='success':progress='RAW 전처리 완료' if mode=='raw' else '전체 완료'
   else:
    tasks=local_api('GET',f"/api/v2/dags/{dag}/dagRuns/"+quote(r['dag_run_id'],safe='')+'/taskInstances')['task_instances']
    active=next((t for t in tasks if t['state']=='failed'),None) or next((t for t in tasks if t['state'] in ('running','up_for_reschedule','up_for_retry')),None)
    if active:
     label=labels.get(active['task_id'],active['task_id']);state=active['state']
     progress=label+(' · 실패' if state=='failed' else ' · 재시도 대기' if state=='up_for_retry' else ' · 승인 대기' if active['task_id'] in ('mask_approved','tracking_approved') else ' · 진행 중')
    else:progress='실패' if r['state']=='failed' else '실행 대기'
  out.append(dict(row,progress=progress))
 return {'episodes':out}

class RetryJob(BaseModel):
 dag_id:str
 run_id:str=Field(min_length=1,max_length=250)
 stage:str

@app.post('/api/jobs/retry')
def retry_job(data:RetryJob,req:Request):
 guard(req)
 order=['prepare_raw','sam_settings_ready','sam3','mask_approved','preflight','track','tracking_approved','prepare_reconstruction','reconstruct','hands','validate','publish']
 wid=dag_worker(data.dag_id)
 if data.dag_id not in [d for w,m,d in active_dags()] or data.stage not in order:raise HTTPException(400,'재시도할 단계를 확인하세요.')
 with lock:
  base=f'/api/v2/dags/{data.dag_id}/dagRuns/'+quote(data.run_id,safe='')
  run=local_api('GET',base);conf=run.get('conf',{});ep=conf.get('episode_id','')
  owner=conf.get('operator_id') or access.owners().get(ep)
  if not owner or owner!=access.user(req)['name']:raise HTTPException(403,'이 에피소드를 실행한 작업자만 재시도할 수 있습니다.')
  if run['state']!='failed':raise HTTPException(409,'실패한 실행만 재시도할 수 있습니다.')
  running_here=batch_active() if wid==DEFAULT_WORKER else 0
  if running_here>=MAX_PARALLEL_RUNS:raise HTTPException(409,f'이 PC에서 이미 {MAX_PARALLEL_RUNS}개 작업이 실행 중입니다. 하나가 끝난 뒤 재시도하세요.')
  for other_wid,_,dag in active_dags():
   recent=local_api('GET',f'/api/v2/dags/{dag}/dagRuns',params={'limit':100,'order_by':'-logical_date'})['dag_runs']
   for other in recent:
    if other['dag_run_id']==data.run_id and dag==data.dag_id:continue
    if other_wid==wid and other['state'] in ('running','queued'):
     running_here+=1
     if other.get('conf',{}).get('episode_id')==ep:raise HTTPException(409,'같은 에피소드가 실행 중입니다. 완료 후 재시도하세요.')
     if running_here>=MAX_PARALLEL_RUNS:raise HTTPException(409,f'이 PC에서 이미 {MAX_PARALLEL_RUNS}개 작업이 실행 중입니다. 하나가 끝난 뒤 재시도하세요.')
    if other.get('conf',{}).get('episode_id')==ep and (other.get('logical_date') or '')>(run.get('logical_date') or ''):raise HTTPException(409,'더 최근에 실행한 에피소드입니다. 최신 실행을 확인하세요.')
  tasks=local_api('GET',base+'/taskInstances')['task_instances']
  failed=sorted((t for t in tasks if t['state']=='failed'),key=lambda t:order.index(t['task_id']))
  if not failed or failed[0]['task_id']!=data.stage:raise HTTPException(409,'현재 실패 단계가 변경됐습니다. 새로고침하세요.')
  targets=[t for t in tasks if order.index(t['task_id'])>=order.index(data.stage)]
  if any(t['state'] in ('success','running','queued','up_for_reschedule') for t in targets):raise HTTPException(409,'후속 단계에 완료 또는 실행 중인 작업이 있어 재시도를 중단했습니다.')
  payload={'dag_run_id':data.run_id,'task_ids':[t['task_id'] for t in targets],'only_failed':False,'reset_dag_runs':True,'include_upstream':False,'include_downstream':False,'prevent_running_task':True,'note':'UI retry by '+access.user(req)['name']}
  local_api('POST',f'/api/v2/dags/{data.dag_id}/clearTaskInstances',json={**payload,'dry_run':True})
  from direct_worker import request_for
  for task in targets:
   call('POST','/worker/jobs/retry',json=request_for(task['task_id'],data.run_id,conf),worker_id=wid)
  local_api('POST',f'/api/v2/dags/{data.dag_id}/clearTaskInstances',json={**payload,'dry_run':False})
  access.audit(req,'retry '+data.run_id+' '+data.stage)
  return {'ok':True,'stage':data.stage}
