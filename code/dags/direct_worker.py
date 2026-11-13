"""Central Airflow worker client; SSH tunnel to GPU PC, no remote Airflow API."""
import json,os,subprocess,threading,time
from pathlib import Path
import httpx
import sys
sys.path.insert(0,str(Path(__file__).parent))
from workers import worker,DEFAULT_WORKER
BASE=worker()['url']  # Older callers retain PC1 default; new paths always pass worker_id.
_SECRETS={}

def token(worker_id=DEFAULT_WORKER):
 pc=worker(worker_id)
 key=(worker_id,pc['token_file'])
 if key not in _SECRETS:
  if pc['token_file']:_SECRETS[key]=Path(pc['token_file']).read_text().strip()
  elif worker_id==DEFAULT_WORKER:
   # Existing Mac pilot only. NAS and additional PCs must configure token files.
   r=subprocess.run(['ssh','-S','/tmp/rira_v5_result.sock','-o','BatchMode=yes','-o','ConnectTimeout=10','cw-wfm@192.168.200.17','cat /home/cw-wfm/Desktop/rira/airflow/worker/secret'],text=True,capture_output=True,timeout=15,check=True)
   _SECRETS[key]=r.stdout.strip()
  else:raise ValueError('GPU PC token file is not configured: '+worker_id)
 return _SECRETS[key]

def call(method,path,worker_id=DEFAULT_WORKER,**kwargs):
 pc=worker(worker_id)
 with httpx.Client(base_url=pc['url'],trust_env=False,timeout=150) as client:
  r=client.request(method,path,headers={'Authorization':'Bearer '+token(worker_id)},**kwargs)
  r.raise_for_status();return r.json()

def request_for(stage,run_id,params):
 req={'stage':stage,'episode':params['episode_id'],'run_id':run_id,'sam_prompt':params.get('sam_prompt',''),
         'sam_scene':params.get('sam_scene','2d'),'sam_confirmed':params.get('sam_settings_confirmed',False)}
 if params.get('raw_episode_id') and params['raw_episode_id']!=params['episode_id']:req['raw_episode']=params['raw_episode_id']
 # Optional SAM3 object mode. Default text mode adds no keys, so existing job requests are unchanged.
 if params.get('sam_object_mode','text')=='point_track':
  req.update(sam_object_mode='point_track',sam_object_points=params.get('sam_object_points'))
 elif params.get('sam_object_mode','text')!='text':
  req.update(sam_object_mode=params['sam_object_mode'],sam_object_bboxes=params.get('sam_object_bboxes'))
 return req

POKE_WAIT_SECONDS=8  # 한 번 확인할 때 짧은 작업의 완료를 기다리는 최대 시간

def poll(stage,worker_id=DEFAULT_WORKER,**ctx):
 worker(worker_id)
 if ctx['params'].get('worker_id',worker_id)!=worker_id:raise ValueError('DAG and worker mismatch')
 req=request_for(stage,ctx['run_id'],ctx['params'])
 result=call('POST','/worker/jobs/start',json=req,worker_id=worker_id)
 # 승인 확인처럼 몇 초면 끝나는 작업은 이번 확인 안에서 결과를 기다려, 다음 확인 주기까지 기다리지 않고 바로 넘어갑니다.
 import time
 deadline=time.monotonic()+POKE_WAIT_SECONDS
 while result['state'] in ('queued','running') and time.monotonic()<deadline:
  time.sleep(1);result=call('POST','/worker/jobs/status',json=req,worker_id=worker_id)
 if result['state'] in ('failed','interrupted'):
  log=call('POST','/worker/log',json=req,worker_id=worker_id)['log'];print(log)
  raise RuntimeError('GPU worker failed at '+stage+'; see worker log above')
 print(json.dumps({'stage':stage,'state':result['state'],'remote_log':result.get('log'),'progress':result.get('progress')}))
 return result['state']=='success'


MANO_STAGES = ('hands', 'validate', 'publish')

MANO_LABELS = {'hands': 'MANO 피팅', 'validate': '결과 검사', 'publish': '최종 저장'}

# 평일(월~금) 오전 8시가 되면 그때 진행 중인 에피소드까지만 끝내고, 다음 에피소드는 시작하지 않습니다.
# 기준 시각은 일괄 실행을 시작한 뒤 처음 오는 평일 오전 8시입니다(예: 금 18시 시작 → 월 08:00).
MANO_TZ = 'Asia/Seoul'
MANO_CUTOFF_HOUR = 8

def mano_cutoff(started):
    import datetime as dt
    from zoneinfo import ZoneInfo
    tz = ZoneInfo(MANO_TZ)
    if isinstance(started, str):
        started = dt.datetime.fromisoformat(started.replace('Z', '+00:00'))
    local = (started or dt.datetime.now(dt.timezone.utc)).astimezone(tz)
    day = local.date()
    while True:
        cut = dt.datetime.combine(day, dt.time(MANO_CUTOFF_HOUR), tz)
        if cut > local and cut.weekday() < 5:
            return cut
        day += dt.timedelta(days=1)

def poll_mano_batch(**ctx):
    """선택한 순서대로 에피소드마다 hands→validate→publish를 진행합니다.
    UI에서 강제 종료한 에피소드(stopped_by_user)는 건너뛰고 다음 에피소드를 이어 갑니다."""
    import datetime as dt
    sources = ctx['dag_run'].conf['sources']
    run_obj = ctx['dag_run']
    cutoff = mano_cutoff(run_obj.start_date or run_obj.queued_at or run_obj.logical_date)
    for n, source in enumerate(sources, 1):
        conf = source['conf']
        run = source['run_id']
        tag = f"[{n}/{len(sources)}] {conf['episode_id']}"
        def request(stage, action):
            return call('POST', '/worker/jobs/' + action, worker_id=DEFAULT_WORKER,
                        json=request_for(stage, run, conf))

        if request('publish', 'status')['state'] == 'success':
            print(tag, '완료')
            continue
        if request('reconstruct', 'status')['state'] != 'success':
            raise RuntimeError(f"{conf['episode_id']} 3D 복원 완료 기록을 확인하세요.")
        # 아직 MANO를 시작하지 않은 에피소드: 평일 오전 8시가 지났으면 여기서 멈추고 나머지는 대기로 남깁니다.
        if request('hands', 'status')['state'] == 'not_started' and dt.datetime.now(cutoff.tzinfo) >= cutoff:
            left = [s['conf']['episode_id'] for s in sources[n - 1:]]
            print(f"평일 오전 {MANO_CUTOFF_HOUR}시({cutoff:%m-%d %H:%M}) 이후라 새 에피소드를 시작하지 않습니다. "
                  f"대기로 남김 {len(left)}개: {', '.join(left)}")
            return True
        stopped = False
        for stage in MANO_STAGES:
            current = request(stage, 'status')
            if current['state'] == 'interrupted' and current.get('stopped_by_user'):
                print(tag, MANO_LABELS[stage], '강제 종료됨 · 이 에피소드는 건너뜁니다')
                stopped = True
                break
            result = request(stage, 'start')
            state = result['state']
            progress = result.get('progress') or {}
            detail = f" · {progress.get('label')} {progress.get('done')}/{progress.get('total')}" if progress.get('total') else ''
            print(tag, MANO_LABELS[stage], state + detail)
            if state in ('failed', 'interrupted'):
                if result.get('stopped_by_user'):
                    stopped = True
                    break
                raise RuntimeError(f"{conf['episode_id']} {stage} {state}")
            if state != 'success':
                return False
        if stopped:
            continue
    return True
