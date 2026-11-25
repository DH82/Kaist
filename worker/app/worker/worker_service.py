"""Authenticated mask review, mounted inside the existing Airflow API server."""
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, Response, FileResponse
from typing import Annotated
from fastapi import Header

WORKER_ROOT=Path(__file__).resolve().parent
SECRET=(WORKER_ROOT/'secret').read_text().strip()
class Operator:
    def get_name(self):return 'admin'
def get_user(authorization: str=Header(default='')):
    if not secrets.compare_digest(authorization,'Bearer '+SECRET):raise HTTPException(401,'Worker authentication required')
    return Operator()
GetUserDep=Annotated[Operator,Depends(get_user)]
def requires_access_dag(method):
    def check(dag_id: str,user:GetUserDep):
        config_for(dag_id)
    return check


# Each PC keeps its own local config, credentials, jobs and GPU lock.
ROOT = WORKER_ROOT.parent
sys.path.insert(0, str(ROOT / "dags"))
from kaist_runtime import check_project_mount
app = FastAPI(dependencies=[Depends(get_user)])
PREFIX = '/kaist-review'


def config_for(dag_id):
    resource = dag_id.removeprefix('kaist_revising_')
    if dag_id != 'kaist_revising_'+resource or resource not in ('gpu_4090_a','gpu_5090','gpu_4090_b'):
        raise HTTPException(404, 'Unknown pipeline')
    config = Path(os.environ['KAIST_AIRFLOW_CONFIG_DIR'])/(resource+'.json')
    if not config.exists():raise HTTPException(404, 'Worker config missing')
    try:
        check_project_mount(json.loads(config.read_text()))
    except RuntimeError as error:
        raise HTTPException(503, str(error)) from error
    return config


def backend(dag_id, episode, action, extra=(), data=None):
    if not re.fullmatch(r'ep_[A-Za-z0-9_]+',episode):raise HTTPException(400,'Invalid episode')
    config = config_for(dag_id);cfg=json.loads(config.read_text())
    cmd=[cfg['control_python'],str(ROOT/'review/review_backend.py'),action,
         '--config',str(config),'--episode',episode,*extra]
    result=subprocess.run(cmd,input=json.dumps(data).encode() if data is not None else None,
                          capture_output=True,timeout=120)
    if result.returncode:raise HTTPException(409,result.stderr.decode(errors='replace')[-1500:])
    return result.stdout if action=='image' else json.loads(result.stdout)


@app.get('/',response_class=HTMLResponse)
def home(request: Request, user: GetUserDep):
    view=request.query_params.get('view', '')
    if not view:
        view='tracking' if request.query_params.get('task_id')=='tracking_approved' else 'mask' if request.query_params.get('task_id')=='mask_approved' else 'queue'
    name={'queue':'queue.html','mask':'index.html','tracking':'tracking.html'}.get(view)
    if name is None:raise HTTPException(400,'Unknown review view')
    response=HTMLResponse((ROOT/'review'/name).read_text())
    token=request.cookies.get('kaist_review_csrf') or secrets.token_urlsafe(32)
    response.set_cookie('kaist_review_csrf',token,httponly=True,samesite='strict',path=PREFIX)
    response.headers['Cache-Control']='no-store'
    response.headers['Content-Security-Policy']="default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' blob:; frame-ancestors 'self'"
    # Token is readable only in this same-origin HTML, not by another site.
    response.body=response.body.replace(b'__CSRF_TOKEN__',token.encode())
    response.headers['content-length']=str(len(response.body))
    return response


@app.get('/dags/{dag_id}/episodes/{episode}', dependencies=[Depends(requires_access_dag(method='GET'))])
def details(dag_id: str, episode: str, user: GetUserDep):
    data=backend(dag_id,episode,'inspect');data['reviewer']=user.get_name()
    return data


@app.get('/dags/{dag_id}/episodes/{episode}/image',dependencies=[Depends(requires_access_dag(method='GET'))])
def frame_image(dag_id: str, episode: str, cam: int, frame: int, mode: str='overlay'):
    value=backend(dag_id,episode,'image', ['--cam',str(cam),'--frame',str(frame),'--mode',mode])
    return Response(value,media_type='image/jpeg',headers={'Cache-Control':'no-store'})


@app.post('/dags/{dag_id}/episodes/{episode}/decision',dependencies=[Depends(requires_access_dag(method='POST'))])
async def decision(dag_id: str, episode: str, request: Request, user: GetUserDep):
    origin=request.headers.get('origin','')
    if urlsplit(origin).netloc != request.headers.get('host'):
        raise HTTPException(403,'Same-origin approval required')
    cookie=request.cookies.get('kaist_review_csrf','');header=request.headers.get('x-csrf-token','')
    if not cookie or not secrets.compare_digest(cookie,header):raise HTTPException(403,'Invalid CSRF token')
    raw=await request.body()
    if len(raw)>8192:raise HTTPException(413,'Request too large')
    try:data=json.loads(raw)
    except ValueError:raise HTTPException(400,'Invalid JSON')
    if not isinstance(data,dict) or data.get('decision') not in ('approve','hold'):
        raise HTTPException(400,'Invalid decision')
    data['operator']=user.get_name()
    return backend(dag_id,episode,'decision',data=data)


@app.get('/dags/{dag_id}/queue', dependencies=[Depends(requires_access_dag(method='GET'))])
def review_queue(dag_id: str, user: GetUserDep):
    return backend(dag_id, 'ep_queue', 'queue')


@app.get('/dags/{dag_id}/episodes/{episode}/tracking', dependencies=[Depends(requires_access_dag(method='GET'))])
def tracking_details(dag_id: str, episode: str, user: GetUserDep):
    data=backend(dag_id,episode,'tracking_inspect');data['reviewer']=user.get_name()
    return data


@app.get('/dags/{dag_id}/episodes/{episode}/tracking/video/{group}', dependencies=[Depends(requires_access_dag(method='GET'))])
def tracking_media(dag_id: str, episode: str, group: str):
    value=backend(dag_id,episode,'tracking_video', ['--group',group])
    return FileResponse(value['path'],media_type='video/mp4',headers={'Cache-Control':'no-store'})


@app.post('/dags/{dag_id}/episodes/{episode}/tracking/decision', dependencies=[Depends(requires_access_dag(method='POST'))])
async def tracking_decision(dag_id: str, episode: str, request: Request, user: GetUserDep):
    origin=request.headers.get('origin','')
    if urlsplit(origin).netloc != request.headers.get('host'):
        raise HTTPException(403,'Same-origin approval required')
    cookie=request.cookies.get('kaist_review_csrf','');header=request.headers.get('x-csrf-token','')
    if not cookie or not secrets.compare_digest(cookie,header):raise HTTPException(403,'Invalid CSRF token')
    raw=await request.body()
    if len(raw)>8192:raise HTTPException(413,'Request too large')
    try:data=json.loads(raw)
    except ValueError:raise HTTPException(400,'Invalid JSON')
    if not isinstance(data,dict) or data.get('decision') not in ('approve','hold'):
        raise HTTPException(400,'Invalid decision')
    data['operator']=user.get_name()
    return backend(dag_id,episode,'tracking_decision',data=data)



def edit_backend(dag_id,episode,action,extra=(),data=None,timeout=120):
    if not re.fullmatch(r'ep_[A-Za-z0-9_]+',episode):raise HTTPException(400,'Invalid episode')
    config=config_for(dag_id);cfg=json.loads(config.read_text())
    cmd=[cfg['control_python'],str(WORKER_ROOT/'mask_editor.py'),action,'--config',str(config),'--episode',episode,*extra]
    result=subprocess.run(cmd,input=json.dumps(data).encode() if data is not None else None,capture_output=True,timeout=timeout)
    if result.returncode:raise HTTPException(409,result.stderr.decode(errors='replace')[-1500:])
    return json.loads(result.stdout)

@app.get('/dags/{dag_id}/episodes/{episode}/edit',dependencies=[Depends(requires_access_dag(method='GET'))])
def edit_frame(dag_id:str,episode:str,cam:int,obj:int,frame:int):
    return edit_backend(dag_id,episode,'frame',['--cam',str(cam),'--obj',str(obj),'--frame',str(frame)])

@app.post('/dags/{dag_id}/episodes/{episode}/edit',dependencies=[Depends(requires_access_dag(method='POST'))])
async def edit_save(dag_id:str,episode:str,request:Request,user:GetUserDep):
    if urlsplit(request.headers.get('origin','')).netloc!=request.headers.get('host'):raise HTTPException(403,'Same-origin editing required')
    cookie=request.cookies.get('kaist_review_csrf','');header=request.headers.get('x-csrf-token','')
    if not cookie or not secrets.compare_digest(cookie,header):raise HTTPException(403,'Invalid CSRF token')
    raw=await request.body()
    if len(raw)>2600000:raise HTTPException(413,'Mask payload too large')
    try:data=json.loads(raw)
    except ValueError:raise HTTPException(400,'Invalid JSON')
    if not isinstance(data,dict):raise HTTPException(400,'Invalid mask request')
    data['operator']=user.get_name()
    return edit_backend(dag_id,episode,'save',data=data)

# ---- SAM3 마법봉 ----
# sam-env에서 SAM3.1을 상주시키는 하위 프로세스(sam_wand_server.py)를 필요할 때만 띄웁니다.
# 남은 GPU 메모리가 WAND_MIN_FREE_MB 이상이면 다른 GPU 작업 중에도 띄우며, WAND_IDLE초 동안
# 쓰지 않으면 모델을 내려 GPU 메모리를 파이프라인 작업에 돌려줍니다.
import fcntl,shlex,threading,time,uuid
WAND_IDLE=int(os.environ.get('KAIST_WAND_IDLE','180'))
# 마법봉(SAM3.1)을 띄우기 위해 남아 있어야 하는 GPU 메모리(MiB). KAIST_WAND_MIN_FREE_MB로 조정합니다.
WAND_MIN_FREE_MB=int(os.environ.get('KAIST_WAND_MIN_FREE_MB','8192'))
WAND_WANT_SECONDS=600
def gpu_free_mb(uuid=None):
    try:
        out=subprocess.check_output(['nvidia-smi','--query-gpu=uuid,memory.free','--format=csv,noheader,nounits'],text=True,timeout=10)
    except (OSError,subprocess.SubprocessError):return None
    rows=[[v.strip() for v in line.split(',')] for line in out.strip().splitlines() if ',' in line]
    for u,free in rows:
        if not uuid or u==uuid:return int(free)
    return None
class Wand:
    def __init__(self):
        self.lock=threading.Lock();self.proc=None;self.gpu=None;self.last=0.0;self.want=None;self.want_at=0.0
        threading.Thread(target=self._reaper,daemon=True).start()
    def _env(self,cfg):
        scripts=Path(cfg['scripts_dir'])
        root=Path(json.loads((scripts/'runtime.local.json').read_text())['pipeline_root'])
        env=os.environ.copy()
        for line in (root/'kaist-full/config.env').read_text().splitlines():
            if line and not line.startswith('#'):
                k,v=line.split('=',1);env[k]=' '.join(shlex.split(v))
        env.update(PYTHONUNBUFFERED='1',PYTORCH_ALLOC_CONF='expandable_segments:True',HF_HUB_OFFLINE='1',TRANSFORMERS_OFFLINE='1')
        return env,root/'kaist-full/PhysTwin'
    def _stop(self):
        if self.proc:
            try:self.proc.stdin.close();self.proc.wait(timeout=15)
            except Exception:self.proc.kill()
        self.proc=None
        if self.gpu:
            self.gpu.close();self.gpu=None
    def _sam_unit(self,cfg):
        # RTX 5090처럼 SAM3를 2개까지 함께 돌리는 GPU: 마법봉이 그중 한 자리를 씁니다(gpu_slots 설명 참고).
        # SAM3 2개가 이미 돌고 있으면 '마법봉 대기' 표시를 켜 두어, 하나가 끝난 뒤 다음 SAM3가 그 자리를 채우지 않게 합니다.
        from gpu_slots import acquire_wand,want_wand
        path=WORKER_ROOT/'gpu_4090_a.lock'
        shared,unit=acquire_wand(path,cfg.get('gpu_uuid'))
        if shared and not unit:
            self.want=self.want or want_wand(path);self.want_at=time.monotonic()
            raise HTTPException(409,'SAM3 작업 2개가 함께 실행 중이라 지금은 마법봉을 쓸 수 없습니다. 하나가 끝나면 그 자리를 마법봉용으로 10분 동안 비워 두니, 잠시 뒤 다시 눌러 주세요.')
        self.gpu=unit;self._unwant()
    def _unwant(self):
        if self.want:self.want.close();self.want=None
    def _start(self,cfg):
        self._sam_unit(cfg)
        # 다른 GPU 작업이 돌고 있는지가 아니라, 남은 GPU 메모리로 판단합니다.
        # (MANO 등은 메모리를 적게 써서, 작업 중에도 마법봉을 함께 쓸 수 있습니다.)
        free=gpu_free_mb(cfg.get('gpu_uuid'))
        if free is not None and free<WAND_MIN_FREE_MB:
            self._stop()
            raise HTTPException(409,f'GPU 메모리가 부족해 마법봉을 쓸 수 없습니다. (남은 메모리 {free/1024:.1f}GB, 필요 {WAND_MIN_FREE_MB/1024:.0f}GB 이상) 브러시를 쓰거나 작업이 끝난 뒤 다시 시도하세요.')
        env,cwd=self._env(cfg)
        log=(WORKER_ROOT/'sam_wand.log').open('ab')
        self.proc=subprocess.Popen([env['KP_PY_SAM3'],str(WORKER_ROOT/'sam_wand_server.py')],cwd=cwd,env=env,
                                   stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=log,text=True,bufsize=1)
        ready=self.proc.stdout.readline()
        if not ready.strip().startswith('{"ready"'):
            self._stop();raise HTTPException(500,'SAM3 마법봉을 시작하지 못했습니다. worker/sam_wand.log를 확인하세요.')
    def segment(self,cfg,request):
        return self.call(cfg,request)
    def call(self,cfg,request,fresh=False,stop_after=False):
        with self.lock:
            try:return self._call(cfg,request,fresh)
            finally:
                if stop_after:self._stop()
    def _call(self,cfg,request,fresh):
        if self.proc and (fresh or self.proc.poll() is not None):self._stop()
        if not self.proc:self._start(cfg)
        self.last=time.monotonic()
        try:
            self.proc.stdin.write(json.dumps(request)+'\n');self.proc.stdin.flush()
            line=self.proc.stdout.readline()
        except (BrokenPipeError,OSError):line=''
        self.last=time.monotonic()
        if not line:
            self._stop();raise HTTPException(500,'SAM3 마법봉이 종료됐습니다. 다시 시도하세요. (worker/sam_wand.log)')
        result=json.loads(line)
        if not result.get('ok'):raise HTTPException(409,result.get('error','분할하지 못했습니다.'))
        return result
    def status(self):
        return {'loaded':bool(self.proc and self.proc.poll() is None),'idle_seconds':WAND_IDLE}
    def _reaper(self):
        while True:
            time.sleep(10)
            with self.lock:
                if self.proc and time.monotonic()-self.last>WAND_IDLE:self._stop()
                # 마법봉을 다시 누르지 않으면 대기 표시를 10분 뒤 꺼서 SAM3가 다시 2개씩 돌게 합니다.
                if self.want and time.monotonic()-self.want_at>WAND_WANT_SECONDS:self._unwant()
wand=Wand()

@app.post('/dags/{dag_id}/episodes/{episode}/wand',dependencies=[Depends(requires_access_dag(method='POST'))])
async def wand_segment(dag_id:str,episode:str,request:Request,user:GetUserDep):
    if urlsplit(request.headers.get('origin','')).netloc!=request.headers.get('host'):raise HTTPException(403,'Same-origin editing required')
    cookie=request.cookies.get('kaist_review_csrf','');header=request.headers.get('x-csrf-token','')
    if not cookie or not secrets.compare_digest(cookie,header):raise HTTPException(403,'Invalid CSRF token')
    raw=await request.body()
    if len(raw)>8192:raise HTTPException(413,'Request too large')
    try:data=json.loads(raw)
    except ValueError:raise HTTPException(400,'Invalid JSON')
    try:
        cam,frame=int(data['cam']),int(data['frame'])
        points=[[float(x),float(y),1 if int(l)==1 else 0] for x,y,l in data['points']][:20]
        text=str(data.get('text') or '').strip()[:120]
        threshold=min(0.95,max(0.05,float(data.get('threshold',0.5))))
    except (KeyError,TypeError,ValueError):raise HTTPException(400,'Invalid wand request')
    if not points or not any(l==1 for _,_,l in points) or any(not(0<=x<=1 and 0<=y<=1) for x,y,_ in points):
        raise HTTPException(400,'포함할 위치를 한 번 이상 클릭하세요.')
    source=edit_backend(dag_id,episode,'wand_source',['--cam',str(cam),'--frame',str(frame)])
    cfg=json.loads(config_for(dag_id).read_text())
    import asyncio
    return await asyncio.to_thread(wand.segment,cfg,{'image':source['rgb'],'points':points,'text':text,'threshold':threshold})

# ---- 추적 일괄 교체: 클릭한 프레임의 마법봉 시드를 SAM3 트래커로 앞뒤 전파해, 구간의 손/물체 마스크를 교체 ----
# 수백 프레임이라 백그라운드로 실행하고, 화면은 상태(진행률)를 조회합니다. 한 번에 하나만 실행합니다.
TRACK_ROOT=WORKER_ROOT/'wand_tracks'
TRACK_JOBS={}
_track_lock=threading.Lock()
def _track_run(job,dag_id,episode,cfg,req,apply):
    try:
        # GPU 초기화: 마법봉 프로세스를 새로 띄워(이전 세션·캐시 메모리 해제) 추적하고, 끝나면 바로 내려 메모리를 돌려줍니다.
        res=wand.call(cfg,req,fresh=True,stop_after=True)
        job.update(state='applying',track=res)
        out=edit_backend(dag_id,episode,'apply_track',data=apply,timeout=900)
        job.update(state='done',result=out,finished=time.time())
    except HTTPException as e:
        job.update(state='failed',error=str(e.detail)[-800:],finished=time.time())
    except Exception as e:
        job.update(state='failed',error=str(e)[-800:],finished=time.time())
    finally:
        import shutil
        shutil.rmtree(req['out'],ignore_errors=True)

@app.post('/dags/{dag_id}/episodes/{episode}/wand/track',dependencies=[Depends(requires_access_dag(method='POST'))])
async def wand_track_start(dag_id:str,episode:str,request:Request,user:GetUserDep):
    if urlsplit(request.headers.get('origin','')).netloc!=request.headers.get('host'):raise HTTPException(403,'Same-origin editing required')
    cookie=request.cookies.get('kaist_review_csrf','');header=request.headers.get('x-csrf-token','')
    if not cookie or not secrets.compare_digest(cookie,header):raise HTTPException(403,'Invalid CSRF token')
    raw=await request.body()
    if len(raw)>8192:raise HTTPException(413,'Request too large')
    try:
        data=json.loads(raw)
        cam,frame,obj=int(data['cam']),int(data['frame']),int(data['obj'])
        a,b=int(data['from']),int(data['to'])
        points=[[float(x),float(y),1 if int(l)==1 else 0] for x,y,l in data['points']][:20]
        text=str(data.get('text') or '').strip()[:120]
        threshold=min(0.95,max(0.05,float(data.get('threshold',0.5))))
    except (KeyError,TypeError,ValueError):raise HTTPException(400,'Invalid track request')
    if obj not in (0,1) or not points or not any(l==1 for _,_,l in points) or any(not(0<=x<=1 and 0<=y<=1) for x,y,_ in points):
        raise HTTPException(400,'포함할 위치를 한 번 이상 클릭하세요.')
    source=edit_backend(dag_id,episode,'track_source',['--cam',str(cam),'--frame',str(frame)])
    n=source['frames'];a=max(0,min(a,n-1));b=max(0,min(b,n-1))
    if a>b:a,b=b,a
    if not a<=frame<=b:raise HTTPException(400,'클릭한 프레임이 적용 구간 안에 있어야 합니다.')
    with _track_lock:
        if any(j['state'] in ('tracking','applying') for j in TRACK_JOBS.values()):raise HTTPException(409,'다른 추적 일괄 교체가 진행 중입니다. 끝난 뒤 다시 시도하세요.')
        job_id=uuid.uuid4().hex[:16];out=TRACK_ROOT/job_id;TRACK_ROOT.mkdir(exist_ok=True)
        job={'id':job_id,'episode':episode,'cam':cam,'obj':obj,'frames':[a,b],'start':frame,'state':'tracking','created':time.time(),'out':str(out)}
        TRACK_JOBS[job_id]=job
    cfg=json.loads(config_for(dag_id).read_text())
    req={'type':'track','color_dir':source['color_dir'],'start':frame,'from':a,'to':b,'fps':source.get('fps',30),
         'points':points,'text':text,'threshold':threshold,'out':str(out)}
    apply={'cam':cam,'obj':obj,'from':a,'to':b,'start':frame,'dir':str(out),'points':points,'text':text,'operator':user.get_name()}
    threading.Thread(target=_track_run,args=(job,dag_id,episode,cfg,req,apply),daemon=True).start()
    return {k:v for k,v in job.items() if k!='out'}

@app.get('/dags/{dag_id}/episodes/{episode}/wand/track/{job_id}',dependencies=[Depends(requires_access_dag(method='GET'))])
def wand_track_status(dag_id:str,episode:str,job_id:str):
    job=TRACK_JOBS.get(job_id)
    if not job or job['episode']!=episode:raise HTTPException(404,'추적 작업이 없습니다. (워커가 다시 시작됐을 수 있습니다)')
    out={k:v for k,v in job.items() if k!='out'}
    if job['state']=='tracking':
        try:out['progress']=json.loads((Path(job['out'])/'progress.json').read_text())
        except (OSError,ValueError):out['progress']={'done':0,'total':job['frames'][1]-job['frames'][0]+1,'phase':'load'}
    return out

@app.get('/dags/{dag_id}/wand/status',dependencies=[Depends(requires_access_dag(method='GET'))])
def wand_status(dag_id:str):
    return wand.status()

review_app=app
app=FastAPI(dependencies=[Depends(get_user)],docs_url=None,redoc_url=None)

def intake_fingerprint(folder):
    """Match the canonical upload manifest used by the central intake catalogue (one RAW folder)."""
    import hashlib
    manifest = folder/'upload_manifest.json'
    if not manifest.is_file() or not (folder/'upload_complete.json').is_file():return None
    try:
        value = json.loads(manifest.read_text())
        if value.get('episode') != folder.name:return None
        return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()
    except (OSError, ValueError, AttributeError):return None

# ---- RAW 목록 캐시 ----
# /worker/info는 화면이 몇 초마다 부르는데, 예전에는 부를 때마다 NAS의 RAW 폴더 전체(수백 개)를 돌며
# 폴더마다 매니페스트를 다시 읽었습니다. NAS가 느린 날에는 한 번에 몇 분이 걸려 RAW 큐가 뜨지 않았습니다.
# 이제 요청은 기억해 둔 목록을 바로 돌려주고, 목록이 오래됐으면 뒤에서 한 번만(동시에 하나만) 다시 읽습니다.
#  - 폴더 이름: 디렉터리 한 번 읽기로 끝냅니다(폴더마다 따로 묻지 않음).
#  - 매니페스트 지문: 한 번 확인한 폴더는 INFO_RECHECK초 동안 다시 읽지 않습니다.
#    실행 직전 확인은 /worker/info?episode=<RAW>가 그 폴더 하나만 NAS에서 새로 읽습니다.
INFO_FRESH=float(os.environ.get('KAIST_INFO_FRESH_S','20'))
INFO_RECHECK=float(os.environ.get('KAIST_INFO_RECHECK_S','300'))
_INFO={'root':None,'episodes':None,'at':0.0,'prints':{},'checked':{},'busy':False,'error':''}
_INFO_DONE=__import__('threading').Condition()
_GPU_LABEL=[]

def _info_scan(raw_root):
    import time
    names=[];folders=[]
    with os.scandir(raw_root) as entries:
        for e in entries:
            if not e.name.startswith('ep_'):continue
            try:
                if not e.is_dir():continue
                real=e.is_dir(follow_symlinks=False)
            except OSError:continue
            if re.fullmatch(r'ep_[A-Za-z0-9_]+',e.name):names.append(e.name)
            if real:folders.append(e.name)
    with _INFO_DONE:  # 목록부터 내놓고, 지문은 이어서 채웁니다.
        if _INFO['root']!=raw_root:return
        _INFO.update(episodes=sorted(names),at=time.time(),error='');_INFO_DONE.notify_all()
    for name in folders:
        with _INFO_DONE:
            if _INFO['root']!=raw_root:return
            if time.time()-_INFO['checked'].get(name,0)<INFO_RECHECK:continue
        value=intake_fingerprint(Path(raw_root)/name)
        with _INFO_DONE:
            if _INFO['root']!=raw_root:return
            _INFO['checked'][name]=time.time()
            if value:_INFO['prints'][name]=value
            else:_INFO['prints'].pop(name,None)
    keep=set(folders)
    with _INFO_DONE:
        if _INFO['root']!=raw_root:return
        for key in ('prints','checked'):
            for name in [n for n in _INFO[key] if n not in keep]:del _INFO[key][name]

def _info_refresh(raw_root):
    error=''
    try:_info_scan(raw_root)
    except Exception as e:error=f'{type(e).__name__}: {e}'
    finally:
        with _INFO_DONE:
            if _INFO['root']==raw_root:_INFO.update(busy=False,error=error)
            _INFO_DONE.notify_all()

def raw_info(raw_root):
    """(RAW 폴더 이름, 매니페스트 지문). 기억해 둔 값을 바로 돌려주고 오래됐으면 뒤에서 새로 읽습니다."""
    import threading,time
    with _INFO_DONE:
        if _INFO['root']!=raw_root:_INFO.update(root=raw_root,episodes=None,at=0.0,prints={},checked={},busy=False,error='')
        if time.time()-_INFO['at']>=INFO_FRESH and not _INFO['busy']:
            _INFO['busy']=True
            threading.Thread(target=_info_refresh,args=(raw_root,),daemon=True).start()
        while _INFO['episodes'] is None and _INFO['busy'] and _INFO['root']==raw_root:_INFO_DONE.wait(1)  # 시작 직후 첫 목록만 기다림
        if _INFO['root']!=raw_root or _INFO['episodes'] is None:
            raise HTTPException(503,'RAW 폴더를 읽지 못했습니다: '+(_INFO['error'] or raw_root))
        return list(_INFO['episodes']),dict(_INFO['prints'])

def intake_fingerprints(raw_root):
    return raw_info(raw_root)[1]

@app.get('/worker/intake/fingerprint')
def manual_fingerprint(episode:str):
    import hashlib
    import finder_raw
    if not re.fullmatch(r'ep_[A-Za-z0-9_]+', episode):
        raise HTTPException(400, 'Invalid episode')
    cfg=json.loads((ROOT/'config/gpu_4090_a.json').read_text())
    check_project_mount(cfg)
    try:
        manifest=finder_raw.manifest(Path(cfg['raw_root'])/episode)
    except (ValueError,KeyError,OSError,TypeError) as error:
        raise HTTPException(409, str(error)) from error
    return {'fingerprint':hashlib.sha256(json.dumps(manifest,sort_keys=True).encode()).hexdigest()}

@app.get('/worker/info')
def worker_info(episode:str=''):
    cfg=json.loads((ROOT/'config/gpu_4090_a.json').read_text())
    if not _GPU_LABEL:  # GPU 이름·UUID는 바뀌지 않으므로 한 번만 묻습니다.
        _GPU_LABEL.append(subprocess.check_output(['nvidia-smi','--query-gpu=name,uuid','--format=csv,noheader'],text=True,timeout=30).strip())
    episodes,prints=raw_info(cfg['raw_root'])
    if re.fullmatch(r'ep_[A-Za-z0-9_]+',episode):  # 실행 직전 확인: 이 RAW의 지문만 NAS에서 새로 읽습니다.
        value=intake_fingerprint(Path(cfg['raw_root'])/episode)
        if value:prints[episode]=value
        else:prints.pop(episode,None)
    return {'episodes':episodes,
            'intake_manifests':prints,
            'gpu':_GPU_LABEL[0],'server':cfg.get('server_label',__import__('socket').gethostname()),'raw_root':cfg['raw_root'],'project_dir':cfg['project_dir'],
            'nas_ready':Path(cfg['nas_mount']).is_mount(),'worker_kind':'ssh','server_airflow_required':False}

def gpu_status():
    """UI 사이드바의 GPU 메모리 표시용: GPU별 사용/전체 메모리(MiB)와 사용률, GPU 메모리를 쓰는 프로세스."""
    q=lambda args:[[v.strip() for v in line.split(',')] for line in subprocess.check_output(
        ['nvidia-smi',*args,'--format=csv,noheader,nounits'],text=True,timeout=10).strip().splitlines() if ',' in line]
    num=lambda v:int(float(v)) if v.replace('.','',1).isdigit() else None
    gpus=[{'index':num(i),'name':n,'uuid':u,'used_mb':num(used),'total_mb':num(total),'util':num(util)}
          for i,n,u,used,total,util in q(['--query-gpu=index,name,uuid,memory.used,memory.total,utilization.gpu'])]
    try:apps=q(['--query-compute-apps=gpu_uuid,pid,process_name,used_memory'])
    except (OSError,subprocess.SubprocessError,ValueError):apps=[]
    for g in gpus:
        g['processes']=[{'pid':num(pid),'name':Path(name.split(' --')[0]).name[:60],'used_mb':num(mem)} for u,pid,name,mem in apps if u==g['uuid']]
    return gpus

@app.get('/worker/gpu')
def worker_gpu():
    cfg=json.loads((ROOT/'config/gpu_4090_a.json').read_text())
    try:gpus=gpu_status()
    except (OSError,subprocess.SubprocessError,ValueError) as error:raise HTTPException(503,f'nvidia-smi 조회 실패: {error}') from error
    return {'gpus':gpus,'worker_gpu_uuid':cfg.get('gpu_uuid')}

@app.post('/worker/jobs/{action}')
async def jobs(action:str,request:Request):
    # stop: 실행 강제 종료 · skip: 시작 전 단계 건너뜀 · sync/sync-status: 에피소드 작업 폴더를 NAS에 올리기 (모두 runner.py가 새 프로세스로 처리)
    if action not in ('start','status','retry','rewind','stop','skip','sync','sync-status','sam-settings','reset','reset-status','drift','stop-all','purge','purge-status'):raise HTTPException(404)
    raw=await request.body()
    if len(raw)>8192:raise HTTPException(413)
    r=subprocess.run(['/usr/bin/python3',str(WORKER_ROOT/'runner.py'),action],input=raw,capture_output=True,timeout=20)
    if r.returncode:raise HTTPException(409,r.stderr.decode(errors='replace')[-1200:])
    return json.loads(r.stdout)

@app.post('/worker/log')
async def job_log(request:Request):
    import importlib.util
    spec=importlib.util.spec_from_file_location('runner',WORKER_ROOT/'runner.py');mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
    req=await request.json();p=mod.folder(req)/'job.log'
    return {'log':p.read_text(errors='replace')[-16000:] if p.exists() else ''}

from output_browser import router as outputs_router
app.include_router(outputs_router)
app.mount('/kaist-review',review_app)
