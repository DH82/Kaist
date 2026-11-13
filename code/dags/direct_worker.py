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
 return req

def poll(stage,worker_id=DEFAULT_WORKER,**ctx):
 worker(worker_id)
 if ctx['params'].get('worker_id',worker_id)!=worker_id:raise ValueError('DAG and worker mismatch')
 req=request_for(stage,ctx['run_id'],ctx['params'])
 result=call('POST','/worker/jobs/start',json=req,worker_id=worker_id)
 if result['state'] in ('failed','interrupted'):
  log=call('POST','/worker/log',json=req,worker_id=worker_id)['log'];print(log)
  raise RuntimeError('GPU worker failed at '+stage+'; see worker log above')
 print(json.dumps({'stage':stage,'state':result['state'],'remote_log':result.get('log')}))
 return result['state']=='success'


MANO_STAGES = ('hands', 'validate', 'publish')

def poll_mano_batch(**ctx):
    for source in ctx['dag_run'].conf['sources']:
        conf = source['conf']
        run = source['run_id']
        def request(stage, action):
            return call('POST', '/worker/jobs/' + action, worker_id=DEFAULT_WORKER,
                        json=request_for(stage, run, conf))['state']

        if request('publish', 'status') == 'success':
            continue
        if request('reconstruct', 'status') != 'success':
            raise RuntimeError(f"{conf['episode_id']} 3D 복원 완료 기록을 확인하세요.")
        for stage in MANO_STAGES:
            state = request(stage, 'start')
            print(conf['episode_id'], stage, state)
            if state in ('failed', 'interrupted'):
                raise RuntimeError(f"{conf['episode_id']} {stage} {state}")
            if state != 'success':
                return False
    return True
