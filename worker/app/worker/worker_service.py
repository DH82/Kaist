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



def edit_backend(dag_id,episode,action,extra=(),data=None):
    if not re.fullmatch(r'ep_[A-Za-z0-9_]+',episode):raise HTTPException(400,'Invalid episode')
    config=config_for(dag_id);cfg=json.loads(config.read_text())
    cmd=[cfg['control_python'],str(WORKER_ROOT/'mask_editor.py'),action,'--config',str(config),'--episode',episode,*extra]
    result=subprocess.run(cmd,input=json.dumps(data).encode() if data is not None else None,capture_output=True,timeout=120)
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
import fcntl,shlex,threading,time
WAND_IDLE=int(os.environ.get('KAIST_WAND_IDLE','180'))
# 마법봉(SAM3.1)을 띄우기 위해 남아 있어야 하는 GPU 메모리(MiB). KAIST_WAND_MIN_FREE_MB로 조정합니다.
WAND_MIN_FREE_MB=int(os.environ.get('KAIST_WAND_MIN_FREE_MB','8192'))
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
        self.lock=threading.Lock();self.proc=None;self.gpu=None;self.last=0.0
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
    def _start(self,cfg):
        # 다른 GPU 작업이 돌고 있는지가 아니라, 남은 GPU 메모리로 판단합니다.
        # (MANO 등은 메모리를 적게 써서, 작업 중에도 마법봉을 함께 쓸 수 있습니다.)
        free=gpu_free_mb(cfg.get('gpu_uuid'))
        if free is not None and free<WAND_MIN_FREE_MB:
            raise HTTPException(409,f'GPU 메모리가 부족해 마법봉을 쓸 수 없습니다. (남은 메모리 {free/1024:.1f}GB, 필요 {WAND_MIN_FREE_MB/1024:.0f}GB 이상) 브러시를 쓰거나 작업이 끝난 뒤 다시 시도하세요.')
        env,cwd=self._env(cfg)
        log=(WORKER_ROOT/'sam_wand.log').open('ab')
        self.proc=subprocess.Popen([env['KP_PY_SAM3'],str(WORKER_ROOT/'sam_wand_server.py')],cwd=cwd,env=env,
                                   stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=log,text=True,bufsize=1)
        ready=self.proc.stdout.readline()
        if not ready.strip().startswith('{"ready"'):
            self._stop();raise HTTPException(500,'SAM3 마법봉을 시작하지 못했습니다. worker/sam_wand.log를 확인하세요.')
    def segment(self,cfg,request):
        with self.lock:
            if self.proc and self.proc.poll() is not None:self._stop()
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

@app.get('/dags/{dag_id}/wand/status',dependencies=[Depends(requires_access_dag(method='GET'))])
def wand_status(dag_id:str):
    return wand.status()

review_app=app
app=FastAPI(dependencies=[Depends(get_user)],docs_url=None,redoc_url=None)

def intake_fingerprints(raw_root):
    """Match the canonical upload manifest used by the central intake catalogue."""
    import hashlib
    result = {}
    for folder in Path(raw_root).glob('ep_*'):
        if not folder.is_dir() or folder.is_symlink():continue
        manifest = folder/'upload_manifest.json'
        if not manifest.is_file() or not (folder/'upload_complete.json').is_file():continue
        try:
            value = json.loads(manifest.read_text())
            if value.get('episode') != folder.name:continue
            result[folder.name] = hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()
        except (OSError, ValueError, AttributeError):continue
    return result

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
def worker_info():
    cfg=json.loads((ROOT/'config/gpu_4090_a.json').read_text())
    gpu=subprocess.check_output(['nvidia-smi','--query-gpu=name,uuid','--format=csv,noheader'],text=True).strip()
    return {'episodes':[p.name for p in sorted(Path(cfg['raw_root']).iterdir()) if p.is_dir() and re.fullmatch(r'ep_[A-Za-z0-9_]+',p.name)],
            'intake_manifests':intake_fingerprints(cfg['raw_root']),
            'gpu':gpu,'server':cfg.get('server_label',__import__('socket').gethostname()),'raw_root':cfg['raw_root'],'project_dir':cfg['project_dir'],
            'nas_ready':Path(cfg['nas_mount']).is_mount(),'worker_kind':'ssh','server_airflow_required':False}

@app.post('/worker/jobs/{action}')
async def jobs(action:str,request:Request):
    if action not in ('start','status','retry'):raise HTTPException(404)
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
