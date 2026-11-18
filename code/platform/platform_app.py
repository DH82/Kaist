import base64,hashlib,json,os,secrets,subprocess,sys,threading,time
from contextlib import asynccontextmanager
import asyncio
from functools import lru_cache
from pathlib import Path
from datetime import datetime,timezone
from urllib.parse import urlsplit,quote
import httpx
from fastapi import FastAPI,Request,HTTPException
from fastapi.responses import HTMLResponse,Response,StreamingResponse,FileResponse,RedirectResponse
from starlette.background import BackgroundTask
from pydantic import BaseModel,Field
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(Path(__file__).parent))
import intake,access,re,fleet
sys.path.insert(0,str(ROOT/'dags'))
from direct_worker import call,token,request_for,MANO_STAGES,mano_cutoff,MANO_CUTOFF_HOUR,REDO_STAGES,REDO_LABELS,REDO_START
from workers import workers,worker,dag_entries,dag_worker,DEFAULT_WORKER,DAY_DAG,BATCH_DAG,MAX_PARALLEL_RUNS,POOL_SLOTS
DIRECT_DAGS={"test":"kaist_mac_to_4090_test","pipeline":"kaist_direct_pipeline_4090","raw":"kaist_direct_raw_4090"}
if os.environ.get('KAIST_GPU_TEST_ENABLED','true').lower()=='false':DIRECT_DAGS.pop('test')
@asynccontextmanager
async def lifespan(app):
 task=None
 if os.environ.get('KAIST_AIRFLOW_API_URL'):
  task=asyncio.create_task(prepare_worker_pools())
  threading.Thread(target=cont_loop,name='kaist-continuous',daemon=True).start()
 try:
  yield
 finally:
  _cont_stop.set()
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

def nudge_gate(worker_id,episode,stage):
 """검수 승인 직후 그 에피소드 실행의 승인 대기 센서(mask_approved/tracking_approved)를 바로 다시 확인하게 합니다.
 센서는 5초 간격으로도 확인하므로, 실패해도 조금 늦게 진행될 뿐입니다."""
 nudged=[]
 for wid,_,dag in active_dags():
  if wid!=worker_id:continue
  runs=local_api('GET',f'/api/v2/dags/{dag}/dagRuns',params={'limit':50,'order_by':'-logical_date','state':['running']})['dag_runs']
  for run in runs:
   if run.get('conf',{}).get('episode_id')!=episode:continue
   base=f'/api/v2/dags/{dag}/dagRuns/'+quote(run['dag_run_id'],safe='')
   try:ti=local_api('GET',base+'/taskInstances/'+stage)
   except Exception:continue
   if ti.get('state')!='up_for_reschedule':continue
   local_api('POST',f'/api/v2/dags/{dag}/clearTaskInstances',json={'dag_run_id':run['dag_run_id'],'task_ids':[stage],'only_failed':False,
    'reset_dag_runs':False,'include_upstream':False,'include_downstream':False,'dry_run':False,'note':'review approved: check gate now'})
   nudged.append(run['dag_run_id'])
 return nudged

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
  if current.get('slots',0)<POOL_SLOTS:
   # 실패해도 실행은 막지 않습니다(슬롯 1개로 순서 대기). Airflow 화면 Admin→Pools에서 직접 늘릴 수 있습니다.
   try:local_api('PATCH',path,params={'update_mask':'slots'},json={'name':pc['pool'],'slots':POOL_SLOTS,'include_deferred':current.get('include_deferred',False)})
   except httpx.HTTPError as exc:print('GPU 풀 슬롯 변경 실패:',pc['pool'],exc,flush=True)
  return
 except httpx.HTTPStatusError as exc:
  if exc.response.status_code!=404:raise
 try:
  local_api('POST','/api/v2/pools',json={'name':pc['pool'],'slots':POOL_SLOTS,'description':pc['label']})
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
  # PC 연동: 다른 PC에서 로그인해 넘어온 1회용 토큰(/sso)과, 그렇게 만든 유효한 로그인 세션은 이 PC의
  # 사이트 비밀번호를 다시 묻지 않습니다(세션은 비밀번호를 통과한 PC의 로그인으로만 생깁니다).
  if not valid and req.url.path=='/sso':valid=True
  if not valid and req.cookies.get('kaist_session'):
   try:access.user(req);valid=True
   except HTTPException:pass
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
 name:str=Field(default='',max_length=80)
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
 if data.role not in ('operator','admin'):raise HTTPException(400,'이름과 역할을 확인하세요.')
 if data.role=='admin':
  # 관리자: PC 선택 없이 비밀번호만 확인하고 이 화면에서 바로 관리합니다(다른 PC로 넘기지 않음).
  users=json.loads(AIRFLOW_USERS.read_text())
  expected=users.get(os.environ.get('KAIST_AIRFLOW_USER','admin'),'')
  if not expected or not secrets.compare_digest(data.password.encode(),expected.encode()):raise HTTPException(403,'관리자 비밀번호가 일치하지 않습니다.')
  token=access.login(access.ADMIN_NAME,'admin')
  with access.db() as c:c.execute('INSERT INTO ui_audit VALUES(?,?,?)',(time.time(),access.ADMIN_NAME,'login-admin on '+access.here_pc()))
  response=Response(json.dumps({'name':access.here_pc(),'role':'admin','label':access.ADMIN_NAME,'admin_login':True},ensure_ascii=False),media_type='application/json')
  response.set_cookie('kaist_session',token,httponly=True,samesite='strict',secure=req.url.scheme=='https',max_age=43200)
  return response
 name=access.pc_name(data.name)
 if not name:raise HTTPException(403,'PC1 ~ PC6 중 하나의 이름으로만 로그인할 수 있습니다.')
 # 로그인 이름 = 조작할 PC. 다른 PC 이름이면 그 PC의 UI로 넘겨줍니다(NAS 1회용 토큰, 다시 로그인 불필요).
 if fleet.MACHINE_PC and name.lower()!=fleet.MACHINE_PC:
  target=name.lower();st=fleet.read_pc(target) if fleet.available() else None
  url=((st or {}).get('meta') or {}).get('ui_url')
  if not st or not st.get('online') or not url:
   raise HTTPException(409,f'{name} 화면에 연결할 수 없습니다. {name}의 UI가 켜져 있고 NAS에 연결되어 있는지 확인하세요. (이 화면은 {fleet.MACHINE_PC.upper()}입니다)')
  token_value=fleet.handoff_create(target,name,data.role)
  with access.db() as c:c.execute('INSERT INTO ui_audit VALUES(?,?,?)',(time.time(),name,'login-handoff '+fleet.MACHINE_PC+'->'+target))
  return {'name':name,'role':data.role,'redirect':url.rstrip('/')+'/sso?t='+quote(token_value)}
 token=access.login(name,data.role)
 response=Response(json.dumps({'name':name,'role':data.role}),media_type='application/json')
 response.set_cookie('kaist_session',token,httponly=True,samesite='strict',secure=req.url.scheme=='https',max_age=43200)
 return response

@app.get('/sso')
def sso(req:Request,t:str=''):
 """다른 PC 화면에서 이 PC 이름으로 로그인했을 때 넘어오는 곳. 토큰은 한 번만, 60초 안에만 쓸 수 있습니다."""
 guard(req)
 h=fleet.handoff_take(t)
 if not h:return HTMLResponse('<meta charset="utf-8"><p>로그인 연결이 만료되었거나 이미 사용되었습니다. <a href="/">다시 로그인</a>하세요.</p>',status_code=403)
 token_value=access.login(h['name'],h['role'])
 with access.db() as c:c.execute('INSERT INTO ui_audit VALUES(?,?,?)',(time.time(),h['name'],'login-sso from '+str(h.get('from'))))
 response=RedirectResponse('/',status_code=303)
 response.set_cookie('kaist_session',token_value,httponly=True,samesite='lax',secure=req.url.scheme=='https',max_age=43200)
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

# RAW 대기열 시작일: 이 날짜(촬영일) 이전 RAW는 새로 배정·표시하지 않습니다.
# 이미 담당자나 실행 기록이 있는 RAW는 진행 중 작업이므로 그대로 보여 줍니다.
RAW_QUEUE_START='20261116'

def raw_in_queue(ep,owners,started=()):
 m=re.match(r'ep_(\d{8})_',ep)
 return bool(m and m.group(1)>=RAW_QUEUE_START) or bool(owners.get(ep)) or ep in started

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
 try:elsewhere=fleet.raw_taken_elsewhere() if fleet.available() else {}
 except Exception:elsewhere={}
 d['episodes']=[ep for ep in d['episodes'] if raw_in_queue(ep,owners) and (ep not in elsewhere or owners.get(ep))]
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
 sam_object_points:dict|None=None
 prep:bool=False  # True: RAW 전처리만 하고 'SAM3 설정 대기'에서 멈춤(분할 대상은 나중에 입력)

gpu_cache={}
def local_gpu_status():
 """Worker에 /worker/gpu가 아직 배포되지 않았을 때(재시작 전) UI와 같은 PC의 nvidia-smi로 대신 조회합니다."""
 out=subprocess.check_output(['nvidia-smi','--query-gpu=index,name,uuid,memory.used,memory.total,utilization.gpu','--format=csv,noheader,nounits'],text=True,timeout=10)
 num=lambda v:int(float(v)) if v.replace('.','',1).isdigit() else None
 rows=[[v.strip() for v in line.split(',')] for line in out.strip().splitlines() if ',' in line]
 return {'gpus':[{'index':num(i),'name':n,'uuid':u,'used_mb':num(used),'total_mb':num(total),'util':num(util),'processes':[]} for i,n,u,used,total,util in rows],'worker_gpu_uuid':None}

@app.get('/api/gpu')
def gpu_usage(req:Request):
 guard(req)
 wid=selected_worker(req)
 hit=gpu_cache.get(wid)
 if hit and time.monotonic()-hit[0]<2:return hit[1]
 try:d=call('GET','/worker/gpu',worker_id=wid)
 except (httpx.HTTPError,OSError,ValueError) as exc:
  if wid!=DEFAULT_WORKER:raise HTTPException(503,'GPU 상태를 불러오지 못했습니다.') from exc
  try:d=local_gpu_status()
  except (OSError,subprocess.SubprocessError) as err:raise HTTPException(503,'GPU 상태를 불러오지 못했습니다.') from err
 gpu_cache[wid]=(time.monotonic(),d)
 return d

# ---- 사이드바 '지금 진행 중': 이 PC Worker의 실행·대기 단계 + 단계별 예상 소요 시간 ----
# 예상 시간 = 단계별 '프레임당 평균 시간'(ESTIMATE_SINCE 이후 성공 기록만, 오류·실패·중단 제외 · 재시도 전 시도 포함) × 이 에피소드 프레임 수.
# 프레임 수(전처리 후 프레임, 이후 단계에서 변하지 않음)를 아직 모르면 같은 기간 평균 소요 시간을 씁니다.
ESTIMATE_SINCE=datetime.fromisoformat(os.environ.get('KAIST_ESTIMATE_SINCE','2026-11-17T15:00:00+09:00')).timestamp()
ESTIMATE_SKIP={'sam_settings_ready','mask_approved','tracking_approved'}   # 검수·설정 대기 관문: 시간은 작업자에게 달림
ACTIVITY_STALE_SECONDS=12*3600
_estimate_cache=[0.0,{}]

def _job_dirs():
 root=worker_jobs_dir()
 try:return [d for d in root.iterdir() if d.is_dir() and not d.name.startswith('_')] if root and root.is_dir() else []
 except OSError:return []

def stage_estimates():
 """{stage:{per_frame,frames,runs,mean,timed}} 60초 캐시. per_frame=총 소요 시간/총 프레임(프레임 수를 아는 기록만)."""
 if time.monotonic()-_estimate_cache[0]<60:return _estimate_cache[1]
 try:frames={r['episode']:r['prepared'] for r in episode_frame_rows() if r.get('prepared')}
 except Exception:frames={}
 acc={}
 for d in _job_dirs():
  try:req=json.loads((d/'request.json').read_text())
  except (OSError,ValueError):continue
  files=[d/'status.json']+(sorted((d/'attempts').glob('*/status.json')) if (d/'attempts').is_dir() else [])
  for f in files:
   try:st=json.loads(f.read_text())
   except (OSError,ValueError):continue
   s,e=st.get('started'),st.get('finished')
   if st.get('state')!='success' or st.get('error') or not s or not e or s<ESTIMATE_SINCE or e<s:continue
   a=acc.setdefault(req.get('stage'),{'secs':0.0,'frames':0,'fsecs':0.0,'runs':0,'timed':0})
   a['secs']+=e-s;a['timed']+=1
   n=frames.get(req.get('episode'))
   if n:a['fsecs']+=e-s;a['frames']+=n;a['runs']+=1
 out={k:{'per_frame':a['fsecs']/a['frames'] if a['frames'] else None,'frames':a['frames'],'runs':a['runs'],
         'mean':a['secs']/a['timed'],'timed':a['timed']} for k,a in acc.items() if k}
 _estimate_cache[:]=[time.monotonic(),out]
 return out

def episode_prepared_frames(ep,cache):
 if ep not in cache:
  n=stage_frames(Path(WORK_DIR),ep).get('prepared') if WORK_DIR and re.fullmatch(r'ep_[A-Za-z0-9_]+',ep or '') else None
  if not n:
   try:
    with frames_db() as c:row=c.execute('SELECT prepared FROM episode_frames WHERE episode=?',(ep,)).fetchone()
    n=row[0] if row else None
   except Exception:n=None
  cache[ep]=n
 return cache[ep]

@app.get('/api/activity')
def activity(req:Request):
 guard(req)
 wid=selected_worker(req)
 if wid!=DEFAULT_WORKER:return {'available':False,'items':[]}   # 작업 기록은 이 PC Worker 폴더에서만 읽습니다
 now=time.time();items=[];frame_cache={};ready_today=set()
 t=time.localtime(now);midnight=time.mktime((t.tm_year,t.tm_mon,t.tm_mday,0,0,0,0,0,-1))
 for d in _job_dirs():
  try:st=json.loads((d/'status.json').read_text())
  except (OSError,ValueError):continue
  # 오늘(이 PC 시간대 0시 이후) 3D 복원까지 성공 = MANO 대기 직전 단계까지 끝낸 에피소드(같은 에피소드 재실행은 1개로 셈)
  if st.get('stage')=='reconstruct' and st.get('state')=='success' and (st.get('finished') or 0)>=midnight:
   try:ready_today.add(json.loads((d/'request.json').read_text()).get('episode'))
   except (OSError,ValueError):pass
   continue
  if st.get('state') not in ('running','queued') or st.get('stage') in ESTIMATE_SKIP:continue
  if now-(st.get('started') or st.get('created') or 0)>ACTIVITY_STALE_SECONDS:continue
  try:rq=json.loads((d/'request.json').read_text())
  except (OSError,ValueError):continue
  items.append({'episode':rq.get('episode'),'stage':rq.get('stage'),'run_id':rq.get('run_id'),'state':st['state'],
                'created':st.get('created'),'started':st.get('started') if st['state']=='running' else None,
                'gpu_slot':st.get('gpu_slot'),'progress':st.get('progress')})
 est=stage_estimates()
 for it in items:
  e=est.get(it['stage']);n=episode_prepared_frames(it['episode'],frame_cache)
  it['frames']=n
  if e and e['per_frame'] and n:it['estimate']={'seconds':e['per_frame']*n,'per_frame':e['per_frame'],'basis':'frames','runs':e['runs']}
  elif e:it['estimate']={'seconds':e['mean'],'basis':'mean','runs':e['timed']}
  else:it['estimate']=None
  ref=it['started'] if it['state']=='running' else it['created']
  it['elapsed']=max(0,now-ref) if ref else None
 items.sort(key=lambda x:(x['state']!='running',x['started'] or x['created'] or 0))
 return {'available':True,'items':items,'since':ESTIMATE_SINCE,'ready_today':len(ready_today-{None})}

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

def object_points(value):
 # 선택 옵션: 첫 프레임 포인트(카메라 1~4 → 0~3, [x,y,label] 정규화, label 1=포함 0=제외). Worker에서도 다시 검사합니다.
 if not isinstance(value,dict) or sorted(value)!=['0','1','2','3']:raise HTTPException(400,'포인트 물체 추적은 카메라 4대 모두 첫 프레임에서 물체를 클릭해야 합니다.')
 result={}
 for cam,pts in value.items():
  if not isinstance(pts,list) or not 1<=len(pts)<=20:raise HTTPException(400,f'카메라 {int(cam)+1} 포인트는 1~20개여야 합니다.')
  clean=[]
  for pt in pts:
   if not isinstance(pt,list) or len(pt)!=3 or any(isinstance(v,bool) or not isinstance(v,(int,float)) for v in pt):raise HTTPException(400,'포인트 형식이 잘못되었습니다.')
   x,y,label=round(float(pt[0]),4),round(float(pt[1]),4),int(pt[2])
   if not (0<=x<=1 and 0<=y<=1) or label not in (0,1):raise HTTPException(400,f'카메라 {int(cam)+1} 포인트가 화면 밖입니다.')
   clean.append([x,y,label])
  if not any(l==1 for _,_,l in clean):raise HTTPException(400,f'카메라 {int(cam)+1}에 포함(왼쪽 클릭) 포인트가 하나 이상 필요합니다.')
  result[cam]=clean
 return result

# 사람의 입력을 기다리는 단계. 실행이 이 단계에 있으면 GPU를 쓰지 않으므로 PC당 동시 실행 한도에서 뺍니다.
WAIT_STAGES=('sam_settings_ready','mask_approved','tracking_approved')
PIPELINE_ORDER=['prepare_raw','sam_settings_ready','sam3','mask_approved','preflight','track','tracking_approved','prepare_reconstruction','reconstruct','hands','validate','publish']

def current_stage(dag,run_id):
 """실행의 현재 단계(순서상 첫 미완료 단계)와 그 태스크 상태"""
 tasks={t['task_id']:t for t in run_tasks(dag,run_id)}
 for stage in PIPELINE_ORDER:
  t=tasks.get(stage)
  if t and t['state'] not in ('success','skipped'):return stage,t['state']
 return None,None

def run_waiting(dag,run):
 """설정·검수 승인을 기다리는 실행인지(실패한 단계가 아니라 대기 중일 때만)"""
 try:stage,state=current_stage(dag,run['dag_run_id'])
 except Exception:return False
 return stage in WAIT_STAGES and state in ('up_for_reschedule','scheduled','queued','running',None)

def update_block():
 """관리자가 이 PC의 GitHub 업데이트를 요청해 두었으면 새 실행 시작을 막습니다(진행 중인 작업은 계속)."""
 try:pending=fleet.update_pending()
 except Exception:pending=None
 if pending:raise HTTPException(503,'이 PC는 프로그램 업데이트 대기 중입니다. 진행 중인 작업이 끝나 업데이트가 반영되면 다시 시작할 수 있습니다. (요청: '+str(pending.get('requested_by') or '관리자')+')')

@app.post('/api/run')
def launch(data:Launch,req:Request):
 return _launch(data,req)

def _launch(data,req,bypass_limit=False):
 """bypass_limit: SAM3 연속 모드 대기열(cont_tick)에서만 True. PC당 동시 실행 한도를 적용하지 않습니다."""
 guard(req)
 if data.mode!='test':update_block()
 if data.mode=='test':access.admin(req)
 elif access.owners().get(data.episode_id) not in (None,access.user(req)['name']):raise HTTPException(403,'다른 작업자가 선택한 에피소드입니다.')
 elif not raw_startable(req,data.episode_id,data.worker_id):raise HTTPException(403,'본인에게 배정된 RAW만 시작할 수 있습니다. RAW 목록의 내 배정을 확인하세요.')
 if data.mode not in DIRECT_DAGS:raise HTTPException(400,'실행 모드를 선택하세요.')
 try:pc=worker(data.worker_id)
 except ValueError as e:raise HTTPException(400,str(e))
 if data.mode=='test' and data.worker_id!=DEFAULT_WORKER:raise HTTPException(400,'GPU 연결 테스트는 '+local_pc_label()+'에서만 실행할 수 있습니다.')
 dag=pc['dag_id'] if data.mode=='pipeline' else DIRECT_DAGS[data.mode]
 if data.mode=='raw' and data.worker_id!=DEFAULT_WORKER:raise HTTPException(400,'RAW 전용 호환 실행은 '+local_pc_label()+'에서만 할 수 있습니다.')
 if data.defer_mano:
  if data.worker_id!=DEFAULT_WORKER or data.mode!='pipeline':raise HTTPException(400,local_pc_label()+' 전체 파이프라인에서만 선택할 수 있습니다.')
  dag=DAY_DAG
 with lock:
  if data.mode in ('raw','pipeline') and not raw_startable(req,data.episode_id,data.worker_id):
   raise HTTPException(403,'배정이 변경되었거나 내 작업에서 제외한 RAW입니다. 목록을 새로고침하세요.')
  if data.mode in ('raw','pipeline') and not bypass_limit and data.episode_id in cont_waiting_eps():
   raise HTTPException(409,'SAM3 연속 모드 대기열에 있는 RAW입니다. 순서가 되면 자동으로 시작합니다(직접 실행하려면 대기열에서 빼세요).')
  # PC 간 중복 실행 방지: 다른 PC가 선점한 RAW는 시작하지 않습니다(이 PC에 이미 담당·실행 기록이 있으면 예외).
  if data.mode in ('raw','pipeline') and not access.owners().get(data.episode_id) and not fleet.raw_claim(data.episode_id):
   raise HTTPException(409,f'이 RAW는 {(fleet.raw_owner(data.episode_id) or "다른 PC").upper()}에서 작업 중입니다.')
  active=local_api('GET',f'/api/v2/dags/{dag}/dagRuns',params={'limit':100,'order_by':'-logical_date'})
  # MANO 대기(3D 복원 완료)와 MANO 일괄 실행은 동시 실행 수에 넣지 않습니다(퇴근 후 일괄 실행용).
  # 실제 GPU 동시 연산은 Worker의 GPU 슬롯(gpu_slots.py)이 제한하므로, 겹치면 GPU 작업이 잠시 queued로 기다립니다.
  used_names=set();running_here=0
  for wid,other_mode,other_dag in active_dags():
   if wid!=data.worker_id:continue
   rows=active if other_dag==dag else local_api('GET',f'/api/v2/dags/{other_dag}/dagRuns',params={'limit':100,'order_by':'-logical_date'})
   for row in rows['dag_runs']:
    conf=row.get('conf') or {}
    used_names.add(conf.get('episode_id'))
    if row['state'] not in ('queued','running'):continue
    # 같은 RAW를 같은 PC에서 동시에 두 번 돌리지 않습니다.
    if data.mode in ('pipeline','raw') and (conf.get('raw_episode_id') or conf.get('episode_id'))==data.episode_id:raise HTTPException(409,'이 RAW는 선택한 PC에서 이미 실행 중입니다.')
    # 설정·검수 승인을 기다리는 실행은 GPU를 쓰지 않으므로 세지 않습니다.
    if not run_waiting(other_dag,row):running_here+=1
  # PC 한 대에서 최대 MAX_PARALLEL_RUNS개까지 동시에 진행합니다.
  # RAW 전처리 일괄 실행(prep)은 한도와 무관하게 시작합니다. 실제 GPU 동시 계산은 GPU PC의 GPU 슬롯이 2개로 제한합니다.
  if running_here>=MAX_PARALLEL_RUNS and not data.prep and not bypass_limit:raise HTTPException(409,f'선택한 PC에서 이미 {running_here}개 작업이 실행 중입니다. (최대 {MAX_PARALLEL_RUNS}개 동시 실행)')
  if data.mode=='pipeline':
   for other_wid,_,other_dag in active_dags():
    if other_wid==data.worker_id:continue
    rows=local_api('GET',f'/api/v2/dags/{other_dag}/dagRuns',params={'limit':100,'order_by':'-logical_date'})['dag_runs']
    if any(r['state'] in ('queued','running') and (r.get('conf',{}).get('raw_episode_id') or r.get('conf',{}).get('episode_id'))==data.episode_id for r in rows):raise HTTPException(409,'이 RAW가 다른 PC에서 실행 중입니다.')
  conf={}
  if data.mode in ('raw','pipeline'):
   if data.prep and (data.mode!='pipeline' or data.sam_object_mode!='text'):raise HTTPException(400,'RAW 전처리 일괄 실행은 기본 분할 방식만 지원합니다.')
   if data.mode=='pipeline' and not data.prep and data.preview_episode!=data.episode_id:raise HTTPException(400,'선택한 에피소드 이미지를 먼저 확인하세요.')
   if data.mode=='pipeline' and not data.prep and (not data.episode_id or not data.sam_prompt.strip() or not data.confirmed):raise HTTPException(400,'에피소드와 SAM3 대상을 입력하고 설정을 확인하세요.')
   if data.sam_object_mode not in ('text','bbox_track','point_track'):raise HTTPException(400,'물체 분할 방식을 확인하세요.')
   bboxes=object_bboxes(data.sam_object_bboxes) if data.mode=='pipeline' and data.sam_object_mode=='bbox_track' else None
   points=object_points(data.sam_object_points) if data.mode=='pipeline' and data.sam_object_mode=='point_track' else None
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
   if data.mode=='pipeline' and data.prep:conf.update(sam_prompt='',sam_scene='2d',sam_settings_confirmed=False)
   elif data.mode=='pipeline':conf.update(sam_prompt=data.sam_prompt.strip(),sam_scene='2d',sam_settings_confirmed=True)
   if bboxes:conf.update(sam_object_mode='bbox_track',sam_object_bboxes=bboxes)
   if points:conf.update(sam_object_mode='point_track',sam_object_points=points)
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

def local_pc_label():
 """화면에 보일 이 PC 이름(예: 'PC 6 · RTX 4090'). 내부 식별자(gpu_4090_a, kaist_pc1_*, /api/pc1/…)는 그대로 둡니다."""
 try:return worker(DEFAULT_WORKER)['label']
 except (ValueError,KeyError,OSError):return (fleet.MACHINE_PC or 'pc1').upper()

def check_batch_idle():
 if any(r['state'] in ('running','queued') for r in airflow_runs(BATCH_DAG)):
  raise HTTPException(409,local_pc_label()+' MANO 일괄 실행이 진행 중입니다.')

BATCH_OF={}  # run_id → 그 실행이 들어 있는 가장 최근 MANO 일괄 실행 ID (batch_history가 갱신)

def batch_history():
 history={};batch_of={}
 for batch in airflow_runs(BATCH_DAG):
  for source in batch['conf'].get('sources',[]):
   history.setdefault(source['run_id'],batch['state']);batch_of.setdefault(source['run_id'],batch['dag_run_id'])
 BATCH_OF.clear();BATCH_OF.update(batch_of)
 return history

# ---- 단계 초기화로 '3D 복원까지만' 다시 실행한 전체 파이프라인 실행 ----
# Airflow의 MANO 단계(hands·validate·publish)는 skipped로 두고, 낮 실행(DAY_DAG)처럼 'MANO 대기' → MANO 일괄 실행으로 이어 갑니다.
# MANO 일괄 실행은 이 PC Worker(DEFAULT_WORKER)에서만 하므로 그 PC의 전체 파이프라인 실행만 해당합니다.
def deferred_db():
 c=access.db()
 c.execute('CREATE TABLE IF NOT EXISTS mano_deferred (run_id TEXT PRIMARY KEY,dag_id TEXT NOT NULL,at REAL)')
 return c

def deferred_runs():
 with deferred_db() as c:return {r[0]:r[1] for r in c.execute('SELECT run_id,dag_id FROM mano_deferred')}

def set_deferred(dag,run_id,on):
 with deferred_db() as c:
  if on:c.execute('INSERT OR REPLACE INTO mano_deferred VALUES(?,?,?)',(run_id,dag,time.time()))
  else:c.execute('DELETE FROM mano_deferred WHERE run_id=?',(run_id,))

def mano_dag(run_id):
 """MANO 대기 실행이 들어 있는 DAG: 낮 실행 또는 '3D까지만' 초기화한 전체 파이프라인"""
 return deferred_runs().get(run_id,DAY_DAG)

def mano_ready_runs():
 """3D 복원까지 끝난(성공) 낮 실행 + '3D까지만' 초기화한 뒤 성공한 전체 파이프라인 실행"""
 yield from airflow_runs(DAY_DAG,state='success')
 for run_id,dag in deferred_runs().items():
  try:run=local_api('GET',f'/api/v2/dags/{dag}/dagRuns/'+quote(run_id,safe=''))
  except Exception:continue
  if run.get('state')=='success':yield run

# ---- MANO 일괄 실행 대기열에서 에피소드 하나 제거 ----
def mano_removed_db():
 c=access.db()
 c.execute('CREATE TABLE IF NOT EXISTS mano_removed (batch_run_id TEXT NOT NULL,run_id TEXT NOT NULL,episode TEXT,name TEXT,at REAL,PRIMARY KEY(batch_run_id,run_id))')
 return c

def mano_removed(batch_run_id=None):
 with mano_removed_db() as c:
  rows=c.execute('SELECT batch_run_id,run_id FROM mano_removed'+(' WHERE batch_run_id=?' if batch_run_id else ''),(batch_run_id,) if batch_run_id else ()).fetchall()
 return {(r[0],r[1]) for r in rows}

MANO_DONE=set()  # publish까지 끝난 실행(run_id)은 다시 묻지 않습니다.

def mano_progress(run,history):
 run_id=run['dag_run_id']
 state=history.get(run_id)
 if not state:return 'MANO 대기',True
 if BATCH_OF.get(run_id) and (BATCH_OF[run_id],run_id) in mano_removed(BATCH_OF[run_id]):
  return 'MANO 대기 · 일괄 실행에서 제거됨',True
 if run_id in MANO_DONE:return '전체 완료',False
 in_batch=state in ('running','queued')
 for stage,label in zip(reversed(MANO_STAGES),('최종 저장','결과 검사','MANO 피팅')):
  st=call('POST','/worker/jobs/status',json=request_for(stage,run_id,run['conf']))
  status=st['state']
  if status in ('failed','interrupted'):
   return label+(' · 강제 종료' if st.get('stopped_by_user') else ' · 실패'),not in_batch
  if status=='success':
   if stage=='publish':MANO_DONE.add(run_id);return '전체 완료',False
   break
  if status in ('queued','running'):return label+' · 진행 중',False
 if in_batch:return 'MANO 실행 대기',False
 # 일괄 실행이 끝났는데 MANO를 시작하지 않은 에피소드(평일 오전 8시 이후 대기로 남김 등)
 return ('MANO 대기',True) if status=='not_started' else ('MANO 재개 대기',True)

class ManoSelection(BaseModel):
 runs:list[str]=Field(min_length=1,max_length=100)
 # 업데이트 전 대기열: 업데이트 전 코드로 고정된 에피소드만 3D 복원부터 다시 한 뒤 MANO 진행(그 밖의 에피소드는 MANO만)
 redo_3d:bool=False

@app.get('/api/pc1/mano')
def mano_candidates(req:Request):
 guard(req)
 history=batch_history()
 rows=[]
 for run in mano_ready_runs():
  conf=run['conf']
  if not access.allowed(req,conf['episode_id']):continue
  label,ready=mano_progress(run,history)
  # ready_at: MANO 대기가 된 시각(3D 복원까지 끝난 실행의 종료 시각) · UI가 날짜별로 나눠 보여 줍니다.
  if ready:rows.append({'run':run['dag_run_id'],'episode':conf['episode_id'],'status':label,'ready_at':run.get('end_date') or run.get('start_date')})
 return list(reversed(rows))

@app.post('/api/pc1/mano')
def launch_mano(data:ManoSelection,req:Request):
 guard(req);update_block()
 if len(set(data.runs))!=len(data.runs):raise HTTPException(400,'중복된 실행입니다.')
 with lock:
  check_batch_idle()
  # MANO 일괄 실행은 동시 실행 한도(MAX_PARALLEL_RUNS)와 무관하게 시작합니다. GPU 연산 겹침은 Worker GPU 슬롯이 조절합니다.
  sources=[]
  history=batch_history()
  for run_id in data.runs:
   reset_busy(run_id)
   run=local_api('GET',f'/api/v2/dags/{mano_dag(run_id)}/dagRuns/'+quote(run_id,safe=''))
   conf=run['conf']
   access.require(req,conf['episode_id'])
   if run['state']!='success' or not mano_progress(run,history)[1]:
    raise HTTPException(409,'복원 완료 후 MANO 대기 중인 실행만 선택하세요.')
   sources.append({'run_id':run_id,'conf':conf})
  for source in sources:
   if history.get(source['run_id']):
    # 3D 복원 다시는 일괄 실행 안에서만 실행되므로, 앞선 일괄 실행에서 실패·중단된 것도 다시 시작합니다.
    for stage in (REDO_STAGES if data.redo_3d else ())+MANO_STAGES:
     payload=request_for(stage,source['run_id'],source['conf'])
     if call('POST','/worker/jobs/status',json=payload)['state'] in ('failed','interrupted'):
      call('POST','/worker/jobs/retry',json=payload)
  ensure_worker_pool(worker(DEFAULT_WORKER))
  local_api('PATCH',f'/api/v2/dags/{BATCH_DAG}',json={'is_paused':False})
  now=datetime.now(timezone.utc)
  result=local_api('POST',f'/api/v2/dags/{BATCH_DAG}/dagRuns',json={'dag_run_id':'mano_'+secrets.token_hex(10),'logical_date':now.isoformat(),'conf':{'sources':sources,'redo_3d':data.redo_3d}})
  access.audit(req,'mano '+result['dag_run_id']+(' redo_3d' if data.redo_3d else ''))
  return {'run_id':result['dag_run_id'],'count':len(sources),'redo_3d':data.redo_3d}

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
 history=None;deferred=deferred_runs()
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
   if r['dag_run_id'] in deferred:
    # '3D까지만' 초기화한 실행: Airflow의 MANO 단계(skipped) 대신 Worker 상태(MANO 일괄 실행)를 보여 줍니다.
    item['mano_deferred']=True
    if 'tasks' in item:item['tasks']=[t for t in item['tasks'] if t['task'] not in MANO_STAGES]
   if (mode=='day' or r['dag_run_id'] in deferred) and r['state']=='success':
     if history is None:history=batch_history()
     item['mano_progress']=mano_progress(r,history)[0]
     # MANO 일괄 실행 단계(손·검사·저장)도 단계 목록과 로그 창에 보이도록 Worker 상태를 붙입니다.
     if detailed:
      for stage in MANO_STAGES:
       try:st=call('POST','/worker/jobs/status',json=request_for(stage,r['dag_run_id'],r.get('conf',{})),worker_id=wid,timeout=15)
       except (httpx.HTTPError,OSError,ValueError):break
       state={'success':'success','running':'running','queued':'queued','failed':'failed','interrupted':'failed'}.get(st['state'])
       entry={'task':stage,'state':state,'worker_state':st['state'],'mano':True}
       if st.get('progress'):entry['progress']=st['progress']
       item.setdefault('tasks',[]).append(entry)
   item['review_required']=next((t['review_stage'] for t in item.get('tasks',[]) if t.get('review_required')),None)
   if mode!='test' and detailed and r['state'] in ('success','failed') and (user['role']=='admin' or owner_of(r.get('conf',{}))==user['name']):
    # 단계 초기화 · 다시 실행: 끝났거나 실패한 실행에서 앞 단계가 모두 완료된 결과 단계
    item['reset_stages']=resettable_stages(item.get('tasks',[]))
   if r['dag_run_id'] in RESETS:item['reset']=RESETS[r['dag_run_id']]
   item['can_retry']=bool(r['state']=='failed' and (owners.get(item['episode']) or r.get('conf',{}).get('operator_id'))==user['name'] and any(t.get('state')=='failed' for t in item.get('tasks',[])))
   if mode!='test' and summary and not detailed:
    item['tasks']=[t for t in item['tasks'] if t['state'] not in ('success','skipped')]
    item['summary_only']=True
   item['operator']=owners.get(item['episode']) or r.get('conf',{}).get('operator_id') or '—';out.append(item)
 legacy=ROOT/'legacy_jobs.json'
 if legacy.exists():
  for item in json.loads(legacy.read_text()):
   if item['dag_id']!=DIRECT_DAGS.get('test') and access.allowed(req,item.get('episode','')):
    item['legacy']=True;out.append(item)
 return sorted(out,key=lambda x:x['start'] or '',reverse=True)

# ---- 단계별 로그 · 진행률 (작업 현황 화면의 로그 창) ----
JOB_STAGES=['prepare_raw','sam_settings_ready','sam3','mask_approved','preflight','track','tracking_approved','prepare_reconstruction','reconstruct','hands','validate','publish']
_ANSI=re.compile(r'\x1b\[[0-9;?]*[A-Za-z]')
_TQDM=re.compile(r'(?:(?P<label>[^|\r\n]{0,80}?):?\s*)?(?P<pct>\d{1,3})%\|[^|]*\|\s*(?P<done>\d+)\s*/\s*(?P<total>\d+)\s*\[(?P<elapsed>[0-9:]+)(?:<(?P<eta>[0-9:?]+))?(?:,\s*(?P<rate>[^\]]+))?\]')

def clean_log(text,limit=400):
 """ANSI 제거, \r 진행줄은 마지막 상태만, 진행률 원본줄·상태 상자는 숨기고 연속 중복을 합칩니다.
 진행률은 bars로 따로 돌려줍니다(라벨별 마지막 값, 처음 본 순서)."""
 bars={};lines=[]
 for raw in text.replace('\r\n','\n').split('\n'):
  seg=[s for s in raw.split('\r') if s.strip()]
  for line in ([seg[-1]] if seg else [''] ):
   line=_ANSI.sub('',line).rstrip()
   if 'PIPELINE_PROGRESS ' in line:
    try:
     v=json.JSONDecoder().raw_decode(line.split('PIPELINE_PROGRESS ',1)[1])[0]
     if v.get('total'):
      label=_short(v['label']);bars.pop(label,None)
      bars[label]={'label':label,'done':v['done'],'total':v['total'],'unit':v.get('unit','항목'),'percent':v.get('percent')}
    except (ValueError,KeyError,TypeError):pass
    continue
   m=_TQDM.search(line)
   if m:
    label=_short((m['label'] or '').strip(' :|')) or '진행';bars.pop(label,None)
    bars[label]={'label':label,'done':int(m['done']),'total':int(m['total']),'unit':'항목','percent':round(100*int(m['done'])/max(1,int(m['total'])),1),
                 'elapsed':m['elapsed'],'eta':m['eta'],'rate':(m['rate'] or '').strip()}
    continue
   if line.startswith('[진행] ') or ('──' in line and '|' in line):continue  # 진행 요약·상태 상자(진행률 막대로 대체)
   if not line.strip() and lines and not lines[-1].strip():continue
   if lines and line==lines[-1]:continue
   lines.append(line)
 while lines and not lines[-1].strip():lines.pop()
 # tqdm처럼 두 줄: 단계 전체 작업 수(구간) + 가장 최근에 갱신된 세부 작업
 order=list(bars.values())
 overall=[b for b in order if b['unit']=='구간' or b['label'].startswith('단계 내')]
 current=[b for b in order if b not in overall]
 return lines[-limit:],([overall[-1]] if overall else [])+([current[-1]] if current else [])

def _short(label):
 # 임시 폴더 경로는 파일 이름만 남깁니다: /tmp/wfm-stage-view-x/ep/color/1.mp4 → 1.mp4
 return re.sub(r'(?:/[^\s/·]+)+/([^\s/·]+)',r'\1',str(label)).strip()

@app.get('/api/jobs/log')
def job_log(req:Request,dag_id:str,run_id:str,stage:str):
 guard(req)
 if stage not in JOB_STAGES or not re.fullmatch(r'[A-Za-z0-9_\-]+',dag_id):raise HTTPException(400,'단계를 확인하세요.')
 from workers import dag_worker
 run=local_api('GET',f'/api/v2/dags/{dag_id}/dagRuns/'+quote(run_id,safe=''))
 conf=run.get('conf',{});ep=conf.get('episode_id','')
 user=access.user(req)
 if user['role']!='admin' and (access.owners().get(ep) or conf.get('operator_id'))!=user['name']:raise HTTPException(403,'본인 작업의 로그만 볼 수 있습니다.')
 return stage_log(dag_worker(dag_id),stage,run_id,conf)

def stage_log(wid,stage,run_id,conf):
 """Worker 단계 작업의 상태·정리된 로그·진행률(+세부 로그 파일)을 돌려줍니다. 작업 현황·MANO 일괄 로그 공용."""
 request=request_for(stage,run_id,conf)
 st=call('POST','/worker/jobs/status',json=request,worker_id=wid,timeout=15)
 text=call('POST','/worker/log',json=request,worker_id=wid,timeout=15).get('log','')
 lines,bars=clean_log(text)
 out={'stage':stage,'state':st.get('state'),'progress':st.get('progress'),'started':st.get('started'),'finished':st.get('finished'),
      'log':lines,'bars':bars,'sub':None}
 # 세부 로그: 진행 표시줄의 "로그: <파일>"(예: hands/logs/19_mano.log)을 작업 폴더 기준으로 읽습니다.
 subs=re.findall(r'로그: (\S+\.log)',_ANSI.sub('',text))
 if subs:
  try:
   project=call('GET','/worker/info',worker_id=wid,timeout=15)['project_dir'].rstrip('/')+'/'
   path=subs[-1]
   if path.startswith(project):
    rel=path[len(project):]
    # 로그는 계속 커지므로 파일 '끝부분'만 Range 요청으로 읽습니다(텍스트 미리보기는 앞 256KB만 돌려줌).
    pc=worker(wid)
    with httpx.Client(base_url=pc['url'],trust_env=False,timeout=15) as client:
     resp=client.get('/worker/outputs/file',params={'root':'project','path':rel},headers={'Authorization':'Bearer '+token(wid),'Range':'bytes=-80000'})
     resp.raise_for_status()
    tail=resp.content.decode('utf-8',errors='replace')
    if resp.status_code==206 and '\n' in tail:tail=tail.split('\n',1)[1]  # 잘린 첫 줄은 버림
    slines,sbars=clean_log(tail,250)
    for bar in sbars:  # 이름 없는 tqdm 막대는 세부 로그 파일 이름으로 표시 (예: 19_mano)
     if bar['label']=='진행':bar['label']=Path(rel).stem+' · 프레임';bar['unit']='프레임'
    out['sub']={'path':rel,'log':slines,'bars':sbars}
  except (httpx.HTTPError,KeyError,ValueError):pass
 return out

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
  gate=re.search(r'/episodes/(ep_[A-Za-z0-9_]+)/(tracking/)?decision$',path)
  if gate:
   try:approve=json.loads(await req.body()).get('decision')=='approve'
   except ValueError:approve=False
   if approve:
    stage='tracking_approved' if gate.group(2) else 'mask_approved'
    async def nudge():
     try:
      runs=await asyncio.to_thread(nudge_gate,wid,gate.group(1),stage)
      print('review approved → gate re-check',gate.group(1),stage,runs,flush=True)
     except Exception as e:print('gate nudge failed (sensor will retry):',e,flush=True)
    # 승인 응답은 지연 없이 돌려주고, Airflow 쪽 처리는 바로 이어서 백그라운드로 합니다.
    asyncio.get_running_loop().create_task(nudge())
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


@app.get('/assets/episode-actions.js')
def episode_actions_script(req:Request):
 guard(req)
 return FileResponse(Path(__file__).parent/'episode-actions.js',media_type='application/javascript',headers={'Cache-Control':'no-store'})


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
 deferred=deferred_runs()
 batch_states=batch_history() if any((v[3]=='day' or v[2]['dag_run_id'] in deferred) and v[2]['state']=='success' for v in latest.values()) else {}
 labels={'prepare_raw':'RAW 전처리','sam_settings_ready':'분할 설정 확인','sam3':'SAM3','mask_approved':'마스크 검수','preflight':'추적 사전 검사','track':'추적','tracking_approved':'추적 검수','prepare_reconstruction':'복원 준비','reconstruct':'3D 복원','hands':'MANO 피팅','validate':'결과 검사','publish':'최종 저장'}
 # 3D 복원까지 끝난 RAW(낮 실행 성공 → MANO 대기/완료, 또는 전체 파이프라인 성공)는 배정 부하에서 빠집니다.
 done={ep for ep,(_,_,r,mode) in latest.items() if r['state']=='success' and mode!='raw'}
 # 다른 PC(NAS 관제 기록)에서 이미 시작한 RAW는 이 PC에서 배정·표시하지 않습니다(이 PC에 담당·실행 기록이 있으면 유지).
 # 다른 PC가 선점(NAS control/claims)했거나 이미 시작한 RAW도 같습니다(배정만 하고 시작 전인 것 포함).
 # 그런 RAW는 날짜별 칸에 '다른 PC 담당'으로 조회만 할 수 있게 따로 붙입니다(배정·실행·제외 불가).
 started_other={};elsewhere={}
 try:
  if fleet.available():
   started_other=fleet.started_elsewhere()
   elsewhere={ep:pc for ep,pc in fleet.raw_claims().items() if pc!=fleet.PC_ID}
   for ep,pc in fleet.raw_distributed().items():  # 연속 모드로 다른 PC에 분배한 RAW
    if pc!=fleet.PC_ID:elsewhere.setdefault(ep,pc)
   for ep,pc in started_other.items():elsewhere.setdefault(ep,pc)
 except Exception:started_other,elsewhere={},{}
 queued=[ep for ep in episodes if raw_in_queue(ep,owners,latest)]
 episodes=[ep for ep in queued if ep not in elsewhere or owners.get(ep) or ep in latest]
 others=[ep for ep in queued if ep not in set(episodes)]
 # '내 작업에서 제외'한 RAW: 이 PC 기록은 NAS에도 남기고(예전 제외분 포함), 어느 PC든 제외한 RAW는 아무에게도 배정하지 않습니다.
 # 제외한 본인(제외 취소용)·관리자·이미 이 PC에서 담당/실행한 RAW가 아니면 목록에도 보이지 않습니다.
 local_excl=access.excluded_any(selected_worker(req))
 excluded_by={ep:pc.upper() for ep,pc in global_excluded().items()}
 for ep,op in local_excl.items():
  if ep not in excluded_by:fleet.raw_exclude(ep);excluded_by[ep]=op
 excluded=access.excluded_raw(user['name'],selected_worker(req))
 def hidden(ep):
  return user['role']!='admin' and ep in excluded_by and ep not in excluded and owners.get(ep)!=user['name'] and ep not in latest
 assigned,team=access.rebalance(selected_worker(req),episodes,done,set(latest),claim=fleet.raw_claim,release=fleet.raw_release,blocked=set(excluded_by),targets=queue_target_of)
 episodes=[ep for ep in episodes if not hidden(ep)]
 others=[ep for ep in others if not hidden(ep)]
 out=[]
 dist=my_distributed();contq=cont_waiting_eps()  # SAM3 연속 모드
 for ep in episodes:
  owner=owners.get(ep);row=raw_row(ep,owner,user)
  # 다른 작업자의 담당·진행 단계도 모두 보여 줍니다(실행·수정은 본인 것만).
  row.update(operator=owner,assigned_to=None if owner else assigned.get(ep),done=ep in done)
  row['excluded_from_mine']=user['role']!='admin' and ep in excluded
  row['can_exclude']=user['role']=='operator' and ep not in excluded and ((owner==user['name'] and ep not in done) or (not owner and assigned.get(ep)==user['name']))
  if user['role']!='admin':row['can_run']=ep not in excluded and (owner==user['name'] or (not owner and (assigned.get(ep)==user['name'] or ep in dist)))
  progress='실행 전' if not owner else '실행 기록 확인 필요'
  if not owner and ep not in latest:progress=('배정됨 · 실행 전' if assigned.get(ep) else '제외됨 · '+excluded_by[ep] if ep in excluded_by else '미배정 · 대기')
  if not owner and ep not in latest and ep in contq:progress='연속 모드 · 실행 대기'
  elif not owner and ep not in latest and ep in dist:progress='연속 모드 분배 · 포인트 전'
  if ep in latest:
   _,dag,r,mode=latest[ep]
   if (mode=='day' or r['dag_run_id'] in deferred) and r['state']=='success':progress=mano_progress(r,batch_states)[0]
   elif r['state']=='success':progress='RAW 전처리 완료' if mode=='raw' else '전체 완료'
   else:
    tasks=local_api('GET',f"/api/v2/dags/{dag}/dagRuns/"+quote(r['dag_run_id'],safe='')+'/taskInstances')['task_instances']
    active=next((t for t in tasks if t['state']=='failed'),None) or next((t for t in tasks if t['state'] in ('running','up_for_reschedule','up_for_retry')),None)
    if active:
     label=labels.get(active['task_id'],active['task_id']);state=active['state']
     progress=label+(' · 실패' if state=='failed' else ' · 재시도 대기' if state=='up_for_retry' else ' · 승인 대기' if active['task_id'] in ('mask_approved','tracking_approved') else ' · 진행 중')
    else:progress='실패' if r['state']=='failed' else '실행 대기'
  out.append(dict(row,progress=progress))
 for ep in others:
  pc=elsewhere[ep];ran=started_other.get(ep)==pc
  out.append({'episode':ep,'operator':None,'assigned_to':None,'can_run':False,'done':False,'excluded_from_mine':False,'can_exclude':False,
              'elsewhere':pc.upper(),'elsewhere_started':ran,'progress':'다른 PC에서 실행' if ran else '제외됨 · '+excluded_by[ep] if ep in excluded_by else '다른 PC 배정 · 실행 전'})
 mine=queue_target_info(user['name'])
 return {'episodes':out,'team':{'total_users':team['total'],'operators':team['operators'],'admins':team['admins'],
         'target':team['targets'].get(user['name'],mine['target']),'targets':team['targets'],'default_target':access.ASSIGN_TARGET,
         'my_queue':mine,'load':team['load'],'me':user['name'],'role':user['role']}}

# ---- 작업자별 RAW 큐 크기(각자 설정): NAS control/queue_targets/<이름>.json, NAS를 못 쓰면 이 PC UI DB ----
def queue_db():
 c=access.db()
 c.execute('CREATE TABLE IF NOT EXISTS queue_targets (name TEXT PRIMARY KEY,target INTEGER NOT NULL,at REAL)')
 return c

def queue_target_of(name):
 """작업자가 정한 큐 크기(정한 적 없으면 None → 기본 access.ASSIGN_TARGET). 배정할 때마다 NAS에서 읽습니다."""
 if fleet.available():return fleet.queue_target_get(name)
 with queue_db() as c:r=c.execute('SELECT target FROM queue_targets WHERE name=?',(name,)).fetchone()
 return r[0] if r else None

def queue_target_info(name):
 v=queue_target_of(name)
 return {'target':access.ASSIGN_TARGET if v is None else v,'custom':v is not None,'nas':fleet.available(),'min':fleet.QUEUE_MIN,'max':fleet.QUEUE_MAX}

class QueueTarget(BaseModel):
 target:int=Field(ge=0,le=30)

@app.post('/api/queue-target')
def set_queue_target(data:QueueTarget,req:Request):
 """내 RAW 큐 크기를 정합니다. NAS에 바로 저장되어 모든 PC 화면이 다음 새로고침부터 같은 값을 씁니다.
 줄이면 시작 전 배정 중 늦은 촬영부터 풀리고, 늘리면 남은 RAW에서 바로 채웁니다(실행을 시작한 RAW는 그대로)."""
 guard(req);u=access.user(req)
 if not fleet.QUEUE_NAME.fullmatch(u['name']):raise HTTPException(400,'PC1~PC6 이름으로 로그인한 작업자만 정할 수 있습니다.')
 if fleet.available():
  try:fleet.queue_target_set(u['name'],data.target,u.get('label') or u['name'])
  except (OSError,ValueError) as e:raise HTTPException(503,'NAS에 저장하지 못했습니다: '+str(e)[:200])
 else:
  with queue_db() as c:c.execute('INSERT OR REPLACE INTO queue_targets VALUES(?,?,?)',(u['name'],data.target,time.time()))
 access.audit(req,f'queue-target {data.target}')
 return queue_target_info(u['name'])

@app.get('/api/queue-targets')
def list_queue_targets(req:Request):
 """관리자: 작업자별 큐 크기(NAS 기록)"""
 guard(req);access.admin(req)
 return {'default':access.ASSIGN_TARGET,'nas':fleet.available(),'targets':fleet.queue_targets() if fleet.available() else {}}

class RawExclusion(BaseModel):
 episode:str=Field(pattern=r'^ep_[A-Za-z0-9_]+$')
 worker_id:str=DEFAULT_WORKER
 excluded:bool=True

@app.post('/api/raw-episodes/exclude')
def exclude_raw_episode(data:RawExclusion,req:Request):
 guard(req)
 person=access.user(req)
 if person['role']!='operator':raise HTTPException(403,'작업자 본인의 내 작업에서 사용할 수 있습니다.')
 worker(data.worker_id)
 with lock:
  if data.excluded:
   owner=access.owners().get(data.episode)
   with access.db() as c:
    assigned=c.execute('SELECT operator,worker_id FROM raw_assignments WHERE episode=?',(data.episode,)).fetchone()
    versions={r['episode'] for r in c.execute('SELECT episode FROM execution_versions WHERE raw_episode=?',(data.episode,))}
   if owner!=person['name'] and not (not owner and assigned and assigned['operator']==person['name'] and assigned['worker_id']==data.worker_id):
    if data.episode not in access.excluded_raw(person['name'],data.worker_id):raise HTTPException(403,'본인의 내 작업 항목만 제외할 수 있습니다.')
   def matches(conf):
    return (conf.get('raw_episode_id') or conf.get('episode_id'))==data.episode or conf.get('episode_id') in versions
   if any(matches(r.get('conf') or {}) for _,_,r in active_runs()) or any(matches(s.get('conf') or {}) for _,s in active_batch_sources()):
    raise HTTPException(409,'대기·실행 중인 작업이 있습니다. 내 작업 현황에서 먼저 강제 종료한 뒤 제외하세요.')
  access.exclude_raw(person['name'],data.worker_id,data.episode,data.excluded)
  # 다른 PC에도 배정·표시하지 않도록 NAS에 제외 표시를 남깁니다(제외 취소 시 이 PC가 남긴 표시만 지움).
  try:
   if data.excluded:fleet.raw_exclude(data.episode)
   elif not access.excluded_any(data.worker_id).get(data.episode):fleet.raw_unexclude(data.episode)
  except Exception:pass
 return {'ok':True,'excluded':data.excluded}

def global_excluded():
 """NAS에 기록된, 어느 PC에서든 '내 작업에서 제외'한 RAW → 제외한 PC"""
 try:return fleet.raw_excluded() if fleet.available() else {}
 except Exception:return {}

# 추적 결과가 지금 승인된 마스크와 맞지 않을 때(마스크를 고쳐 다시 승인했거나 아직 승인 전) 추적 검수에 나오는 사유.
STALE_TRACKING_REASONS=('추적 입력이 현재 승인본과 다릅니다','승인된 마스크가 없습니다','승인 이후 초안이 바뀌었습니다')

def tracking_input_stale(wid,ep):
 """추적 검수 항목이 '마스크가 바뀌어 추적을 다시 해야 함'을 알리면 True."""
 for x in all_review_items(wid):
  if x.get('episode')==ep and x.get('stage')=='tracking' and not x.get('unavailable'):
   return not x.get('ready',True) and any(r in str(x.get('reason','')) for r in STALE_TRACKING_REASONS)
 return False

# 10_reconstruct.verify_outputs: 3D 결과를 만든 뒤 kaist-full 코드(align.py 등)·입력·설정이 바뀌면 MANO 이후 단계가 이 문구로 멈춥니다.
STALE_3D_REASON='3D 입력/코드/설정 계약 변경'
AFTER_3D_STAGES=('hands','validate','publish')

def reconstruction_stale(wid,stage,run_id,conf):
 """실패한 MANO·검사·저장 단계의 마지막 시도 로그가 '3D 계약 변경'이면 True(3D 복원부터 다시 해야 함)."""
 from direct_worker import request_for
 try:log=call('POST','/worker/log',json=request_for(stage,run_id,conf),worker_id=wid,timeout=15).get('log','')
 except Exception:return False
 return STALE_3D_REASON in log.rsplit('WORKER stage=',1)[-1]

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
  reset_busy(data.run_id)
  base=f'/api/v2/dags/{data.dag_id}/dagRuns/'+quote(data.run_id,safe='')
  run=local_api('GET',base);conf=run.get('conf',{});ep=conf.get('episode_id','')
  owner=access.owners().get(ep) or conf.get('operator_id')
  if not owner or owner!=access.user(req)['name']:raise HTTPException(403,'이 에피소드를 실행한 작업자만 재시도할 수 있습니다.')
  if run['state']!='failed':raise HTTPException(409,'실패한 실행만 재시도할 수 있습니다.')
  running_here=0  # MANO 일괄 실행은 세지 않습니다.
  for other_wid,_,dag in active_dags():
   recent=local_api('GET',f'/api/v2/dags/{dag}/dagRuns',params={'limit':100,'order_by':'-logical_date'})['dag_runs']
   for other in recent:
    if other['dag_run_id']==data.run_id and dag==data.dag_id:continue
    if other_wid==wid and other['state'] in ('running','queued'):
     if run_waiting(dag,other):continue  # 설정·승인 대기는 세지 않음
     running_here+=1
     if other.get('conf',{}).get('episode_id')==ep:raise HTTPException(409,'같은 에피소드가 실행 중입니다. 완료 후 재시도하세요.')
     if running_here>=MAX_PARALLEL_RUNS:raise HTTPException(409,f'이 PC에서 이미 {MAX_PARALLEL_RUNS}개 작업이 실행 중입니다. 하나가 끝난 뒤 재시도하세요.')
    if other.get('conf',{}).get('episode_id')==ep and (other.get('logical_date') or '')>(run.get('logical_date') or ''):raise HTTPException(409,'더 최근에 실행한 에피소드입니다. 최신 실행을 확인하세요.')
  tasks=local_api('GET',base+'/taskInstances')['task_instances']
  failed=sorted((t for t in tasks if t['state']=='failed'),key=lambda t:order.index(t['task_id']))
  if not failed or failed[0]['task_id']!=data.stage:raise HTTPException(409,'현재 실패 단계가 변경됐습니다. 새로고침하세요.')
  start=data.stage
  # 추적 뒤 단계(3D 복원 등)가 실패했는데 추적 입력이 예전 마스크 기준이면, 실패 단계만 다시 해도 같은 오류가 반복됩니다.
  # 마스크 승인 확인 단계부터 다시 돌려 새 마스크로 추적·추적 검수·3D 복원을 이어서 진행합니다.
  if order.index(data.stage)>order.index('track') and tracking_input_stale(wid,ep):start='mask_approved'
  # 3D 결과 뒤 단계가 '3D 계약 변경'(파이프라인 업데이트로 kaist-full 코드가 바뀜 등)으로 실패했으면 같은 오류가 반복되므로
  # 3D 복원부터 다시 돌립니다. 3D 복원은 --auto-new-attempt로 기존 결과를 99_history에 보관하고 새로 만듭니다.
  elif data.stage in AFTER_3D_STAGES and reconstruction_stale(wid,data.stage,data.run_id,conf):start='reconstruct'
  targets=[t for t in tasks if order.index(t['task_id'])>=order.index(start)]
  rewound={t['task_id'] for t in targets if order.index(t['task_id'])<order.index(data.stage)}
  if any(t['state'] in (('running','queued','up_for_reschedule') if t['task_id'] in rewound else ('success','running','queued','up_for_reschedule')) for t in targets):raise HTTPException(409,'후속 단계에 완료 또는 실행 중인 작업이 있어 재시도를 중단했습니다.')
  payload={'dag_run_id':data.run_id,'task_ids':[t['task_id'] for t in targets],'only_failed':False,'reset_dag_runs':True,'include_upstream':False,'include_downstream':False,'prevent_running_task':True,'note':'UI retry by '+access.user(req)['name']}
  local_api('POST',f'/api/v2/dags/{data.dag_id}/clearTaskInstances',json={**payload,'dry_run':True})
  from direct_worker import request_for
  for task in targets:
   request=request_for(task['task_id'],data.run_id,conf)
   # Airflow만 실패로 표시되고 워커 작업은 계속 돌거나 이미 끝난 경우(예: 센서 'state mismatch')에는
   # 워커 작업을 건드리지 않고 Airflow 태스크만 다시 연결합니다. 센서가 진행 중인 작업을 이어서 확인합니다.
   # 'waiting'(검수 승인 대기)도 실패가 아니므로 같습니다. 다시 연결된 센서가 승인 여부를 다시 확인합니다.
   state=call('POST','/worker/jobs/status',json=request,worker_id=wid,timeout=15).get('state')
   if task['task_id'] in rewound:
    # 앞 단계 다시 실행: 끝난 워커 작업(성공 포함)을 시작 전으로 되돌립니다. 승인 대기 중이면 그대로 다시 연결합니다.
    if state in ('running','queued','waiting'):continue
    action='rewind'
   elif state in ('running','queued','success','waiting'):continue
   else:action='retry'
   try:call('POST','/worker/jobs/'+action,json=request,worker_id=wid)
   except httpx.HTTPStatusError as e:
    # 워커가 거부한 실제 사유를 보여 줍니다(일반 '서버 연결 실패' 문구 대신).
    try:detail=str(e.response.json().get('detail',''))
    except ValueError:detail=e.response.text
    raise HTTPException(409,f"{task['task_id']} 단계 재시도를 GPU PC가 거부했습니다: "+(detail.strip().splitlines()[-1][-300:] if detail.strip() else str(e.response.status_code))) from e
  local_api('POST',f'/api/v2/dags/{data.dag_id}/clearTaskInstances',json={**payload,'dry_run':False})
  access.audit(req,'retry '+data.run_id+' '+start)
  return {'ok':True,'stage':start,'requested_stage':data.stage}


# ---- 단계 초기화 · 다시 실행: 완료(또는 실패)한 실행의 한 단계부터 결과를 보관하고 다시 실행합니다 ----
# 원본 단계 스크립트는 기존 결과를 이어서 쓰므로 Worker(stage_reset.py)가 그 단계부터의 결과를 99_history/reset으로 옮기고
# 작업 상태를 되돌린 뒤, 이 서버가 Airflow 단계를 다시 실행합니다. 검수 확인 단계는 앞 결과 단계의 초기화로 함께 다시 열립니다.
RESET_START={'prepare_raw':'prepare_raw','sam_settings_ready':'sam3','sam3':'sam3','preflight':'preflight','track':'preflight',
             'prepare_reconstruction':'prepare_reconstruction','reconstruct':'prepare_reconstruction','hands':'hands','validate':'validate','publish':'publish'}
RESET_STARTS=('prepare_raw','sam3','preflight','prepare_reconstruction','hands','validate','publish')
RESETS={}  # run_id → {'state':'running'|'success'|'failed','stage','message','at'} (최근 초기화 진행 상황 · 작업 현황 표시용)

def resettable_stages(tasks):
 """완료된 단계 목록에서 초기화를 시작할 수 있는 단계: 그 단계가 끝났거나 실패했고, 앞 단계가 모두 완료된 경우."""
 by={t['task']:t.get('state') for t in tasks}
 out=[]
 for start in RESET_STARTS:
  if by.get(start) not in ('success','failed'):continue
  if all(by[t]=='success' for t in by if JOB_STAGES.index(t)<JOB_STAGES.index(start)):out.append(start)
 return out

def reset_busy(run_id):
 if RESETS.get(run_id,{}).get('state')=='running':raise HTTPException(409,'이 실행은 단계 초기화 중입니다. 끝난 뒤 다시 시도하세요.')

class ResetJob(BaseModel):
 dag_id:str
 run_id:str=Field(min_length=1,max_length=250)
 stage:str
 until_3d:bool=True  # 전체 파이프라인: 3D 복원까지만 다시 하고 MANO 대기로 둡니다(MANO는 일괄 실행에서).

@app.post('/api/jobs/reset')
def reset_job(data:ResetJob,req:Request):
 guard(req);update_block()
 return start_reset(data,req)

def start_reset(data,req=None,who=None):
 """단계 초기화 본체. req가 없으면 시스템 요청(업데이트 뒤 3D 복원 다시 등): 소유자 확인·감사 기록 대신 who로 남깁니다."""
 start=RESET_START.get(data.stage)
 wid=dag_worker(data.dag_id)
 if not start or data.dag_id not in [d for w,m,d in active_dags()]:raise HTTPException(400,'초기화할 단계를 확인하세요.')
 with lock:
  reset_busy(data.run_id)
  base=f'/api/v2/dags/{data.dag_id}/dagRuns/'+quote(data.run_id,safe='')
  run=local_api('GET',base);conf=run.get('conf',{});ep=conf.get('episode_id','')
  if req is not None:require_owner(req,conf)
  if run['state'] not in ('success','failed'):raise HTTPException(409,'완료되었거나 실패한 실행만 초기화할 수 있습니다. 진행 중이면 먼저 강제 종료하세요.')
  if active_batch_sources(run_id=data.run_id):raise HTTPException(409,'MANO 일괄 실행에 들어 있는 실행입니다. 일괄 실행이 끝나거나 제거한 뒤 초기화하세요.')
  tasks=[{'task':t['task_id'],'state':t['state']} for t in local_api('GET',base+'/taskInstances')['task_instances']]
  airflow_ids={t['task'] for t in tasks}
  mode=next((m for w,m,d in active_dags() if d==data.dag_id),None)
  was_deferred=data.run_id in deferred_runs()
  until_3d=data.until_3d and mode=='pipeline' and wid==DEFAULT_WORKER
  if start in MANO_STAGES and (start not in airflow_ids or was_deferred):
   # 낮 실행·'3D까지만' 초기화한 실행: MANO 단계는 Worker 작업 상태로 확인합니다.
   tasks=[t for t in tasks if t['task'] not in MANO_STAGES]
   for stage in MANO_STAGES:
    tasks.append({'task':stage,'state':{'success':'success','failed':'failed','interrupted':'failed'}.get(call('POST','/worker/jobs/status',json=request_for(stage,data.run_id,conf),worker_id=wid,timeout=15).get('state'))})
  if start not in resettable_stages(tasks):raise HTTPException(409,'이 단계는 아직 끝나지 않았거나 앞 단계가 완료되지 않아 초기화할 수 없습니다. 새로고침하세요.')
  rerun=[t for t in airflow_ids if JOB_STAGES.index(t)>=JOB_STAGES.index(start)]
  # 3D까지만: Airflow의 MANO 단계는 다시 실행하지 않고 skipped로 둡니다 → 실행이 3D 복원 뒤 성공으로 끝나 MANO 대기가 됩니다.
  skip=[t for t in MANO_STAGES if t in airflow_ids] if until_3d else []
  rerun=[t for t in rerun if t not in skip]
  defer=True if until_3d else (False if was_deferred else None)
  if rerun:
   # 다시 실행하면 이 실행이 동시 실행 자리를 차지합니다(MANO 일괄 실행·검수 대기는 세지 않음).
   running=[(w,d,r) for w,d,r in active_runs() if w==wid and not run_waiting(d,r)]
   if any((r.get('conf') or {}).get('episode_id')==ep for w,d,r in active_runs()):raise HTTPException(409,'같은 에피소드가 실행 중입니다.')
   if len(running)>=MAX_PARALLEL_RUNS:raise HTTPException(409,f'이 PC에서 이미 {MAX_PARALLEL_RUNS}개 작업이 실행 중입니다. 하나가 끝난 뒤 초기화하세요.')
  payload={'episode':ep,'run_id':data.run_id,'start':start}
  st=worker_action('reset',payload,wid)
  if st.get('state')=='failed':raise HTTPException(409,'초기화 실패: '+str(st.get('error','')))
  RESETS[data.run_id]={'state':'running','stage':start,'message':'결과 보관 중','at':time.time()}
  if req is not None:access.audit(req,'reset '+data.run_id+' '+start)
 spawn_reset(data.dag_id,wid,data.run_id,payload,sorted(rerun,key=JOB_STAGES.index),access.user(req)['name'] if req is not None else who,skip,defer)
 return {'ok':True,'stage':start,'requested_stage':data.stage,'airflow':bool(rerun),'until_3d':until_3d}

def spawn_reset(*args):
 threading.Thread(target=_finish_reset,args=args,name='kaist-reset',daemon=True).start()

def skip_tasks(dag_id,run_id,tasks,note):
 """Airflow 단계를 skipped로 바꿉니다. 뒤 단계부터 바꿔야 Airflow가 '뒤의 실패 단계 다시 시작'(set_task_instance_state)을 하지 않습니다."""
 base=f'/api/v2/dags/{dag_id}/dagRuns/'+quote(run_id,safe='')
 current={t['task_id']:t['state'] for t in run_tasks(dag_id,run_id)}
 for t in sorted(tasks,key=JOB_STAGES.index,reverse=True):
  if current.get(t)=='skipped':continue
  local_api('PATCH',base+'/taskInstances/'+quote(t,safe=''),json={'new_state':'skipped','note':note})

def _finish_reset(dag_id,wid,run_id,payload,rerun,who,skip=(),defer=None):
 """Worker 초기화(NAS 되돌리기·결과 보관·작업 되돌리기)가 끝나면 Airflow 단계를 다시 실행합니다."""
 entry=RESETS[run_id]
 try:
  deadline=time.monotonic()+6*3600
  while True:
   st=worker_action('reset-status',payload,wid)
   if st.get('state')!='running':break
   if time.monotonic()>deadline:raise RuntimeError('초기화가 6시간 안에 끝나지 않았습니다.')
   time.sleep(3)
  if st.get('state')!='success':raise RuntimeError(st.get('error') or '초기화 실패')
  with lock:
   base=f'/api/v2/dags/{dag_id}/dagRuns/'+quote(run_id,safe='')
   if (rerun or skip) and local_api('GET',base)['state'] not in ('success','failed'):raise RuntimeError('초기화 중 실행 상태가 바뀌어 다시 실행하지 않았습니다. 새로고침 후 확인하세요.')
   if skip:skip_tasks(dag_id,run_id,skip,'UI reset (3D까지만 · MANO 대기) by '+who)
   if defer is not None:set_deferred(dag_id,run_id,defer)
   # MANO 단계만 초기화(다시 실행할 Airflow 단계 없음): 실패로 끝났던 실행도 3D 복원까지는 끝났으므로 성공 → MANO 대기
   if skip and not rerun and local_api('GET',base)['state']=='failed':
    local_api('PATCH',base,json={'state':'success','note':'UI reset → MANO 대기 by '+who})
   if rerun:
    body={'dag_run_id':run_id,'task_ids':rerun,'only_failed':False,'reset_dag_runs':True,'include_upstream':False,
          'include_downstream':False,'prevent_running_task':True,'note':'UI reset by '+who}
    local_api('POST',f'/api/v2/dags/{dag_id}/clearTaskInstances',json={**body,'dry_run':True})
    local_api('POST',f'/api/v2/dags/{dag_id}/clearTaskInstances',json={**body,'dry_run':False})
  MANO_DONE.discard(run_id);finished_tasks.cache_clear()
  msg='다시 실행 시작'+(' · 3D 복원까지 하고 MANO 대기로 둡니다' if skip else '') if rerun else 'MANO 대기로 되돌림'
  entry.update(state='success',message=msg,archive=st.get('archive'),at=time.time())
 except Exception as e:
  entry.update(state='failed',message=str(getattr(e,'detail',None) or e)[-400:],at=time.time())


# ---- 목록 항목 관리: NAS에 올리기 · 강제 종료 · 목록에서 삭제(숨김) ----
ACTIVE_TI=('running','queued','scheduled','up_for_reschedule','up_for_retry','deferred','restarting')

def local_runner(wid):
 """Worker가 아직 새 동작(stop/sync)을 모르는 경우(재시작 전), 같은 PC의 runner.py를 직접 실행합니다."""
 if wid!=DEFAULT_WORKER:return None
 tf=worker(wid).get('token_file')
 path=Path(tf).with_name('runner.py') if tf else None
 return path if path and path.is_file() else None

def worker_action(action,payload,wid):
 try:return call('POST','/worker/jobs/'+action,json=payload,worker_id=wid,timeout=60)
 except httpx.HTTPStatusError as e:
  if e.response.status_code!=404:
   try:detail=e.response.json().get('detail','')
   except ValueError:detail=e.response.text
   raise HTTPException(409,'GPU PC 처리 실패: '+str(detail).strip().splitlines()[-1][-300:] if str(detail).strip() else 'GPU PC 처리 실패') from e
  runner=local_runner(wid)
  if not runner:raise HTTPException(503,'GPU PC Worker를 업데이트(재시작)한 뒤 사용할 수 있습니다.') from e
 r=subprocess.run(['/usr/bin/python3',str(runner),action],input=json.dumps(payload).encode(),capture_output=True,timeout=60)
 if r.returncode:raise HTTPException(409,'GPU PC 처리 실패: '+(r.stderr.decode(errors='replace').strip().splitlines() or [''])[-1][-300:])
 return json.loads(r.stdout)

def owner_of(conf):
 # 담당자 DB(이전된 담당자 포함)를 Airflow 실행 기록보다 먼저 봅니다.
 return access.owners().get(conf.get('episode_id','')) or conf.get('operator_id')

def require_owner(req,conf):
 user=access.user(req)
 if user['role']!='admin' and owner_of(conf)!=user['name']:raise HTTPException(403,'본인이 실행한 작업만 관리할 수 있습니다.')

def active_runs(episode=None,dag_id=None,run_id=None):
 """대기·실행 중인 파이프라인 실행 (worker_id, dag, run). 동시 실행 수를 차지하는 실행들입니다."""
 found=[]
 for wid,_,dag in active_dags():
  if dag_id and dag!=dag_id:continue
  for r in local_api('GET',f'/api/v2/dags/{dag}/dagRuns',params={'limit':100,'order_by':'-logical_date','state':['queued','running']})['dag_runs']:
   if r['state'] not in ('queued','running'):continue
   if run_id and r['dag_run_id']!=run_id:continue
   if episode and (r.get('conf') or {}).get('episode_id')!=episode:continue
   found.append((wid,dag,r))
 return found

def active_batch_sources(episode=None,run_id=None):
 out=[]
 for b in airflow_runs(BATCH_DAG,state=['queued','running']):
  if b['state'] not in ('queued','running'):continue
  for src in b['conf'].get('sources',[]):
   if (episode and src['conf'].get('episode_id')!=episode) or (run_id and src['run_id']!=run_id):continue
   out.append((b,src))
 return out

def fail_airflow_run(dag,run_id,note):
 """대기·실행 중인 단계를 failed로 바꾼 뒤 실행 전체를 failed로 끝냅니다(동시 실행 자리 반환).
 단계를 먼저 failed로 두므로 나중에 '실패 단계 다시 실행'으로 이어서 실행할 수 있습니다."""
 base=f'/api/v2/dags/{dag}/dagRuns/'+quote(run_id,safe='')
 failed=[]
 for t in run_tasks(dag,run_id):
  if t['state'] in ACTIVE_TI:
   try:local_api('PATCH',base+'/taskInstances/'+quote(t['task_id'],safe=''),json={'new_state':'failed','note':note});failed.append(t['task_id'])
   except httpx.HTTPError as e:print('task fail patch:',dag,run_id,t['task_id'],e,flush=True)
 if local_api('GET',base)['state'] in ('queued','running'):
  local_api('PATCH',base,json={'state':'failed','note':note})
 return failed

class StopRequest(BaseModel):
 episode:str=Field(pattern=r'^ep_[A-Za-z0-9_]+$')
 dag_id:str=Field(default='',max_length=250)
 run_id:str=Field(default='',max_length=250)

@app.post('/api/episodes/stop')
def stop_episode(data:StopRequest,req:Request):
 guard(req);access.require(req,data.episode)
 who=access.user(req)['name'];note='UI 강제 종료: '+who
 with lock:
  targets=active_runs(data.episode,data.dag_id or None,data.run_id or None)
  batch=active_batch_sources(data.episode,data.run_id or None)
  if data.run_id and not targets and not batch:
   raise HTTPException(409,'이 실행은 이미 끝났거나 대기·실행 중이 아닙니다.')
  if not targets and not batch:raise HTTPException(409,'이 에피소드에 대기·실행 중인 작업이 없습니다.')
  for _,_,r in targets:require_owner(req,r.get('conf') or {})
  for _,src in batch:require_owner(req,src['conf'])
  runs=[];stages=[]
  for wid,dag,r in targets:
   fail_airflow_run(dag,r['dag_run_id'],note)
   res=worker_action('stop',{'episode':data.episode,'run_id':r['dag_run_id']},wid)
   runs.append(r['dag_run_id']);stages+=res.get('stopped',[])
  # MANO 일괄 실행 안의 이 에피소드: MANO 단계만 끝내고, 일괄 실행은 다음 에피소드로 넘어갑니다.
  for b,src in batch:
   res=worker_action('stop',{'episode':data.episode,'run_id':src['run_id'],'stages':list(MANO_STAGES)},DEFAULT_WORKER)
   stages+=res.get('stopped',[])
   # 아직 시작 전인 MANO 단계도 건너뛰게 '강제 종료' 기록을 남깁니다(일괄 실행이 다음 에피소드로 진행).
   if not res.get('stopped'):
    skip=request_for('hands',src['run_id'],src['conf'])
    if call('POST','/worker/jobs/status',json=skip)['state']=='not_started':
     worker_action('skip',skip,DEFAULT_WORKER)
   runs.append(b['dag_run_id']+' (MANO 일괄)')
  access.audit(req,'force-stop '+data.episode+' '+','.join(runs))
  for wid in {t[0] for t in targets}:review_queue_cache.pop(wid,None)
  return {'ok':True,'runs':runs,'stages':stages}

class EpisodeRef(BaseModel):
 episode:str=Field(pattern=r'^ep_[A-Za-z0-9_]+$')

@app.post('/api/episodes/upload')
def upload_episode(data:EpisodeRef,req:Request):
 guard(req);access.require(req,data.episode)
 st=worker_action('sync',{'episode':data.episode},access.execution_worker(data.episode))
 access.audit(req,'nas-upload '+data.episode)
 return st

@app.get('/api/episodes/upload-status')
def upload_status(req:Request,episode:str):
 guard(req)
 if not re.fullmatch(r'ep_[A-Za-z0-9_]+',episode):raise HTTPException(400,'에피소드 이름을 확인하세요.')
 access.require(req,episode)
 return worker_action('sync-status',{'episode':episode},access.execution_worker(episode))

HIDE_SCOPES=('jobs','outputs','review')
class HideRequest(BaseModel):
 scope:str
 key:str=Field(min_length=1,max_length=600)
 episode:str=Field(pattern=r'^ep_[A-Za-z0-9_]+$')
 hidden:bool=True

@app.get('/api/hidden')
def hidden_list(req:Request,scope:str):
 guard(req)
 if scope not in HIDE_SCOPES:raise HTTPException(400,'목록 종류를 확인하세요.')
 with access.db() as c:rows=c.execute('SELECT key,episode FROM ui_hidden WHERE scope=?',(scope,)).fetchall()
 return {'keys':[r['key'] for r in rows if access.allowed(req,r['episode'])]}

@app.post('/api/hidden')
def hidden_set(data:HideRequest,req:Request):
 guard(req)
 if data.scope not in HIDE_SCOPES:raise HTTPException(400,'목록 종류를 확인하세요.')
 access.require(req,data.episode)
 if data.hidden:
  if data.scope=='jobs':
   try:dag,run=json.loads(data.key)
   except (ValueError,TypeError):raise HTTPException(400,'항목을 확인하세요.')
   busy=active_runs(dag_id=dag,run_id=run) or active_batch_sources(run_id=run)
  else:busy=active_runs(data.episode) or active_batch_sources(data.episode)
  if busy:raise HTTPException(409,'대기·실행 중인 작업이 있습니다. 먼저 강제 종료한 뒤 목록에서 삭제하세요.')
 with access.db() as c:
  if data.hidden:c.execute('INSERT OR REPLACE INTO ui_hidden VALUES(?,?,?,?,?)',(data.scope,data.key,data.episode,access.user(req)['name'],time.time()))
  else:c.execute('DELETE FROM ui_hidden WHERE scope=? AND key=?',(data.scope,data.key))
 access.audit(req,('hide ' if data.hidden else 'unhide ')+data.scope+' '+data.key)
 return {'ok':True}

# ---- MANO 일괄 실행 진행 로그 ----
MANO_LABELS={'hands':'MANO 피팅','validate':'결과 검사','publish':'최종 저장',**REDO_LABELS}
MANO_ALL_STAGES=REDO_STAGES+MANO_STAGES  # 강제 종료·대기열 제거 대상(3D 복원 다시 포함)

def mano_source_state(src,redo=False):
 """일괄 실행 안 한 에피소드의 MANO 단계 상태. 끝난 것은 다시 묻지 않습니다.
 redo(3D 복원 다시 일괄 실행): MANO 시작 전이면 3D 복원 단계 중 진행·실패한 것도 앞에 넣습니다.
 (원래 성공한 3D 복원은 넣지 않습니다. 다시 하지 않은 에피소드가 '다시 완료'로 보이지 않게)"""
 if src['run_id'] in MANO_DONE:return {s:{'state':'success'} for s in MANO_STAGES}
 states={}
 for stage in MANO_STAGES:
  st=call('POST','/worker/jobs/status',json=request_for(stage,src['run_id'],src['conf']),timeout=15)
  states[stage]=st
  if st['state']=='not_started':
   for rest in MANO_STAGES[MANO_STAGES.index(stage)+1:]:states[rest]={'state':'not_started'}
   break
 if states['publish']['state']=='success':MANO_DONE.add(src['run_id'])
 if redo and states['hands']['state']=='not_started':
  front={}
  for stage in REDO_STAGES:
   st=call('POST','/worker/jobs/status',json=request_for(stage,src['run_id'],src['conf']),timeout=15)
   if st['state'] in ('queued','running','failed','interrupted'):front[stage]=st
  states={**front,**states}
 return states

def mano_line(n,total,episode,states,waiting=''):
 head=f'[{n}/{total}] {episode}'
 if states['publish']['state']=='success':return head+'  ✔ 전체 완료'
 for stage in states:
  st=states[stage];label=MANO_LABELS[stage]
  if st['state'] in ('failed','interrupted'):return head+'  ✖ '+label+(' · 강제 종료(건너뜀)' if st.get('stopped_by_user') else ' · 실패(건너뜀)')
  if st['state'] in ('running','queued'):
   p=st.get('progress') or {}
   extra=f" · {p.get('label')} {p.get('done')}/{p.get('total')} {p.get('unit','')} ({p.get('percent')}%)" if p.get('total') else (' · '+p['label'] if p.get('label') else '')
   return head+'  ▶ '+label+(' · GPU 대기' if st['state']=='queued' else ' · 진행 중')+extra
  if st['state']=='not_started':
   done=[MANO_LABELS[s] for s in MANO_STAGES if states[s]['state']=='success']
   return head+('  · '+label+' 대기 ('+', '.join(done)+' 완료)' if done else '  · '+(waiting or '대기'))
 return head

@app.get('/api/pc1/mano/status')
def mano_status(req:Request,run_id:str=''):
 guard(req)
 batches=[]
 for b in airflow_runs(BATCH_DAG):
  sources=[s for s in b['conf'].get('sources',[]) if access.allowed(req,s['conf'].get('episode_id',''))]
  if sources:batches.append((b,sources))
  if len(batches)>=10:break
 out={'batches':[{'run_id':b['dag_run_id'],'state':b['state'],'start':b.get('start_date') or b.get('queued_at'),'end':b.get('end_date'),'count':len(srcs)} for b,srcs in batches],'selected':None}
 pick=next(((b,srcs) for b,srcs in batches if b['dag_run_id']==run_id),batches[0] if batches else None)
 if not pick:return out
 b,sources=pick;lines=[];current=None;total=len(b['conf'].get('sources',[]));redo=bool(b['conf'].get('redo_3d'))
 removed={r for _,r in mano_removed(b['dag_run_id'])};queue=[]
 for n,src in enumerate(b['conf'].get('sources',[]),1):
  if src not in sources:continue
  if src['run_id'] in removed:
   lines.append(f'[{n}/{total}] {src["conf"].get("episode_id")}  ⊘ 대기열에서 제거됨 (MANO 대기로 돌아감)');continue
  try:states=mano_source_state(src,redo)
  except (httpx.HTTPError,OSError,ValueError):lines.append(f'[{n}/{total}] {src["conf"].get("episode_id")}  ? GPU PC 연결 확인 필요');continue
  active=b['state'] in ('queued','running')
  lines.append(mano_line(n,total,src['conf'].get('episode_id'),states,'대기' if active else f'대기 · 시작 안 함 (평일 오전 {MANO_CUTOFF_HOUR}시 이후 또는 중단) → 목록에서 다시 선택'))
  if active and states['publish']['state']!='success':queue.append({'n':n,'episode':src['conf'].get('episode_id'),'run_id':src['run_id'],
   'running':any(st['state'] in ('running','queued') for st in states.values())})
  for stage in states:
   s=states[stage]['state']
   if s in ('running','queued') or (current is None and s in ('failed','interrupted')):
    if s in ('running','queued') or current is None:current=(src,stage,n)
 when=lambda v:datetime.fromisoformat(v.replace('Z','+00:00')).astimezone().strftime('%m-%d %H:%M:%S') if v else '—'
 state_label={'queued':'실행 대기','running':'진행 중','success':'완료','failed':'실패·중단'}.get(b['state'],b['state'])
 cut=mano_cutoff(b.get('start_date') or b.get('queued_at'))
 days='월화수목금토일'
 head=[f"MANO 일괄 실행 {b['dag_run_id']} · {state_label}",f"시작 {when(b.get('start_date') or b.get('queued_at'))}"+(f" · 종료 {when(b.get('end_date'))}" if b.get('end_date') else ''),
       f"새 에피소드 시작 마감: {cut:%m-%d}({days[cut.weekday()]}) {cut:%H:%M} · 이후에는 진행 중인 에피소드까지만 끝내고 나머지는 대기"]
 if redo:head.append('업데이트 전 대기열 3D 복원 다시: 업데이트 전 코드로 만든 3D 결과만 3D 복원부터 다시 한 뒤 MANO 진행')
 head.append('')
 sel={'run_id':b['dag_run_id'],'state':b['state'],'summary':head+lines,'current':None,'queue':queue,
      'can_stop':b['state'] in ('queued','running') and (access.user(req)['role']=='admin' or len(sources)==total)}
 if current:
  src,stage,n=current
  try:log=stage_log(DEFAULT_WORKER,stage,src['run_id'],src['conf'])
  except (httpx.HTTPError,OSError,ValueError) as e:log={'state':'unavailable','log':['로그를 불러오지 못했습니다: '+str(e)[:200]],'bars':[],'sub':None}
  sel['current']={'episode':src['conf'].get('episode_id'),'stage':stage,'label':MANO_LABELS[stage],'index':n,**log}
 out['selected']=sel
 return out

class ManoRemove(BaseModel):
 run_id:str=Field(min_length=1,max_length=250)        # MANO 일괄 실행
 source_run_id:str=Field(min_length=1,max_length=250) # 제거할 에피소드의 실행

@app.post('/api/pc1/mano/remove')
def mano_remove(data:ManoRemove,req:Request):
 """일괄 실행 대기열에서 에피소드 하나를 바로 뺍니다. 진행 중인 MANO 단계는 종료하고, 시작 전 단계는 건너뜀으로
 기록해 일괄 실행이 다음 에피소드로 넘어가게 합니다. 이 에피소드는 곧바로 'MANO 대기'로 돌아갑니다(다시 선택·이관 가능)."""
 guard(req)
 with lock:
  b=local_api('GET',f'/api/v2/dags/{BATCH_DAG}/dagRuns/'+quote(data.run_id,safe=''))
  if b['state'] not in ('queued','running'):raise HTTPException(409,'이미 끝난 일괄 실행입니다.')
  src=next((x for x in b['conf'].get('sources',[]) if x['run_id']==data.source_run_id),None)
  if not src:raise HTTPException(404,'이 일괄 실행에 없는 에피소드입니다.')
  ep=src['conf'].get('episode_id','');access.require(req,ep)
  if (data.run_id,data.source_run_id) in mano_removed(data.run_id):return {'ok':True,'episode':ep,'stopped':[],'already':True}
  states=mano_source_state(src)
  if states['publish']['state']=='success':raise HTTPException(409,'이미 MANO까지 끝난 에피소드입니다.')
  # 3D 복원 다시 준비(결과 보관·작업 되돌리기) 중에 빼면, 준비가 끝나며 건너뜀 표시까지 되돌려 일괄 실행이 이어서 진행합니다.
  if b['conf'].get('redo_3d') and worker_action('reset-status',{'episode':ep,'run_id':src['run_id'],'start':REDO_START},DEFAULT_WORKER).get('state')=='running':
   raise HTTPException(409,'3D 복원 다시 준비(이전 결과 보관) 중입니다. 잠시 뒤 다시 제거하세요.')
  res=worker_action('stop',{'episode':ep,'run_id':src['run_id'],'stages':list(MANO_ALL_STAGES)},DEFAULT_WORKER)
  # 아직 시작 전인 다음 MANO 단계는 '사용자 강제 종료'로 기록 → 일괄 실행이 이 에피소드를 건너뜁니다.
  if not res.get('stopped'):
   for stage in MANO_STAGES:
    st=call('POST','/worker/jobs/status',json=request_for(stage,src['run_id'],src['conf']))
    if st['state']=='success':continue
    if st['state']=='not_started':worker_action('skip',request_for(stage,src['run_id'],src['conf']),DEFAULT_WORKER)
    break
  with mano_removed_db() as c:c.execute('INSERT OR REPLACE INTO mano_removed VALUES(?,?,?,?,?)',(data.run_id,data.source_run_id,ep,access.user(req)['name'],time.time()))
  access.audit(req,'mano-remove '+ep+' from '+data.run_id)
  return {'ok':True,'episode':ep,'stopped':res.get('stopped',[])}

class ManoStop(BaseModel):
 run_id:str=Field(min_length=1,max_length=250)

@app.post('/api/pc1/mano/stop')
def mano_stop(data:ManoStop,req:Request):
 guard(req)
 with lock:
  b=local_api('GET',f'/api/v2/dags/{BATCH_DAG}/dagRuns/'+quote(data.run_id,safe=''))
  if b['state'] not in ('queued','running'):raise HTTPException(409,'이미 끝난 일괄 실행입니다.')
  sources=b['conf'].get('sources',[])
  for src in sources:access.require(req,src['conf'].get('episode_id',''))
  fail_airflow_run(BATCH_DAG,data.run_id,'UI 강제 종료: '+access.user(req)['name'])
  stopped=[]
  for src in sources:
   res=worker_action('stop',{'episode':src['conf']['episode_id'],'run_id':src['run_id'],'stages':list(MANO_ALL_STAGES)},DEFAULT_WORKER)
   stopped+=[src['conf']['episode_id']+' '+s for s in res.get('stopped',[])]
  access.audit(req,'force-stop mano '+data.run_id)
  return {'ok':True,'stopped':stopped}


# ================= NAS 관제 · 원격 실행 (관리자) =================
# 각 PC의 UI가 20초마다 자기 상태를 NAS control/status/<pc>.sqlite 에 쓰고, 자기 수신함(control/inbox/<pc>)의
# 원격 명령을 처리합니다. 관리자는 어느 PC의 UI에서든 전체 PC를 보고 명령을 보냅니다(관리자 확인 후 실행).
#  - run_raw     : 대상 PC가 새 RAW를 자기 PC에서 처음부터 실행 (실행자 = 대상 PC 이름 → NAS results/<대상 pc>)
#  - mano_export : 원본 PC가 'MANO 대기' 에피소드를 NAS work/<원본 pc>에 검증 업로드 → 대상 PC에 mano_import 전달
#  - mano_import : 대상 PC가 NAS에서 가져와(adopt) MANO 피팅 · 결과 검사 · 최종 저장(results/<대상 pc>)까지 실행
#  - mano_release: 원본 PC의 이관 표시를 풀어 다시 'MANO 대기'로 (대상 PC에서 실패했을 때)
import fleet
from starlette.requests import Request as _StarletteRequest
FLEET_INTERVAL=20
TRANSFER_STAGES=('adopt','hands','validate','publish')
_fleet_stop=threading.Event()
_fleet_lock=threading.Lock()
_published_cache=[0,[]]

def fleet_db():
 c=access.db()
 c.execute('CREATE TABLE IF NOT EXISTS fleet_tasks (id TEXT PRIMARY KEY,type TEXT,payload TEXT,state TEXT,step TEXT,message TEXT,created REAL,updated REAL)')
 # 다른 PC로 이관한(또는 이관 중인) MANO 대기 실행. 여기에 있으면 이 PC의 MANO 대기·일괄 실행에서 빠집니다.
 c.execute('CREATE TABLE IF NOT EXISTS mano_transfers (run_id TEXT PRIMARY KEY,episode TEXT,target TEXT,state TEXT,task_id TEXT,at REAL)')
 return c

def mano_transfer_of(run_id):
 with fleet_db() as c:r=c.execute('SELECT target,state FROM mano_transfers WHERE run_id=?',(run_id,)).fetchone()
 return dict(r) if r else None

_mano_progress_base=mano_progress
def mano_progress(run,history):
 t=mano_transfer_of(run['dag_run_id'])
 if t:return ('다른 PC로 이관 중 → '+t['target'] if t['state']=='exporting' else '이관됨 → '+t['target']+' (MANO는 그 PC에서 실행)'),False
 return _mano_progress_base(run,history)

def task_update(task_id,**fields):
 fields['updated']=time.time()
 with fleet_db() as c:c.execute('UPDATE fleet_tasks SET '+','.join(k+'=?' for k in fields)+' WHERE id=?',(*fields.values(),task_id))

def internal_request(token_value):
 host=sorted(ALLOWED_HOSTS)[0]
 return _StarletteRequest({'type':'http','method':'POST','path':'/api/run','query_string':b'','scheme':'http','server':('127.0.0.1',0),
  'headers':[(b'host',host.encode()),(b'origin',('http://'+host).encode()),(b'x-platform-csrf',CSRF.encode()),(b'cookie',('kaist_session='+token_value).encode())]})

def fleet_run_raw(task,p):
 ep=p['episode']
 where=fleet.raw_taken_elsewhere().get(ep)
 if where:raise RuntimeError(f'이 RAW는 이미 {where.upper()}에서 작업 중입니다.')
 # 이 PC 이름(PC1~PC6)의 관리자 세션으로 일반 실행과 같은 검사를 거쳐 시작합니다. 결과는 results/<이 pc>.
 tok=access.login(fleet.PC_ID.upper(),'admin')
 try:
  res=launch(Launch(mode='pipeline',worker_id=DEFAULT_WORKER,episode_id=ep,sam_prompt=p['sam_prompt'],confirmed=True,preview_episode=ep),internal_request(tok))
 finally:
  with access.db() as c:c.execute('DELETE FROM ui_sessions WHERE token=?',(tok,))
 task_update(task['id'],state='done',message=f"실행 시작: {res['episode']} ({res['run_id']}) · 분할 대상 {p['sam_prompt']}")

def fleet_mano_export(task,p):
 run_id,ep,target=p['run_id'],p['episode'],p['target_pc']
 if not task['step']:
  run=local_api('GET',f'/api/v2/dags/{mano_dag(run_id)}/dagRuns/'+quote(run_id,safe=''))
  if run['conf'].get('episode_id')!=ep or run['state']!='success':raise RuntimeError('복원이 끝난 실행이 아닙니다.')
  label,ready=mano_progress(run,batch_history())
  if not ready:raise RuntimeError('MANO 대기 상태가 아닙니다: '+label)
  with fleet_db() as c:c.execute('INSERT INTO mano_transfers VALUES(?,?,?,?,?,?)',(run_id,ep,target,'exporting',task['id'],time.time()))
  worker_action('sync',{'episode':ep},DEFAULT_WORKER)
  task_update(task['id'],state='running',step='syncing',message='NAS work/'+fleet.PC_ID+' 에 작업 폴더 업로드 중')
  return
 st=worker_action('sync-status',{'episode':ep},DEFAULT_WORKER)
 if st['state']=='running':return
 if st['state']!='success':
  with fleet_db() as c:c.execute('DELETE FROM mano_transfers WHERE run_id=?',(run_id,))
  raise RuntimeError('NAS 업로드 실패: '+str(st.get('error') or st['state']))
 run=local_api('GET',f'/api/v2/dags/{mano_dag(run_id)}/dagRuns/'+quote(run_id,safe=''))
 conf=run['conf']
 fleet.send(target,{'type':'mano_import','episode':ep,'source_pc':fleet.PC_ID,'source_run_id':run_id,
  'source_key':hashlib.sha256(run_id.encode()).hexdigest()[:24],'sam_prompt':conf.get('sam_prompt',''),
  'raw_episode':conf.get('raw_episode_id'),'requested_by':p.get('requested_by'),'target_pc':target})
 with fleet_db() as c:c.execute("UPDATE mano_transfers SET state='sent' WHERE run_id=?",(run_id,))
 task_update(task['id'],state='done',step='sent',message=f'NAS 업로드 완료 → {target}에 MANO 실행 요청')

def transfer_request(p,stage):
 req={'stage':stage,'episode':p['episode'],'run_id':'transfer__'+p['source_pc']+'__'+p['source_run_id'],
      'sam_prompt':p.get('sam_prompt',''),'sam_scene':'2d','sam_confirmed':False}
 if stage=='adopt':req.update(transfer_from=p['source_pc'],transfer_key=p['source_key'])
 if stage=='publish':req['result_pc']=fleet.PC_ID
 return req

def fleet_mano_import(task,p):
 labels={'adopt':'NAS에서 가져오기','hands':'MANO 피팅','validate':'결과 검사','publish':'최종 저장'}
 i=int(task['step'] or 0)
 if i==0 and task['state']=='received':
  with access.db() as c:c.execute('INSERT OR IGNORE INTO episode_owners VALUES(?,?)',(p['episode'],fleet.PC_ID.upper()))
 stage=TRANSFER_STAGES[i];req=transfer_request(p,stage)
 st=worker_action('status',req,DEFAULT_WORKER)
 if st['state']=='not_started':
  st=worker_action('start',req,DEFAULT_WORKER)
 if st['state'] in ('queued','running','not_started'):
  task_update(task['id'],state='running',step=str(i),message=f'{labels[stage]} 진행 중 ({i+1}/{len(TRANSFER_STAGES)})');return
 if st['state']=='success':
  if i+1<len(TRANSFER_STAGES):
   task_update(task['id'],state='running',step=str(i+1),message=f'{labels[stage]} 완료');return
  task_update(task['id'],state='done',step=str(i),message=f"전체 완료 · NAS results/{fleet.PC_ID}/{p.get('sam_prompt') or '미지정'}/{p['episode']}");return
 try:log=call('POST','/worker/log',json=req)['log'][-800:]
 except Exception:log=''
 raise RuntimeError(f'{labels[stage]} 실패 ({st["state"]}). '+log.strip().splitlines()[-1][-300:] if log.strip() else f'{labels[stage]} 실패 ({st["state"]})')

def fleet_mano_release(task,p):
 with fleet_db() as c:n=c.execute('DELETE FROM mano_transfers WHERE run_id=?',(p['run_id'],)).rowcount
 task_update(task['id'],state='done',message='이관 표시 해제 · 다시 MANO 대기' if n else '이관 기록이 없습니다.')

def fleet_raw_reset(task,p):
 """이 PC의 시작 전 RAW 배정을 풉니다. episode가 있으면 그 RAW만(PC 간 겹침 정리), 없으면 대기열 전체 초기화
 (배정 + '내 작업에서 제외' 기록). 실행을 시작한 RAW(담당·실행 기록)·결과 파일은 건드리지 않습니다."""
 ep=p.get('episode') or None
 if ep and not fleet.RAW_NAME.fullmatch(ep):raise RuntimeError('RAW 이름이 잘못되었습니다.')
 own=access.owners()
 with access._assign_lock:
  with access.db() as c:
   rows=[r[0] for r in (c.execute('SELECT episode FROM raw_assignments WHERE episode=?',(ep,)) if ep else c.execute('SELECT episode FROM raw_assignments'))]
   unmark=[] if ep else [r[0] for r in c.execute('SELECT DISTINCT episode FROM raw_exclusions')]
   c.execute('DELETE FROM raw_assignments WHERE episode=?',(ep,)) if ep else c.execute('DELETE FROM raw_assignments')
   excl=0 if ep else c.execute('DELETE FROM raw_exclusions').rowcount
   c.execute('INSERT INTO ui_audit VALUES(?,?,?)',(time.time(),str(p.get('requested_by')),'raw-reset '+(ep or 'all')))
  for e in rows:
   if not own.get(e):fleet.raw_release(e)
  for e in unmark:fleet.raw_unexclude(e)  # 이 PC가 NAS에 남긴 제외 표시도 풉니다.
  # SAM3 연속 모드: 이 PC에 분배된 시작 전 RAW와 실행 전 대기열 항목도 풉니다.
  dist=[e for e in my_distributed() if (not ep or e==ep) and not own.get(e)]
  for e in dist:fleet.raw_undistribute(e,fleet.PC_ID)
  with cont_db() as c:
   ncont=c.execute("UPDATE sam_queue SET state='cancelled' WHERE state IN ('waiting','failed')"+(' AND raw_episode=?' if ep else ''),(ep,) if ep else ()).rowcount
  # 겹침 정리: 남기는 PC가 따로 있으면 이 PC의 선점도 풉니다(이 PC가 실행한 RAW는 제외).
  if ep and not own.get(ep):fleet.raw_release(ep)
 msg=(f'{ep} 시작 전 배정 해제' if ep else f'대기열 초기화: 시작 전 배정 {len(rows)}개 · 제외 기록 {excl}개 해제')+(' (배정 없음)' if not rows and not dist else '')
 if dist or ncont:msg+=f' · 연속 모드 분배 {len(dist)}개 · 대기열 {ncont}개 해제'
 task_update(task['id'],state='done',message=msg)

def fleet_reconstruct_redo(task,p):
 """이전 updater가 보낸 자동 재복원 요청은 종료합니다. 재복원은 사용자가 직접 선택합니다."""
 task_update(task['id'],state='cancelled',
             message='자동 3D 재복원 중지 · 추가 실행 없음 · 이미 시작한 작업은 계속 진행합니다. 필요하면 작업 목록에서 직접 재실행하세요.')

FLEET_HANDLERS={'reconstruct_redo':fleet_reconstruct_redo,'raw_reset':fleet_raw_reset,'run_raw':fleet_run_raw,'mano_export':fleet_mano_export,'mano_import':fleet_mano_import,'mano_release':fleet_mano_release}

def fleet_process():
 for cmd in fleet.claim():
  if cmd.get('type') not in FLEET_HANDLERS or not re.fullmatch(r'[A-Za-z0-9_]+',str(cmd.get('id',''))):continue
  with fleet_db() as c:c.execute('INSERT OR IGNORE INTO fleet_tasks VALUES(?,?,?,?,?,?,?,?)',(cmd['id'],cmd['type'],json.dumps(cmd,ensure_ascii=False),'received','','수신',time.time(),time.time()))
 with fleet_db() as c:tasks=[dict(r) for r in c.execute("SELECT * FROM fleet_tasks WHERE state IN ('received','running') ORDER BY created")]
 for task in tasks:
  p=json.loads(task['payload'])
  try:FLEET_HANDLERS[task['type']](task,p)  # run_raw의 launch()는 실행 잠금(lock)을 스스로 잡습니다.
  except HTTPException as e:task_update(task['id'],state='failed',message=str(e.detail)[-400:])
  except Exception as e:task_update(task['id'],state='failed',message=str(e)[-400:])

def published_rows():
 if time.monotonic()-_published_cache[0]<300:return _published_cache[1]
 rows=[];base=Path(os.environ.get('NAS_MOUNT','/mnt/kaist-data'))/'results'/fleet.PC_ID
 try:
  for d in base.iterdir() if base.is_dir() else []:
   if not d.is_dir() or d.name.startswith('.'):continue
   if d.name.startswith('ep_'):eps=[('(이전 형식)',d)]
   else:eps=[(d.name,e) for e in d.iterdir() if e.is_dir() and e.name.startswith('ep_')]
   for target,e in eps:
    for k in e.iterdir():
     if k.is_dir() and not k.name.startswith('.'):rows.append({'sam_target':target,'episode':e.name,'run_key':k.name,'at':k.stat().st_mtime})
 except OSError:pass
 _published_cache[:]=[time.monotonic(),rows];return rows

# ---- 단계별 작업 시간 기록: 이 PC Worker의 jobs/<id>/{request,status}.json (재시도 이전 시도는 attempts/) ----
JOB_LOG_DAYS=int(os.environ.get('KAIST_JOB_LOG_DAYS','14'))
_job_cache=[0,[]]

def worker_jobs_dir():
 d=os.environ.get('KAIST_WORKER_JOBS_DIR')
 if d:return Path(d)
 tf=worker(DEFAULT_WORKER).get('token_file')
 return Path(tf).with_name('jobs') if tf else None

def job_records(operators=None):
 """[{run_id,episode,stage,state,operator,sam_prompt,created,started,finished,attempt}] 최근 JOB_LOG_DAYS일.
 attempt=0 현재(마지막) 시도, 1 이상 = 재시도로 보관된 이전 시도."""
 if time.monotonic()-_job_cache[0]<30:rows=_job_cache[1]
 else:
  rows=[];root=worker_jobs_dir();since=time.time()-JOB_LOG_DAYS*86400
  try:dirs=[d for d in root.iterdir() if d.is_dir() and not d.name.startswith('_')] if root and root.is_dir() else []
  except OSError:dirs=[]
  for d in dirs:
   try:
    if (d/'status.json').stat().st_mtime<since:continue
    req=json.loads((d/'request.json').read_text())
    tries=[json.loads((d/'status.json').read_text())]
    if (d/'attempts').is_dir():
     tries+=sorted((json.loads((a/'status.json').read_text()) for a in (d/'attempts').iterdir() if (a/'status.json').is_file()),key=lambda x:x.get('started') or 0,reverse=True)
   except (OSError,ValueError):continue
   for i,st in enumerate(tries):
    if not st.get('started'):continue
    rows.append({'run_id':req.get('run_id'),'episode':req.get('episode'),'stage':req.get('stage'),'state':st.get('state'),'operator':None,
                 'sam_prompt':req.get('sam_prompt'),'created':st.get('created'),'started':st.get('started'),'finished':st.get('finished'),'attempt':i})
  _job_cache[:]=[time.monotonic(),rows]
 owners=access.owners();operators=operators or {}
 return [dict(r,operator=owners.get(r['episode']) or operators.get(r['run_id'])) for r in rows]

# ---- 처리 단계별 프레임 수(관리자 'RAW 프레임' 탭): 이 PC 작업 폴더(GPU_PROJECT_DIR)의 단계 기록 ----
# 동기화(4대 카메라 시각 맞춤) → 전처리(앞뒤 자르기 후) → SAM3 마스크. 그 뒤 추적·3D 복원·MANO는 전처리 프레임 전체를 그대로 씁니다
# (추적은 metadata.json frame_num 전체, MANO 검증은 모든 프레임이 있어야 통과). 작업 폴더가 정리돼도 남도록 UI DB에 기록해 둡니다.
WORK_DIR=os.environ.get('KAIST_WORK_DIR') or os.environ.get('GPU_PROJECT_DIR') or ''
FRAME_SCAN_SECONDS=60
_frame_scan=[0.0]

def _read_json(p):
 try:return json.loads(p.read_text())
 except (OSError,ValueError):return None

def _int(v):return v if isinstance(v,int) and not isinstance(v,bool) and v>=0 else None

def stage_frames(root,ep):
 """{synced,prepared,masks,object_frames} 중 작업 폴더에서 확인된 값만."""
 out={}
 trim=_read_json(root/'03_prepared'/ep/'trim.json') or {}
 prep=_read_json(root/'03_prepared'/ep/'preparation.json') or {}
 if _int(trim.get('before')) is not None:out['synced']=trim['before']
 if _int(prep.get('frames')) is not None:out['prepared']=prep['frames']
 elif _int(trim.get('after')) is not None:out['prepared']=trim['after']
 for bucket in ('approved','pending',''):
  m=root/'04_masks'/bucket/ep
  val=_read_json(m/'mask_validation.json')
  if not val or _int(val.get('frames')) is None:continue
  out['masks']=val['frames']
  tracks=((_read_json(m/'state.json') or {}).get('object_tracks') or {}).values()
  seen=[t['nonempty_frames'] for t in tracks if isinstance(t,dict) and _int(t.get('nonempty_frames')) is not None]
  if seen:out['object_frames']=min(seen)   # 물체 마스크가 비어 있지 않은 프레임(카메라 중 가장 적은 값)
  break
 return out

def frames_db():
 c=access.db()
 c.execute('CREATE TABLE IF NOT EXISTS episode_frames (episode TEXT PRIMARY KEY,raw_episode TEXT,synced INTEGER,prepared INTEGER,masks INTEGER,object_frames INTEGER,at REAL)')
 return c

def episode_frame_rows(job_eps=()):
 """이 PC 실행(출력 에피소드)별 단계 프레임 수. FRAME_SCAN_SECONDS마다 작업 폴더를 다시 읽어 DB를 갱신합니다."""
 root=Path(WORK_DIR) if WORK_DIR else None
 if root and root.is_dir() and time.monotonic()-_frame_scan[0]>=FRAME_SCAN_SECONDS:
  _frame_scan[0]=time.monotonic()
  with access.db() as c:runs=dict(c.execute('SELECT episode,raw_episode FROM execution_versions').fetchall())
  for ep in set(job_eps):runs.setdefault(ep,None)
  try:
   for d in (root/'03_prepared').iterdir():runs.setdefault(d.name,None)   # 실행 기록보다 오래된 작업 폴더
  except OSError:pass
  with frames_db() as c:
   for ep,raw in runs.items():
    if not ep or not re.fullmatch(r'ep_[A-Za-z0-9_]+',ep):continue
    f=stage_frames(root,ep)
    if not f:continue
    # 값이 없어진 단계(작업 폴더 정리)는 이전 기록을 유지합니다.
    c.execute('INSERT INTO episode_frames VALUES(?,?,?,?,?,?,?) ON CONFLICT(episode) DO UPDATE SET raw_episode=COALESCE(excluded.raw_episode,raw_episode),'
              'synced=COALESCE(excluded.synced,synced),prepared=COALESCE(excluded.prepared,prepared),masks=COALESCE(excluded.masks,masks),'
              'object_frames=COALESCE(excluded.object_frames,object_frames),at=excluded.at',
              (ep,raw,f.get('synced'),f.get('prepared'),f.get('masks'),f.get('object_frames'),time.time()))
 with frames_db() as c:
  return [dict(r) for r in c.execute('SELECT * FROM episode_frames')]

def update_busy():
 """repro/update_stack.py가 서비스 재시작 전에 확인: 실행 중인 Airflow 태스크 수 · MANO 일괄 실행 여부."""
 try:
  tis=local_api('GET','/api/v2/dags/~/dagRuns/~/taskInstances',params={'state':['running','queued'],'limit':1})['total_entries']
  batch=any(r['state'] in ('queued','running') for r in airflow_runs(BATCH_DAG))
  return {'running_tis':tis,'batch':batch}
 except Exception as e:return {'error':str(e)[-200:]}

def fleet_snapshot():
 snap={'meta':{'host':os.uname().nodename,'label':worker(DEFAULT_WORKER).get('label'),'gpu_error':None,'ui_url':fleet.ui_url(ALLOWED_HOSTS),'update_busy':update_busy()}}
 try:
  try:g=call('GET','/worker/gpu',worker_id=DEFAULT_WORKER)
  except (httpx.HTTPError,OSError,ValueError):g=local_gpu_status()
  snap['gpu']=[{'idx':x.get('index'),'name':x.get('name'),'uuid':x.get('uuid'),'used_mb':x.get('used_mb'),'total_mb':x.get('total_mb'),'util':x.get('util'),'processes':len(x.get('processes') or [])} for x in g['gpus'] if not g.get('worker_gpu_uuid') or x.get('uuid')==g['worker_gpu_uuid']]
 except Exception as e:snap['meta']['gpu_error']=str(e)[-200:]
 runs=[];started=set(access.owners())
 for wid,mode,dag in active_dags():
  if wid!=DEFAULT_WORKER:continue
  for r in local_api('GET',f'/api/v2/dags/{dag}/dagRuns',params={'limit':100,'order_by':'-logical_date'})['dag_runs']:
   conf=r.get('conf') or {};raw=conf.get('raw_episode_id') or conf.get('episode_id')
   if raw:started.add(raw)
   stage=None
   if r['state'] in ('queued','running'):
    try:stage=next((t['task_id'] for t in run_tasks(dag,r['dag_run_id']) if t['state'] in ('running','up_for_reschedule','up_for_retry','failed')),None)
    except Exception:pass
   runs.append({'run_id':r['dag_run_id'],'dag':dag,'mode':mode,'episode':conf.get('episode_id'),'raw_episode':raw,'operator':conf.get('operator_id'),
                'sam_prompt':conf.get('sam_prompt'),'state':r['state'],'stage':stage,'started':r.get('start_date') or r.get('queued_at'),'ended':r.get('end_date')})
 snap['runs']=runs;snap['raw_started']=sorted(e for e in started if e and re.fullmatch(r'ep_\d{8}_\d{6}',e))
 history=batch_history();waiting=[]
 for r in mano_ready_runs():
  label,ready=mano_progress(r,history);conf=r['conf']
  if ready:waiting.append({'run_id':r['dag_run_id'],'episode':conf['episode_id'],'raw_episode':conf.get('raw_episode_id'),'operator':conf.get('operator_id'),'sam_prompt':conf.get('sam_prompt'),'status':label})
 snap['mano_waiting']=waiting
 snap['published']=published_rows()
 with fleet_db() as c:tasks=[dict(r) for r in c.execute('SELECT * FROM fleet_tasks ORDER BY updated DESC LIMIT 100')]
 cmds=[]
 for t in tasks:
  p=json.loads(t['payload'])
  cmds.append({'id':t['id'],'type':t['type'],'episode':p.get('episode'),'run_id':p.get('source_run_id') or p.get('run_id'),'source_pc':p.get('source_pc') or (fleet.PC_ID if t['type'] in ('mano_export','mano_release') else None),
               'target_pc':p.get('target_pc') or fleet.PC_ID,'state':t['state'],'step':t['step'],'message':t['message'],'requested_by':p.get('requested_by'),'created':t['created'],'updated':t['updated']})
 snap['commands']=cmds
 snap['assignments']=[{'episode':ep,'operator':op} for ep,op in access.assignments().items()]
 try:snap['jobs']=job_records({r['run_id']:r['operator'] for r in runs if r.get('operator')})
 except Exception as e:print('작업 시간 기록 읽기 실패:',e,flush=True)
 try:snap['frames']=episode_frame_rows({j['episode'] for j in snap.get('jobs') or [] if j.get('episode')})
 except Exception as e:print('단계별 프레임 수 읽기 실패:',e,flush=True)
 return snap

def reconcile_raw_claims(snap):
 """NAS RAW 선점을 이 PC 상태와 맞춥니다.
 - 이 PC의 담당·실행 RAW는 선점(없으면 채움). 배정만 된 RAW를 다른 PC가 먼저 선점했으면 이 PC 배정을 회수.
 - 이 PC가 선점했지만 배정·담당·실행이 모두 없으면 해제(다른 PC가 가져갈 수 있게)."""
 with access._assign_lock:
  started=set(snap.get('raw_started') or []);mine=started|{e for e in access.owners() if fleet.RAW_NAME.fullmatch(e)}
  mine|=my_distributed()|cont_waiting_eps()  # SAM3 연속 모드: 분배받은 RAW·대기열 RAW는 배정 행이 없어도 선점 유지
  for ep in mine:fleet.raw_claim(ep)
  assigned=access.assignments()
  for ep in list(assigned):
   if not fleet.raw_claim(ep):
    with access.db() as c:c.execute('DELETE FROM raw_assignments WHERE episode=?',(ep,))
    del assigned[ep]
  for ep,pc in fleet.raw_claims().items():
   if pc==fleet.PC_ID and ep not in mine and ep not in assigned:fleet.raw_release(ep)

# ---- 강제 업데이트: updater가 빌드를 마치고 stop.json을 쓰면, 이 PC의 진행 중인 작업을 모두 강제 종료합니다 ----
def stop_for_update(note):
 """MANO 일괄 실행·파이프라인 실행(검수·설정 입력 대기는 유지)·그 밖의 GPU 작업(이관 MANO·다시 실행 등)을 강제 종료합니다.
 Airflow 단계는 failed로 두므로 업데이트 뒤 '실패 단계 다시 실행'으로 이어서 할 수 있습니다. → {'stopped':[...],'errors':[...]}"""
 stopped,errors=[],[]
 def attempt(label,fn):
  try:return fn()
  except Exception as e:errors.append(f'{label}: '+str(getattr(e,'detail',None) or e)[-200:])
 with lock:
  for b in attempt('MANO 일괄 실행 조회',lambda:list(airflow_runs(BATCH_DAG,state=['queued','running']))) or []:
   if b['state'] not in ('queued','running'):continue
   attempt('MANO 일괄 실행 '+b['dag_run_id'],lambda:fail_airflow_run(BATCH_DAG,b['dag_run_id'],note))
   for src in b['conf'].get('sources',[]):
    res=attempt(src['conf'].get('episode_id',''),lambda:worker_action('stop',{'episode':src['conf']['episode_id'],'run_id':src['run_id'],'stages':list(MANO_ALL_STAGES)},DEFAULT_WORKER)) or {}
    stopped+=[src['conf']['episode_id']+' '+x for x in res.get('stopped',[])]
   stopped.append('MANO 일괄 실행 '+b['dag_run_id'])
  for wid,dag,r in attempt('실행 조회',active_runs) or []:
   if run_waiting(dag,r):continue        # 검수·설정 입력 대기: 계산 중이 아니므로 그대로 두고 업데이트 뒤 새 코드로 이어갑니다
   ep=(r.get('conf') or {}).get('episode_id','')
   attempt(ep+' 실행',lambda:fail_airflow_run(dag,r['dag_run_id'],note))
   res=attempt(ep+' GPU 작업',lambda:worker_action('stop',{'episode':ep,'run_id':r['dag_run_id']},wid)) or {}
   stopped.append(ep+(' ('+', '.join(res['stopped'])+')' if res.get('stopped') else ''))
   review_queue_cache.pop(wid,None)
  # 남은 GPU 작업(다른 PC에서 이관 받은 MANO, 목록에서 다시 실행한 단계 등): Worker의 대기·실행 중 작업 전체
  for wid in {w for w,_,_ in active_dags()}:
   res=attempt(wid+' 남은 GPU 작업',lambda:worker_action('stop-all',{},wid)) or {}
   stopped+=res.get('stopped',[])
   if res.get('left'):errors.append('종료되지 않은 GPU 작업: '+', '.join(res['left']))
 return {'stopped':list(dict.fromkeys(stopped)),'errors':errors}

def force_update_stop():
 try:req=fleet.force_stop_order()
 except Exception as e:print('강제 업데이트 확인 실패:',e,flush=True);return
 if not req:return
 who=req.get('forced_by') or req.get('requested_by') or '관리자'
 print('강제 업데이트: 진행 중인 작업을 강제 종료합니다 · 요청',who,flush=True)
 result=stop_for_update('관리자 강제 업데이트: '+who)
 with access.db() as c:c.execute('INSERT INTO ui_audit VALUES(?,?,?)',(time.time(),who,'force-update stop '+','.join(result['stopped'])[:900]))
 fleet.force_stop_done(req['id'],result)
 print('강제 업데이트: 종료',len(result['stopped']),'개 · 오류',result['errors'],flush=True)

def fleet_tick():
 with _fleet_lock:
  force_update_stop()
  fleet_process()
  snap=fleet_snapshot()
  try:reconcile_raw_claims(snap)
  except Exception as e:print('RAW 선점 정리 실패:',e,flush=True)
  fleet.publish_status(snap)

def fleet_loop():
 import logging
 log=logging.getLogger('kaist.fleet');wait=2
 while not _fleet_stop.wait(wait):
  wait=FLEET_INTERVAL
  try:fleet_tick()
  except Exception as e:log.warning('NAS 관제 갱신 실패: %s',e)

if os.environ.get('KAIST_AIRFLOW_API_URL') and os.environ.get('KAIST_FLEET','on')!='off' and re.fullmatch(r'pc[1-6]',fleet.PC_ID):
 _base_lifespan=lifespan
 @asynccontextmanager
 async def _fleet_lifespan(app):
  th=threading.Thread(target=fleet_loop,name='kaist-fleet',daemon=True);th.start()
  try:
   async with _base_lifespan(app):yield
  finally:_fleet_stop.set()
 app.router.lifespan_context=_fleet_lifespan

def fleet_view(pc):
 st=dict(pc)
 runs=st.get('runs') or [];today=datetime.now().strftime('%Y-%m-%d')
 st['stats']={'runs':len(runs),'running':st.get('busy_runs',0),'failed':sum(r['state']=='failed' for r in runs),
  'success':sum(r['state']=='success' for r in runs),'mano_waiting':len(st.get('mano_waiting') or []),
  'published':len(st.get('published') or []),'published_today':sum(datetime.fromtimestamp(p['at']).strftime('%Y-%m-%d')==today for p in st.get('published') or [])}
 st['active_runs']=[r for r in runs if r['state'] in ('queued','running')]
 st.pop('runs',None);st.pop('raw_started',None);st.pop('jobs',None)
 return st

def free_raws(started):
 """어느 PC에서도 선점·시작하지 않은 RAW(촬영일 RAW_QUEUE_START 이후)."""
 try:raws=call('GET','/worker/info',worker_id=DEFAULT_WORKER)['episodes']
 except Exception:raws=[]
 own=access.owners()
 return [e for e in raws if re.match(r'ep_(\d{8})_',e) and re.match(r'ep_(\d{8})_',e).group(1)>=RAW_QUEUE_START and e not in started and not own.get(e)]

@app.get('/api/fleet')
def fleet_status(req:Request):
 guard(req);access.admin(req)
 if not fleet.available():raise HTTPException(503,'NAS 관제 폴더를 사용할 수 없습니다: '+str(fleet.CONTROL))
 pcs=fleet.read_all();started=dict(fleet.raw_claims())
 for p in pcs:
  for e in p.get('raw_started') or []:started.setdefault(e,p['pc'])
 raw_free=free_raws(started)
 mano=[dict(m,pc=p['pc']) for p in pcs for m in p.get('mano_waiting') or []]
 commands=sorted((c for p in pcs for c in p.get('commands') or []),key=lambda c:-(c.get('updated') or 0))[:60]
 overlaps=fleet.overlaps(pcs)
 return {'me':fleet.PC_ID,'control':str(fleet.CONTROL),'overlaps':overlaps,'pcs':[fleet_view(p) for p in pcs],'raw_free':raw_free,'mano_waiting':mano,'commands':commands,
         'idle_rule':{'util_below':fleet.IDLE_UTIL,'free_mb_at_least':fleet.IDLE_FREE_MB,'stale_seconds':fleet.STALE_SECONDS}}

@app.get('/api/fleet/pcs')
def fleet_pcs(req:Request):
 """관리자 PC 전환 메뉴용: PC별 연결 여부만 가볍게."""
 guard(req);access.admin(req)
 here=fleet.MACHINE_PC or fleet.PC_ID
 if not fleet.available():return {'me':here,'pcs':[{'pc':here,'online':True}]}
 return {'me':here,'pcs':[{'pc':p['pc'],'online':bool(p.get('online')) or p['pc']==here} for p in fleet.read_all()]}

@app.get('/api/fleet/open')
def fleet_open(req:Request,pc:str=''):
 """관리자 원격 관리: 다른 PC 화면을 관리자로 바로 엽니다(그 PC의 사이트 비밀번호·로그인 없이).
 NAS 1회용 넘김 토큰(60초)을 만들어 대상 PC의 /sso로 보냅니다. 이전 버전 PC도 받을 수 있게 세션 이름은 대상 PC 이름입니다."""
 guard(req);admin=access.admin(req)
 pc=pc.strip().lower()
 if pc not in fleet.PCS:raise HTTPException(400,'PC 이름이 잘못되었습니다.')
 if pc==fleet.MACHINE_PC or not fleet.MACHINE_PC and pc==fleet.PC_ID:return RedirectResponse('/#dashboard',status_code=303)
 if not fleet.available():raise HTTPException(503,'NAS 관제 폴더를 사용할 수 없어 다른 PC로 연결할 수 없습니다.')
 st=fleet.read_pc(pc);url=((st or {}).get('meta') or {}).get('ui_url')
 if not st or not st.get('online') or not url:
  return HTMLResponse(f'<meta charset="utf-8"><p>{pc.upper()} 화면에 연결할 수 없습니다. {pc.upper()}의 UI가 켜져 있고 NAS에 연결되어 있는지 확인하세요.</p>',status_code=409)
 token_value=fleet.handoff_create(pc,pc.upper(),'admin')
 with access.db() as c:c.execute('INSERT INTO ui_audit VALUES(?,?,?)',(time.time(),admin.get('label') or admin['name'],'admin-open '+fleet.PC_ID+'->'+pc))
 return RedirectResponse(url.rstrip('/')+'/sso?t='+quote(token_value),status_code=303)

@app.get('/api/fleet/report')
def fleet_report(req:Request):
 """관리자 통합 관리: PC1~PC6 상태 파일을 모아 단계별 작업 시간 기록·결과 저장·진행 상황을 한 번에 돌려줍니다.
 날짜별 집계는 화면(브라우저 시간대)에서 합니다. NAS를 못 쓰면 이 PC 기록만 보여줍니다."""
 guard(req);access.admin(req)
 if fleet.available():pcs=fleet.read_all()
 else:
  snap=fleet_snapshot();pcs=[dict(snap,pc=fleet.PC_ID,online=True,age=0,local_only=True)]
 started=dict(fleet.raw_claims()) if fleet.available() else {}
 out={'me':fleet.PC_ID,'nas':fleet.available(),'days':JOB_LOG_DAYS,'pcs':[],'jobs':[],'published':[],'runs':[],'mano_waiting':[],'frames':[]}
 for p in pcs:
  for e in p.get('raw_started') or []:started.setdefault(e,p['pc'])
  g=(p.get('gpu') or [None])[0]
  out['pcs'].append({'pc':p['pc'],'online':bool(p.get('online')),'missing':bool(p.get('missing')),'error':p.get('error'),'age':p.get('age'),
   'updated_at':(p.get('meta') or {}).get('updated_at'),'ui_url':(p.get('meta') or {}).get('ui_url'),'has_jobs':bool(p.get('has_jobs') or p.get('local_only')),
   'gpu':g,'assignments':len(p.get('assignments') or []),'queue_target':queue_target_of(p['pc'].upper()) if fleet.available() else None,'default_queue':access.ASSIGN_TARGET,'idle':bool(p.get('idle')),'update':fleet.update_info(p['pc']) if fleet.available() else None})
  for k in ('jobs','published','runs','mano_waiting','frames'):
   out[k]+=[dict(r,pc=p['pc']) for r in p.get(k) or []]
 out['raw_free']=len(free_raws(started))
 out['stage_names']=JOB_STAGES+['adopt']
 raws=raw_frames()
 out['raws']=None if raws is None else [dict(r,pc=started.get(r['episode'])) for r in raws]  # None = NAS RAW 폴더를 아직 읽는 중
 return out

# ---- RAW 프레임 수(관리자 통합 관리 'RAW 프레임' 탭): NAS RAW 폴더의 rgbd_episode.json(capture.frameCounts) ----
RAW_FRAME_NAME=re.compile(r'ep_(\d{8})_(\d{6})')
RAW_FRAME_RESCAN=60      # 초. NAS 폴더 목록은 이 간격으로만 다시 훑고, 바뀐 RAW 폴더만 다시 읽습니다.
_raw_frames_cache=[0.0,None];_raw_frame_rows={}

def _iso_epoch(v):
 try:return datetime.fromisoformat(str(v).replace('Z','+00:00')).timestamp()
 except (TypeError,ValueError):return None

def raw_frame_info(d):
 """RAW 하나의 카메라별 프레임 수. 촬영 앱 기록(rgbd_episode.json)에 없으면 cam_N_timestamps.csv 줄 수(머리글 제외)로 셉니다."""
 meta={}
 try:meta=json.loads((d/'rgbd_episode.json').read_text())
 except (OSError,ValueError):pass
 cap=meta.get('capture') or {};obj=meta.get('object') or {}
 counts={str(k):int(v) for k,v in (cap.get('frameCounts') or {}).items() if isinstance(v,(int,float)) and not isinstance(v,bool)};source='meta'
 if not counts:
  source='csv'
  for f in d.glob('cam_*_timestamps.csv'):
   try:
    with f.open('rb') as fh:counts[f.name[:-len('_timestamps.csv')]]=max(sum(1 for line in fh if line.strip())-1,0)
   except OSError:pass
 cams=[str(c) for c in cap.get('cameras') or []] or sorted(counts,key=lambda c:int(re.sub(r'\D','',c) or 0))
 for c in counts:
  if c not in cams:cams.append(c)
 m=RAW_FRAME_NAME.fullmatch(d.name)
 at=datetime.strptime(m.group(1)+m.group(2),'%Y%m%d%H%M%S').replace(tzinfo=timezone.utc).timestamp()  # 폴더 이름 시각은 UTC
 start,stop=_iso_epoch(cap.get('startedAt')),_iso_epoch(cap.get('stoppedAt'))
 return {'episode':d.name,'at':at,'object':obj.get('name') or obj.get('label') or obj.get('prompt') or (meta.get('task') or {}).get('taskName'),
         'interaction':obj.get('interaction'),'cameras':cams,'frames':{c:counts.get(c) for c in cams},'failed':[str(x) for x in cap.get('failed') or []],
         'duration':stop-start if start and stop and stop>=start else None,'source':source if counts else None}

_raw_frames_lock=threading.Lock()

def raw_frames():
 """마지막으로 읽은 목록을 바로 돌려주고, 오래됐으면 뒤에서 다시 읽습니다(NFS 첫 읽기는 수십 초 걸릴 수 있음).
 아직 한 번도 못 읽었으면 None."""
 if time.monotonic()-_raw_frames_cache[0]>=RAW_FRAME_RESCAN and _raw_frames_lock.acquire(blocking=False):
  def run():
   try:_raw_frames_scan()
   except Exception as e:print('RAW 프레임 수 읽기 실패:',e,flush=True);_raw_frames_cache[0]=time.monotonic()
   finally:_raw_frames_lock.release()
  threading.Thread(target=run,name='kaist-raw-frames',daemon=True).start()
 return _raw_frames_cache[1]

def _raw_frames_scan():
 root=Path(os.environ.get('KAIST_NAS_RAW_ROOT') or Path(os.environ.get('NAS_MOUNT','/mnt/kaist-data'))/'rgbd_raw')
 rows=[]
 try:dirs=[d for d in root.iterdir() if RAW_FRAME_NAME.fullmatch(d.name)] if root.is_dir() else []
 except OSError:dirs=[]
 for d in dirs:
  try:
   if not d.is_dir():continue
   key=d.stat().st_mtime_ns
  except OSError:continue
  hit=_raw_frame_rows.get(d.name)
  if not hit or hit[0]!=key:hit=_raw_frame_rows[d.name]=(key,raw_frame_info(d))
  rows.append(hit[1])
 rows.sort(key=lambda r:r['at'],reverse=True)
 _raw_frames_cache[:]=[time.monotonic(),rows]

class FleetUpdate(BaseModel):
 pcs:list[str]=Field(min_length=1,max_length=6)
 action:str=Field(pattern=r'^(request|force|cancel)$')  # force: 진행 중인 작업을 강제 종료하고 업데이트(걸린 요청도 강제로 전환)

@app.post('/api/fleet/update')
def fleet_update(data:FleetUpdate,req:Request):
 """GitHub 업데이트 요청/취소. 각 PC의 호스트 타이머(repro/update_stack.py)가 쉬는 시점에 받아 반영합니다."""
 guard(req);admin=access.admin(req)
 if not fleet.available():raise HTTPException(409,'NAS에 연결되어 있지 않아 업데이트를 요청할 수 없습니다.')
 pcs=[pc for pc in dict.fromkeys(data.pcs) if pc in fleet.PCS]
 if not pcs:raise HTTPException(400,'PC를 고르세요.')
 by=admin['name']+'@'+fleet.PC_ID;done=[]
 for pc in pcs:
  if data.action=='request':fleet.update_request(pc,by);done.append(pc)
  elif data.action=='force':fleet.update_force(pc,by);done.append(pc)
  elif fleet.update_cancel(pc):done.append(pc)
 with access.db() as c:c.execute('INSERT INTO ui_audit VALUES(?,?,?)',(time.time(),admin['name'],f"fleet update {data.action} {','.join(done)}"))
 return {'ok':True,'pcs':done}

@app.get('/api/fleet/updates')
def fleet_updates(req:Request):
 """업데이트 진행 상황만 가볍게(대시보드가 업데이트 중 5초마다 조회). NAS control/update/<pc>/*.json"""
 guard(req);access.admin(req)
 if not fleet.available():return {'nas':False,'pcs':[]}
 return {'nas':True,'now':time.time(),'pcs':[dict(pc=pc,**fleet.update_info(pc)) for pc in fleet.PCS]}

class FleetDispatch(BaseModel):
 type:str=Field(pattern=r'^(run_raw|mano_transfer|mano_release|queue_reset|resolve_overlaps)$')
 target_pc:str=Field(default='',pattern=r'^(pc[1-6])?$')
 source_pc:str=Field(default='',pattern=r'^(pc[1-6])?$')
 episode:str=Field(default='',pattern=r'^(ep_[A-Za-z0-9_]+)?$')
 run_id:str=Field(default='',max_length=250)
 sam_prompt:str=Field(default='',max_length=200)

@app.post('/api/fleet/dispatch')
def fleet_dispatch(data:FleetDispatch,req:Request):
 guard(req);admin=access.admin(req)
 pcs={p['pc']:p for p in fleet.read_all()}
 by=admin['name']+'@'+fleet.PC_ID
 if data.type=='queue_reset':
  # PC 하나의 작업자 대기열 초기화(시작 전 배정·제외 기록). 실행 기록·결과는 그대로.
  if not data.target_pc:raise HTTPException(400,'초기화할 PC를 고르세요.')
  cmd=fleet.send(data.target_pc,{'type':'raw_reset','target_pc':data.target_pc,'requested_by':by})
  with access.db() as c:c.execute('INSERT INTO ui_audit VALUES(?,?,?)',(time.time(),admin['name'],f"fleet queue_reset {data.target_pc} {cmd['id']}"))
  return {'id':cmd['id'],'sent_to':data.target_pc}
 if data.type=='resolve_overlaps':
  # PC 간 겹치는 RAW마다 남길 PC 하나만 두고, 나머지 PC의 시작 전 배정을 풉니다(양쪽 실행은 수동 확인).
  sent=[];manual=[]
  for o in fleet.overlaps(list(pcs.values())):
   if data.episode and o['episode']!=data.episode:continue
   if o['manual'] or not o['keep']:manual.append(o['episode']);continue
   if o['claim']!=o['keep']:
    # 남기는 PC로 선점을 옮깁니다(선점이 비었거나 풀릴 PC가 갖고 있을 때).
    try:
     if o['claim']:os.unlink(fleet.CONTROL/'claims'/o['episode'])
     os.symlink(o['keep'],fleet.CONTROL/'claims'/o['episode'])
    except OSError:pass
   for pc in o['release']:
    fleet.send(pc,{'type':'raw_reset','episode':o['episode'],'target_pc':pc,'requested_by':by});sent.append(pc+':'+o['episode'])
  with access.db() as c:c.execute('INSERT INTO ui_audit VALUES(?,?,?)',(time.time(),admin['name'],'fleet resolve_overlaps '+','.join(sent)))
  return {'sent':sent,'manual':manual,'sent_to':', '.join(sorted({x.split(':')[0] for x in sent})) or '없음'}
 if not data.episode:raise HTTPException(400,'에피소드를 고르세요.')
 if data.type=='run_raw':
  if not data.target_pc or not data.sam_prompt.strip():raise HTTPException(400,'대상 PC와 SAM3 분할 대상을 입력하세요.')
  if not pcs[data.target_pc].get('online'):raise HTTPException(409,data.target_pc+'가 연결되어 있지 않습니다.')
  where=fleet.raw_owner(data.episode) or next((p for p,s in pcs.items() if data.episode in (s.get('raw_started') or [])),None)
  if where and where!=data.target_pc:raise HTTPException(409,f'이 RAW는 이미 {where.upper()}에서 작업 중입니다.')
  cmd=fleet.send(data.target_pc,{'type':'run_raw','episode':data.episode,'sam_prompt':data.sam_prompt.strip(),'target_pc':data.target_pc,'requested_by':by})
 elif data.type=='mano_transfer':
  if not data.source_pc or not data.target_pc or data.source_pc==data.target_pc:raise HTTPException(400,'원본 PC와 대상 PC를 다르게 고르세요.')
  if not pcs[data.target_pc].get('online') or not pcs[data.source_pc].get('online'):raise HTTPException(409,'원본·대상 PC가 모두 연결되어 있어야 합니다.')
  if not any(m['run_id']==data.run_id and m['episode']==data.episode for m in pcs[data.source_pc].get('mano_waiting') or []):raise HTTPException(409,'원본 PC의 MANO 대기 목록에 없습니다. 새로고침하세요.')
  cmd=fleet.send(data.source_pc,{'type':'mano_export','episode':data.episode,'run_id':data.run_id,'source_pc':data.source_pc,'target_pc':data.target_pc,'requested_by':by})
 else:
  if not data.source_pc or not data.run_id:raise HTTPException(400,'원본 PC와 실행을 확인하세요.')
  cmd=fleet.send(data.source_pc,{'type':'mano_release','episode':data.episode,'run_id':data.run_id,'source_pc':data.source_pc,'requested_by':by})
 with access.db() as c:c.execute('INSERT INTO ui_audit VALUES(?,?,?)',(time.time(),admin['name'],f"fleet {data.type} {data.episode} {data.source_pc}->{data.target_pc} {cmd['id']}"))
 return {'id':cmd['id'],'sent_to':data.target_pc if data.type=='run_raw' else data.source_pc}

@app.get('/assets/dashboard.js')
def dashboard_script(req:Request):
 guard(req)
 return FileResponse(Path(__file__).parent/'dashboard.js',media_type='application/javascript',headers={'Cache-Control':'no-store'})

@app.get('/assets/fleet.js')
def fleet_script(req:Request):
 guard(req)
 return FileResponse(Path(__file__).parent/'fleet.js',media_type='application/javascript',headers={'Cache-Control':'no-store'})


# ================= RAW 전처리 일괄 실행 → SAM3 설정 대기 → 분할 대상 입력 후 이어서 실행 =================
class BatchPrep(BaseModel):
 worker_id:str=DEFAULT_WORKER
 episodes:list[str]=Field(min_length=1,max_length=30)
 defer_mano:bool=False

@app.post('/api/run/batch-prep')
def batch_prep(data:BatchPrep,req:Request):
 """선택한 RAW마다 실행을 시작하되 분할 대상 없이 시작해, RAW 전처리 후 'SAM3 설정 대기'에서 멈추게 합니다."""
 guard(req)
 started=[];failed=[]
 for ep in dict.fromkeys(data.episodes):
  if not re.fullmatch(r'ep_[A-Za-z0-9_]+',ep):failed.append({'episode':ep,'error':'에피소드 이름이 잘못되었습니다.'});continue
  try:
   r=launch(Launch(worker_id=data.worker_id,mode='pipeline',episode_id=ep,prep=True,defer_mano=data.defer_mano),req)
   started.append({'raw':ep,'episode':r['episode'],'run_id':r['run_id'],'dag_id':r['dag_id']})
  except HTTPException as e:failed.append({'episode':ep,'error':str(e.detail)})
 access.audit(req,'batch-prep '+','.join(x['raw'] for x in started))
 return {'started':started,'failed':failed}

@app.get('/api/run/sam-waiting')
def sam_waiting(req:Request,worker_id:str=DEFAULT_WORKER):
 """내 실행 중 RAW 전처리가 끝나 'SAM3 설정 대기'인 것"""
 guard(req);out=[]
 for wid,mode,dag in active_dags():
  if wid!=worker_id or mode=='raw':continue
  for r in local_api('GET',f'/api/v2/dags/{dag}/dagRuns',params={'limit':100,'order_by':'-logical_date','state':['running','queued']})['dag_runs']:
   conf=r.get('conf') or {};ep=conf.get('episode_id','')
   if conf.get('sam_settings_confirmed') or not access.allowed(req,ep):continue
   try:stage,state=current_stage(dag,r['dag_run_id'])
   except Exception:continue
   if stage!='sam_settings_ready':continue
   prepared=None
   try:prepared=call('POST','/worker/jobs/status',json=request_for('prepare_raw',r['dag_run_id'],conf),worker_id=wid,timeout=15).get('state')=='success'
   except Exception:pass
   out.append({'dag_id':dag,'run_id':r['dag_run_id'],'episode':ep,'raw_episode':conf.get('raw_episode_id') or ep,
               'operator':conf.get('operator_id'),'state':state,'prepared':prepared,'queued_at':r.get('queued_at')})
 return {'runs':out}

class SamSettings(BaseModel):
 dag_id:str
 run_id:str=Field(min_length=1,max_length=250)
 sam_prompt:str=Field(min_length=1,max_length=200)

@app.post('/api/run/sam-settings')
def save_sam_settings(data:SamSettings,req:Request):
 """'SAM3 설정 대기' 실행에 분할 대상을 저장하고 바로 다음 확인을 깨워 SAM3를 이어서 시작합니다."""
 guard(req)
 wid=dag_worker(data.dag_id)
 if data.dag_id not in [d for w,m,d in active_dags()]:raise HTTPException(400,'실행을 확인하세요.')
 run=local_api('GET',f'/api/v2/dags/{data.dag_id}/dagRuns/'+quote(data.run_id,safe=''));conf=run.get('conf') or {};ep=conf.get('episode_id','')
 owner=access.owners().get(ep) or conf.get('operator_id')
 if access.user(req)['role']!='admin' and owner!=access.user(req)['name']:raise HTTPException(403,'본인 실행만 설정할 수 있습니다.')
 if run['state'] not in ('running','queued'):raise HTTPException(409,'진행 중인 실행이 아닙니다.')
 stage,_=current_stage(data.dag_id,data.run_id)
 if stage!='sam_settings_ready':raise HTTPException(409,'SAM3 설정 대기 단계가 아닙니다. 새로고침하세요.')
 if call('POST','/worker/jobs/status',json=request_for('prepare_raw',data.run_id,conf),worker_id=wid,timeout=15).get('state')!='success':
  raise HTTPException(409,'RAW 전처리가 아직 끝나지 않았습니다. 끝난 뒤 입력하세요.')
 res=worker_action('sam-settings',{'episode':ep,'prompt':data.sam_prompt.strip()},wid)
 try:nudge_gate(wid,ep,'sam_settings_ready')
 except Exception as e:print('SAM3 설정 대기 깨우기 실패:',e,flush=True)
 access.audit(req,'sam-settings '+ep+' '+data.sam_prompt.strip())
 return {'ok':True,'episode':ep,'settings':res.get('settings')}

# ==== SAM3 연속 모드 ====
# 작업자가 분배·배정받은 RAW마다 첫 프레임 4장(카메라 1~4)에 포인트(카메라당 최대 20개)와 분할 대상 텍스트를 넣고 '완료'하면
# 대기열(sam_queue)에 쌓이고, 아래 스레드가 완료한 순서대로 하나씩 실행합니다. 앞 실행의 RAW 전처리~SAM3가 끝나야 다음을 시작하며,
# PC당 동시 실행 한도(MAX_PARALLEL_RUNS)는 적용하지 않습니다. SAM3 뒤는 일반 실행과 같습니다(마스크 검수 대기).
# 분배: 관리자가 남은 RAW 전체를 고른 PC(=작업자, PC 이름으로 로그인)에 균등 분배 → NAS control/distributed + claims.
CONT_BUSY_STAGES=('prepare_raw','sam_settings_ready','sam3')
CONT_TICK_SECONDS=10
_cont_lock=threading.Lock();_cont_stop=threading.Event()

def cont_db():
 c=access.db()
 c.execute('CREATE TABLE IF NOT EXISTS sam_queue (id INTEGER PRIMARY KEY AUTOINCREMENT,raw_episode TEXT NOT NULL,operator TEXT NOT NULL,worker_id TEXT NOT NULL,'
           'sam_prompt TEXT NOT NULL,points TEXT NOT NULL,defer_mano INTEGER NOT NULL DEFAULT 0,created REAL NOT NULL,state TEXT NOT NULL,'
           'run_id TEXT,dag_id TEXT,episode TEXT,error TEXT,started REAL)')
 return c

def my_distributed():
 """이 PC에 분배된 RAW"""
 try:return {ep for ep,pc in fleet.raw_distributed().items() if pc==fleet.PC_ID} if fleet.available() else set()
 except Exception:return set()

def cont_waiting_eps():
 with cont_db() as c:return {r[0] for r in c.execute("SELECT raw_episode FROM sam_queue WHERE state='waiting'")}

def raw_startable(req,ep,wid):
 """자동 배정·담당(access.selectable) 또는 이 PC에 연속 모드로 분배된 RAW"""
 if access.selectable(req,ep,wid):return True
 u=access.user(req)
 return ep in my_distributed() and ep not in access.excluded_raw(u['name'],wid) and access.owners().get(ep) in (None,u['name'])

def raw_object(ep,cache=None):
 """RAW 촬영 기록(rgbd_episode.json)의 물체 이름 → SAM3 분할 대상 기본값"""
 rows=cache if cache is not None else {r['episode']:r for r in (raw_frames() or [])}
 if ep in rows:return rows[ep].get('object')
 root=Path(os.environ.get('KAIST_NAS_RAW_ROOT') or Path(os.environ.get('NAS_MOUNT','/mnt/kaist-data'))/'rgbd_raw')
 try:meta=json.loads((root/ep/'rgbd_episode.json').read_text())
 except (OSError,ValueError):return None
 obj=meta.get('object') or {}
 return obj.get('name') or obj.get('label') or obj.get('prompt') or (meta.get('task') or {}).get('taskName')

def cont_busy(c):
 """연속 모드로 시작한 실행 중 RAW 전처리~SAM3 단계에 있는 것이 있으면 True(확인할 수 없어도 기다림)."""
 for r in c.execute("SELECT run_id,dag_id FROM sam_queue WHERE state='started' AND started>?",(time.time()-2*86400,)).fetchall():
  try:
   run=local_api('GET',f"/api/v2/dags/{r['dag_id']}/dagRuns/"+quote(r['run_id'],safe=''))
   if run['state'] not in ('queued','running'):continue
   stage,state=current_stage(r['dag_id'],r['run_id'])
  except Exception:return True
  if stage in CONT_BUSY_STAGES and state not in ('failed','upstream_failed'):return True
 return False

def cont_fail(row_id,msg):
 with cont_db() as c:c.execute("UPDATE sam_queue SET state='failed',error=? WHERE id=?",(str(msg)[:500],row_id))

def cont_tick():
 if not _cont_lock.acquire(blocking=False):return
 try:
  with cont_db() as c:
   nxt=c.execute("SELECT * FROM sam_queue WHERE state='waiting' AND worker_id=? ORDER BY created,id LIMIT 1",(DEFAULT_WORKER,)).fetchone()
   if not nxt or cont_busy(c):return
  try:update_block()
  except HTTPException:return   # 업데이트 대기 중: 끝나면 이어서 시작
  ep,op=nxt['raw_episode'],nxt['operator']
  own=access.owners().get(ep)
  if own:return cont_fail(nxt['id'],f'이미 실행을 시작한 RAW입니다({own}). 같은 RAW를 두 번 실행하지 않습니다.')
  if fleet.available():
   if not (own==op or ep in my_distributed() or access.assignments().get(ep)==op):return cont_fail(nxt['id'],'분배·배정이 회수된 RAW입니다.')
   where=fleet.raw_taken_elsewhere().get(ep)
   if where:return cont_fail(nxt['id'],f'이 RAW는 {where.upper()}에서 작업 중입니다.')
  # 포인트를 찍은 작업자 이름의 관리자 세션으로 일반 실행과 같은 검사를 거쳐 시작합니다(동시 실행 한도만 제외).
  tok=access.login(op,'admin')
  try:
   res=_launch(Launch(mode='pipeline',worker_id=nxt['worker_id'],episode_id=ep,sam_prompt=nxt['sam_prompt'],confirmed=True,preview_episode=ep,
                      defer_mano=bool(nxt['defer_mano']),sam_object_mode='point_track',sam_object_points=json.loads(nxt['points'])),
               internal_request(tok),bypass_limit=True)
  except HTTPException as e:
   if e.status_code==503:return
   return cont_fail(nxt['id'],e.detail)
  except Exception as e:
   print('연속 모드 실행 시작 실패(다음 확인 때 다시 시도):',e,flush=True);return
  finally:
   with access.db() as c:c.execute('DELETE FROM ui_sessions WHERE token=?',(tok,))
  with cont_db() as c:
   c.execute("UPDATE sam_queue SET state='started',run_id=?,dag_id=?,episode=?,started=?,error=NULL WHERE id=?",(res['run_id'],res['dag_id'],res['episode'],time.time(),nxt['id']))
   c.execute('INSERT INTO ui_audit VALUES(?,?,?)',(time.time(),op,f"continuous start {ep} {res['run_id']}"))
 finally:_cont_lock.release()

def cont_kick():threading.Thread(target=cont_tick,name='kaist-cont-kick',daemon=True).start()

def cont_loop():
 while not _cont_stop.wait(CONT_TICK_SECONDS):
  try:cont_tick()
  except Exception as e:print('연속 모드 대기열 확인 실패:',e,flush=True)

@app.get('/api/continuous')
def cont_status(req:Request):
 guard(req);u=access.user(req);wid=selected_worker(req)
 if wid!=DEFAULT_WORKER:return {'available':False}
 me=u['name'];own=access.owners();assigned=access.assignments();dist=my_distributed();excluded=access.excluded_raw(me,wid)
 with cont_db() as c:
  rows=[dict(r) for r in c.execute("SELECT id,raw_episode,operator,sam_prompt,state,run_id,dag_id,episode,error,created,started FROM sam_queue "
                                  "WHERE worker_id=? AND (state IN ('waiting','failed') OR (state='started' AND started>?)) ORDER BY created,id",(wid,time.time()-86400))]
 active={r['raw_episode'] for r in rows if r['state'] in ('waiting','failed')}
 try:raws=set(call('GET','/worker/info',worker_id=wid)['episodes'])
 except Exception:raws=None
 others=fleet.raw_taken_elsewhere() if fleet.available() else {}
 cand=dist|{ep for ep,op in assigned.items() if op==me}
 todo=sorted(ep for ep in cand if not own.get(ep) and ep not in active and ep not in excluded and ep not in others and (raws is None or ep in raws))
 cache={r['episode']:r for r in (raw_frames() or [])}
 n=0;shown=0
 for r in rows:
  if r['state']=='waiting':n+=1;r['position']=n
  r['mine']=r['operator']==me or u['role']=='admin'
 # 오늘 시작한 실행은 최근 8개만 현재 단계를 붙입니다(Airflow 조회 수 제한).
 for r in reversed(rows):
  if r['state']!='started' or shown>=8:continue
  shown+=1
  try:
   run=local_api('GET',f"/api/v2/dags/{r['dag_id']}/dagRuns/"+quote(r['run_id'],safe=''))
   if run['state']=='success':r['stage']='완료'
   elif run['state']=='failed':r['stage']='실패 · 작업 현황 확인'
   else:
    stage,state=current_stage(r['dag_id'],r['run_id'])
    r['stage']=(JOB_STAGE_LABELS.get(stage,stage or '실행 대기'))+(' · 검수 대기' if stage in WAIT_STAGES[1:] else ' · 실패' if state=='failed' else '')
  except Exception:r['stage']=None
 return {'available':True,'me':me,'todo':[{'episode':ep,'object':raw_object(ep,cache) if i<1 or ep in cache else None} for i,ep in enumerate(todo)],
         'queue':rows,'distributed':len(dist),'busy_stages':list(CONT_BUSY_STAGES)}

JOB_STAGE_LABELS={'prepare_raw':'RAW 전처리','sam_settings_ready':'분할 설정 확인','sam3':'SAM3','mask_approved':'마스크 검수','preflight':'추적 사전 검사','track':'추적',
                  'tracking_approved':'추적 검수','prepare_reconstruction':'복원 준비','reconstruct':'3D 복원','hands':'MANO 피팅','validate':'결과 검사','publish':'최종 저장'}

@app.get('/api/continuous/object')
def cont_object(req:Request,episode:str):
 guard(req)
 if not re.fullmatch(r'ep_[A-Za-z0-9_]+',episode):raise HTTPException(400,'에피소드 이름이 잘못되었습니다.')
 return {'episode':episode,'object':raw_object(episode)}

class ContSubmit(BaseModel):
 episode:str=Field(pattern=r'^ep_[A-Za-z0-9_]+$')
 sam_prompt:str=Field(min_length=1,max_length=200)
 points:dict
 defer_mano:bool=False

@app.post('/api/continuous/submit')
def cont_submit(data:ContSubmit,req:Request):
 guard(req);u=access.user(req);wid=DEFAULT_WORKER;ep=data.episode
 points=object_points(data.points)
 if data.defer_mano and wid!=DEFAULT_WORKER:raise HTTPException(400,'MANO 나중에 실행은 이 PC에서만 선택할 수 있습니다.')
 if access.owners().get(ep):raise HTTPException(409,'이미 실행을 시작한 RAW입니다.')
 if not raw_startable(req,ep,wid):raise HTTPException(403,'나에게 분배·배정된 RAW가 아닙니다. 목록을 새로고침하세요.')
 if fleet.available():
  where=fleet.raw_taken_elsewhere().get(ep)
  if where:raise HTTPException(409,f'이 RAW는 {where.upper()}에서 작업 중입니다.')
  if not fleet.raw_claim(ep):raise HTTPException(409,'다른 PC가 먼저 가져간 RAW입니다.')
 with cont_db() as c:
  c.execute('BEGIN IMMEDIATE')
  if c.execute("SELECT 1 FROM sam_queue WHERE raw_episode=? AND state IN ('waiting','failed')",(ep,)).fetchone():raise HTTPException(409,'이미 연속 모드 대기열에 있는 RAW입니다.')
  c.execute("INSERT INTO sam_queue(raw_episode,operator,worker_id,sam_prompt,points,defer_mano,created,state) VALUES(?,?,?,?,?,?,?,'waiting')",
            (ep,u['name'],wid,data.sam_prompt.strip(),json.dumps(points),int(data.defer_mano),time.time()))
 access.audit(req,'continuous submit '+ep+' '+data.sam_prompt.strip())
 cont_kick()
 return {'ok':True}

class ContItem(BaseModel):
 id:int

def _cont_row(req,row_id,states):
 u=access.user(req)
 with cont_db() as c:r=c.execute('SELECT * FROM sam_queue WHERE id=?',(row_id,)).fetchone()
 if not r or r['state'] not in states:raise HTTPException(409,'대기열 상태가 바뀌었습니다. 새로고침하세요.')
 if u['role']!='admin' and r['operator']!=u['name']:raise HTTPException(403,'본인 항목만 바꿀 수 있습니다.')
 return r

@app.post('/api/continuous/cancel')
def cont_cancel(data:ContItem,req:Request):
 """실행 전(대기·실패) 항목을 빼면 그 RAW는 다시 '포인트 찍기'로 돌아갑니다."""
 guard(req)
 with _cont_lock:
  r=_cont_row(req,data.id,('waiting','failed'))
  with cont_db() as c:c.execute("UPDATE sam_queue SET state='cancelled' WHERE id=?",(r['id'],))
 access.audit(req,'continuous cancel '+r['raw_episode'])
 return {'ok':True,'episode':r['raw_episode']}

@app.post('/api/continuous/retry')
def cont_retry(data:ContItem,req:Request):
 guard(req)
 r=_cont_row(req,data.id,('failed',))
 with cont_db() as c:c.execute("UPDATE sam_queue SET state='waiting',error=NULL,created=? WHERE id=?",(time.time(),r['id']))
 access.audit(req,'continuous retry '+r['raw_episode']);cont_kick()
 return {'ok':True}

# ---- 관리자: 남은 RAW 균등 분배 ----
def cont_started_all(pcs):
 return set(access.owners())|{e for p in pcs for e in (p.get('raw_started') or [])}

def cont_pool(started,dist,date=''):
 """어느 PC도 선점·시작·제외·분배하지 않은 RAW(촬영일 RAW_QUEUE_START 이후, date='YYYY-MM-DD'면 그날 촬영만)"""
 try:raws=call('GET','/worker/info',worker_id=DEFAULT_WORKER)['episodes']
 except Exception as e:raise HTTPException(503,'RAW 목록을 불러오지 못했습니다.') from e
 claims=fleet.raw_claims();excl=global_excluded();day=date.replace('-','')
 out=[]
 for ep in raws:
  m=re.match(r'ep_(\d{8})_',ep)
  if not m or m.group(1)<RAW_QUEUE_START or (day and m.group(1)!=day):continue
  if ep in started or ep in claims or ep in excl or ep in dist:continue
  out.append(ep)
 return sorted(out)

@app.get('/api/continuous/distribution')
def cont_distribution(req:Request):
 guard(req);access.admin(req)
 if not fleet.available():return {'available':False}
 pcs=fleet.read_all();dist=fleet.raw_distributed();started=cont_started_all(pcs)
 per={p['pc']:{'pc':p['pc'],'online':bool(p.get('online')),'waiting':0,'started':0} for p in pcs}
 for ep,pc in dist.items():
  if pc in per:per[pc]['started' if ep in started else 'waiting']+=1
 pool=cont_pool(started,dist);dates={}
 for ep in pool:d=ep[3:7]+'-'+ep[7:9]+'-'+ep[9:11];dates[d]=dates.get(d,0)+1
 return {'available':True,'pcs':list(per.values()),'pool':len(pool),'dates':dict(sorted(dates.items(),reverse=True))}

class ContDistribute(BaseModel):
 pcs:list[str]=Field(min_length=1,max_length=6)
 date:str=Field(default='',pattern=r'^(\d{4}-\d{2}-\d{2})?$')

@app.post('/api/continuous/distribute')
def cont_distribute(data:ContDistribute,req:Request):
 """남은 RAW를 고른 PC들에 오래된 촬영부터 하나씩, 시작 전 분배가 가장 적은 PC에 줍니다(PC별 개수가 같아지도록)."""
 guard(req);admin=access.admin(req)
 if not fleet.available():raise HTTPException(503,'NAS 관제를 사용할 수 없어 분배할 수 없습니다.')
 targets=[pc for pc in fleet.PCS if pc in set(data.pcs)]
 if not targets:raise HTTPException(400,'분배할 PC를 고르세요.')
 with _cont_lock:
  pcs=fleet.read_all();dist=fleet.raw_distributed();started=cont_started_all(pcs)
  load={pc:0 for pc in targets}
  for ep,pc in dist.items():
   if pc in load and ep not in started:load[pc]+=1
  given={pc:[] for pc in targets}
  for ep in cont_pool(started,dist,data.date):
   pc=min(targets,key=lambda p:(load[p],targets.index(p)))
   if fleet.raw_distribute(ep,pc):load[pc]+=1;given[pc].append(ep)
 with access.db() as c:c.execute('INSERT INTO ui_audit VALUES(?,?,?)',(time.time(),admin['name'],'continuous distribute '+json.dumps({p:len(v) for p,v in given.items()})))
 return {'given':{p:len(v) for p,v in given.items()},'total':sum(len(v) for v in given.values()),'load':load}

class ContRecall(BaseModel):
 pc:str=Field(pattern=r'^pc[1-6]$')

@app.post('/api/continuous/recall')
def cont_recall(data:ContRecall,req:Request):
 """PC 하나에 분배한 RAW 중 아직 시작하지 않은 것을 회수합니다(그 PC 대기열에 있던 항목은 시작 때 '회수됨'으로 실패 처리)."""
 guard(req);admin=access.admin(req)
 if not fleet.available():raise HTTPException(503,'NAS 관제를 사용할 수 없습니다.')
 started=cont_started_all(fleet.read_all());n=0
 for ep,pc in fleet.raw_distributed().items():
  if pc==data.pc and ep not in started:fleet.raw_undistribute(ep,pc);n+=1
 with access.db() as c:c.execute('INSERT INTO ui_audit VALUES(?,?,?)',(time.time(),admin['name'],f'continuous recall {data.pc} {n}'))
 return {'recalled':n}

@app.get('/assets/continuous.js')
def continuous_script(req:Request):
 guard(req)
 return FileResponse(Path(__file__).parent/'continuous.js',media_type='application/javascript',headers={'Cache-Control':'no-store'})
