"""Durable worker jobs. No Airflow daemon/import is required on the GPU PC."""
import fcntl,hashlib,json,os,re,subprocess,sys,time,traceback,uuid
from pathlib import Path
ROOT=Path(__file__).resolve().parent
JOBS=ROOT/'jobs'
CONFIG=ROOT.parent/'config/gpu_4090_a.json'
ADAPTER=ROOT.parent/'dags/kaist_runtime.py'
STAGES={'prepare_raw','sam_settings_ready','sam3','mask_approved','preflight','track','tracking_approved','prepare_reconstruction','reconstruct','hands','validate','publish'}
def write(path,data):
 tmp=path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp');tmp.write_text(json.dumps(data,indent=2));os.replace(tmp,path)
def folder(req):
 if req.get('stage') not in STAGES:raise ValueError('Unknown stage')
 if not re.fullmatch('ep_[A-Za-z0-9_]+',req.get('episode','')):raise ValueError('Invalid episode')
 if not isinstance(req.get('run_id'),str) or not 1<=len(req['run_id'])<=250:raise ValueError('Invalid run')
 if len(req.get('sam_prompt',''))>200 or req.get('sam_scene','2d')!='2d':raise ValueError('Invalid SAM settings')
 return JOBS/hashlib.sha256((req['run_id']+'\0'+req['episode']+'\0'+req['stage']).encode()).hexdigest()[:32]
def status(p):
 st=json.loads((p/'status.json').read_text()) if (p/'status.json').exists() else {'state':'not_started'}
 if st['state'] in ('queued','running') and time.time()-st.get('created',time.time())>20:
  pid=st.get('pid')
  if not pid and (p/'pid').exists():pid=int((p/'pid').read_text())
  # Inspect the exact command to avoid treating a reused PID as this job.
  try:alive=bool(pid and str(p).encode() in (Path('/proc')/str(pid)/'cmdline').read_bytes())
  except (OSError,ValueError):alive=False
  if not alive:
   st={**st,'state':'interrupted','error':'Worker process disappeared; inspect log before retrying'};write(p/'status.json',st)
 return st
def start(req):
 JOBS.mkdir(parents=True,exist_ok=True);p=folder(req);p.mkdir(exist_ok=True)
 with (p/'launch.lock').open('a') as lock:
  fcntl.flock(lock,fcntl.LOCK_EX)
  if (p/'request.json').exists() and json.loads((p/'request.json').read_text())!=req:raise ValueError('Request changed for existing job')
  st=status(p)
  # Waiting is an unsatisfied review condition (exit 75), not an approval.
  if st['state'] not in ('not_started','waiting'):return st
  write(p/'request.json',req);write(p/'status.json',{'state':'queued','stage':req['stage'],'created':time.time(),'log':str(p/'job.log')})
  with (p/'job.log').open('ab') as log:
   proc=subprocess.Popen(['/usr/bin/python3',str(__file__),'run',str(p)],stdin=subprocess.DEVNULL,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
  (p/'pid').write_text(str(proc.pid))
 return status(p)
def retry(req):
 """Reset a terminal failure only; retain the previous log/status for diagnosis."""
 import shutil
 p=folder(req)
 if not p.exists():return {'state':'not_started'}
 with (p/'launch.lock').open('a') as lock:
  fcntl.flock(lock,fcntl.LOCK_EX)
  if json.loads((p/'request.json').read_text())!=req:raise ValueError('Retry request differs from original')
  st=status(p)
  if st['state']=='not_started':return st
  if st['state'] not in ('failed','interrupted'):raise ValueError('Only failed/interrupted jobs can be retried')
  archive=p/'attempts'/uuid.uuid4().hex;archive.mkdir(parents=True)
  for name in ('request.json','status.json','job.log','pid'):
   if (p/name).exists():shutil.copy2(p/name,archive/name)
  # The existing log is append-only; the archive preserves the exact failed attempt.
  ready={'state':'not_started','stage':req['stage'],'retry_prepared':time.time(),'previous_attempt':str(archive),'log':str(p/'job.log')}
  write(p/'status.json',ready)
  return ready

def run(p):
 req=json.loads((p/'request.json').read_text());initial=status(p)
 try:
  with (ROOT/'gpu_4090_a.lock').open('a') as lock:
   fcntl.flock(lock,fcntl.LOCK_EX)
   write(p/'status.json',{**initial,'state':'running','started':time.time(),'pid':os.getpid()})
   cmd=['/usr/bin/python3',str(ADAPTER),req['stage'],req['episode'],req['run_id'],'--config',str(CONFIG),'--sam-prompt',req.get('sam_prompt',''),'--sam-scene',req.get('sam_scene','2d')]
   if req.get('raw_episode'):cmd.extend(['--raw-episode',req['raw_episode']])
   if req.get('sam_confirmed',False):cmd.append('--sam-confirmed')
   print('WORKER stage='+req['stage']+' episode='+req['episode']+' run='+req['run_id'],flush=True)
   result=subprocess.run(cmd,pass_fds=(lock.fileno(),))
   write(p/'status.json',{**status(p),'state':{0:'success',75:'waiting'}.get(result.returncode,'failed'),'returncode':result.returncode,'finished':time.time()})
 except Exception:
  traceback.print_exc();write(p/'status.json',{**status(p),'state':'failed','finished':time.time(),'error':'Worker execution failed; inspect job.log'})
if __name__=='__main__':
 if sys.argv[1]=='run':run(Path(sys.argv[2]))
 else:
  req=json.load(sys.stdin);p=folder(req)
  print(json.dumps(start(req) if sys.argv[1]=='start' else retry(req) if sys.argv[1]=='retry' else status(p)))
