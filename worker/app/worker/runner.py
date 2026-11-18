"""Durable worker jobs. No Airflow daemon/import is required on the GPU PC."""
import fcntl,hashlib,json,os,re,subprocess,sys,time,traceback,uuid
from pathlib import Path
ROOT=Path(__file__).resolve().parent
JOBS=ROOT/'jobs'
CONFIG=ROOT.parent/'config/gpu_4090_a.json'
ADAPTER=ROOT.parent/'dags/kaist_runtime.py'
STAGES={'adopt','prepare_raw','sam_settings_ready','sam3','mask_approved','preflight','track','tracking_approved','prepare_reconstruction','reconstruct','hands','validate','publish'}
def write(path,data):
 tmp=path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp');tmp.write_text(json.dumps(data,indent=2));os.replace(tmp,path)
def folder(req):
 if req.get('stage') not in STAGES:raise ValueError('Unknown stage')
 if not re.fullmatch('ep_[A-Za-z0-9_]+',req.get('episode','')):raise ValueError('Invalid episode')
 if not isinstance(req.get('run_id'),str) or not 1<=len(req['run_id'])<=250:raise ValueError('Invalid run')
 if len(req.get('sam_prompt',''))>200 or req.get('sam_scene','2d')!='2d':raise ValueError('Invalid SAM settings')
 # Optional object mode; absent keys keep the original request (and job folder) unchanged.
 # bbox_track은 sam_object_bboxes, point_track은 sam_object_points만 함께 와야 합니다.
 mode=req.get('sam_object_mode');need={'bbox_track':'sam_object_bboxes','point_track':'sam_object_points'}
 if ('sam_object_mode' in req and mode not in need) or any((k in req)!=(mode==m) for m,k in need.items()):raise ValueError('Invalid SAM object mode')
 # adopt(MANO 이관 받기)만 transfer_from(pc1~6)·transfer_key(원본 실행 키)를 함께 받습니다.
 if (req['stage']=='adopt')!=('transfer_from' in req) or ('transfer_from' in req)!=('transfer_key' in req):raise ValueError('Invalid transfer request')
 if 'transfer_from' in req and (not re.fullmatch('pc[1-6]',str(req['transfer_from'])) or not re.fullmatch('[0-9a-f]{24}',str(req['transfer_key']))):raise ValueError('Invalid transfer source')
 if 'result_pc' in req and (req['stage']!='publish' or not re.fullmatch('pc[1-6]',str(req['result_pc']))):raise ValueError('Invalid result PC')
 # 처리 fps: 15fps 실행만 target_fps=15를 함께 보냅니다. 키가 없으면 30fps(기존 요청·작업 폴더 그대로).
 if 'target_fps' in req and req['target_fps']!=15:raise ValueError('Invalid target fps')
 if 'sam_object_bboxes' in req and len(json.dumps(req['sam_object_bboxes']))>1000:raise ValueError('Invalid SAM object bbox')
 if 'sam_object_points' in req and len(json.dumps(req['sam_object_points']))>6000:raise ValueError('Invalid SAM object points')
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

def rewind(req):
 """앞 단계 다시 실행(UI 재시도가 마스크 재승인으로 바뀐 추적 입력을 감지한 경우): 끝난 작업(성공 포함)을 시작 전으로 되돌립니다.
 실행·대기 중인 작업은 건드리지 않고, 이전 기록은 retry와 같이 attempts/에 보관합니다."""
 import shutil
 p=folder(req)
 if not p.exists():return {'state':'not_started'}
 with (p/'launch.lock').open('a') as lock:
  fcntl.flock(lock,fcntl.LOCK_EX)
  if json.loads((p/'request.json').read_text())!=req:raise ValueError('Retry request differs from original')
  st=status(p)
  if st['state']=='not_started':return st
  if st['state'] in ('queued','running'):raise ValueError('Running jobs cannot be rewound')
  archive=p/'attempts'/uuid.uuid4().hex;archive.mkdir(parents=True)
  for name in ('request.json','status.json','job.log','pid'):
   if (p/name).exists():shutil.copy2(p/name,archive/name)
  ready={'state':'not_started','stage':req['stage'],'retry_prepared':time.time(),'rewound_from':st['state'],'previous_attempt':str(archive),'log':str(p/'job.log')}
  write(p/'status.json',ready)
  return ready

def run(p):
 req=json.loads((p/'request.json').read_text());initial=status(p)
 try:
  # GPU 슬롯(기본 2개) 중 빈 곳을 기다렸다가 잡습니다. 3번째 작업부터는 queued로 대기합니다.
  from gpu_slots import acquire
  lock,slot=acquire(ROOT/'gpu_4090_a.lock')
  with lock:
   write(p/'status.json',{**initial,'state':'running','started':time.time(),'pid':os.getpid(),'gpu_slot':slot})
   cmd=['/usr/bin/python3',str(ADAPTER),req['stage'],req['episode'],req['run_id'],'--config',str(CONFIG),'--sam-prompt',req.get('sam_prompt',''),'--sam-scene',req.get('sam_scene','2d')]
   if req.get('raw_episode'):cmd.extend(['--raw-episode',req['raw_episode']])
   if req.get('result_pc'):cmd.extend(['--result-pc',req['result_pc']])
   if req.get('transfer_from'):cmd.extend(['--transfer-from',req['transfer_from'],'--transfer-key',req['transfer_key']])
   if req.get('sam_confirmed',False):cmd.append('--sam-confirmed')
   if req.get('target_fps'):cmd.extend(['--fps',str(req['target_fps'])])
   if req.get('sam_object_mode')=='point_track':cmd.extend(['--object-mode','point_track','--object-points',json.dumps(req['sam_object_points'])])
   elif req.get('sam_object_mode'):cmd.extend(['--object-mode',req['sam_object_mode'],'--object-bboxes',json.dumps(req['sam_object_bboxes'])])
   print('WORKER stage='+req['stage']+' episode='+req['episode']+' run='+req['run_id']+' gpu_slot='+str(slot),flush=True)
   sys.path.insert(0,str(ADAPTER.parent))
   from pipeline_progress import parse
   env=os.environ.copy();env['PYTHONUNBUFFERED']='1'
   with subprocess.Popen(cmd,pass_fds=(lock.fileno(),),env=env,stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT,text=True,errors='replace',bufsize=1) as proc:
    last_update=0;previous=None
    for line in proc.stdout:
     print(line,end='',flush=True)
     progress=parse(line)
     if progress is not None:
      now=time.time()
      key=(progress['label'],progress.get('total'))
      if key!=previous or now-last_update>=1 or progress.get('done')==progress.get('total'):
       write(p/'status.json',{**status(p),'progress':{**progress,'updated':now}})
       if progress.get('total') is not None:
        print(f"[진행] {req['stage']} · {progress['label']} · {progress['done']} / {progress['total']} {progress['unit']} ({progress['percent']}%)",flush=True)
       last_update=now;previous=key
    result=proc
    proc.wait()
   write(p/'status.json',{**status(p),'state':{0:'success',75:'waiting'}.get(result.returncode,'failed'),'returncode':result.returncode,'finished':time.time()})
 except Exception:
  traceback.print_exc();write(p/'status.json',{**status(p),'state':'failed','finished':time.time(),'error':'Worker execution failed; inspect job.log'})

# ---- 강제 종료 (UI "강제 종료": 동시 실행 자리를 비우기 위해 이 실행의 Worker 작업을 끝냅니다) ----
STAGE_ORDER=['prepare_raw','sam_settings_ready','sam3','mask_approved','preflight','track','tracking_approved','prepare_reconstruction','reconstruct','hands','validate','publish']
STOP_MARK='사용자가 강제 종료했습니다.'
def _check_ids(req):
 if not re.fullmatch('ep_[A-Za-z0-9_]+',req.get('episode','')):raise ValueError('Invalid episode')
 if not isinstance(req.get('run_id'),str) or not 1<=len(req['run_id'])<=250:raise ValueError('Invalid run')
def _job_dir(run_id,episode,stage):
 return JOBS/hashlib.sha256((run_id+'\0'+episode+'\0'+stage).encode()).hexdigest()[:32]
def _children():
 kids={}
 for d in Path('/proc').iterdir():
  if not d.name.isdigit():continue
  try:ppid=int((d/'stat').read_text().rsplit(')',1)[1].split()[1])
  except (OSError,ValueError,IndexError):continue
  kids.setdefault(ppid,[]).append(int(d.name))
 return kids
def _tree(pid):
 kids=_children();out=[pid];i=0
 while i<len(out):out.extend(kids.get(out[i],[]));i+=1
 return out
def _alive(pid):
 try:
  # 좀비(Z)는 이미 끝난 프로세스로 봅니다.
  return (Path('/proc')/str(pid)/'stat').read_text().rsplit(')',1)[1].split()[0]!='Z'
 except (OSError,IndexError):return False
def _kill_tree(pid,wait=8.0):
 import signal
 procs=_tree(pid)
 for sig in (signal.SIGTERM,signal.SIGKILL):
  for q in procs:
   try:os.kill(q,sig)
   except ProcessLookupError:pass
  deadline=time.time()+(wait if sig==signal.SIGTERM else 3)
  while time.time()<deadline and any(_alive(q) for q in procs):time.sleep(0.2)
  if not any(_alive(q) for q in procs):return True
  procs=[q for q in procs if _alive(q)]+[c for q in procs for c in _tree(q)[1:]]
 return not any(_alive(q) for q in procs)
def stop(req):
 """이 실행(run_id)·에피소드의 대기/실행 중 단계를 프로세스 트리째 종료하고 interrupted로 기록합니다.
 interrupted는 기존 '실패 단계 다시 실행'으로 이어서 실행할 수 있습니다."""
 _check_ids(req);stopped=[];stages=req.get('stages') or STAGE_ORDER
 if any(s not in STAGES for s in stages):raise ValueError('Unknown stage')
 for stage in stages:
  p=_job_dir(req['run_id'],req['episode'],stage)
  if not (p/'status.json').exists():continue
  with (p/'launch.lock').open('a') as lock:
   fcntl.flock(lock,fcntl.LOCK_EX)
   st=status(p)
   if st['state'] not in ('queued','running'):continue
   pid=st.get('pid')
   if not pid and (p/'pid').exists():pid=int((p/'pid').read_text())
   try:mine=bool(pid and str(p).encode() in (Path('/proc')/str(pid)/'cmdline').read_bytes())
   except OSError:mine=False
   done=_kill_tree(int(pid)) if mine else True
   if not done:raise RuntimeError('프로세스를 종료하지 못했습니다: '+stage)
   write(p/'status.json',{**status(p),'state':'interrupted','error':STOP_MARK,'stopped_by_user':True,'finished':time.time()})
   with (p/'job.log').open('a') as log:log.write('\n[강제 종료] '+time.strftime('%Y-%m-%d %H:%M:%S')+' '+STOP_MARK+'\n')
   stopped.append(stage)
 return {'stopped':stopped}

GATE_STAGES=('sam_settings_ready','mask_approved','tracking_approved')  # 검수·설정 확인(몇 초): 강제 업데이트도 건드리지 않습니다
def stop_all(req):
 """강제 업데이트: 이 Worker의 대기·실행 중인 단계 작업을 모두 종료하고 '사용자 강제 종료'(interrupted)로 기록합니다.
 모든 프로세스에 한꺼번에 SIGTERM → 8초 → SIGKILL 하므로 작업 수와 관계없이 약 11초 안에 끝납니다.
 단계 초기화(_reset)·NAS 업로드(_nas_sync)는 재시작이 끊습니다(다시 하면 이어서/처음부터 안전하게 진행)."""
 import signal
 found=[]
 for p in JOBS.iterdir() if JOBS.is_dir() else []:
  if not p.is_dir() or p.name.startswith('_') or not (p/'status.json').exists():continue
  st=status(p)
  if st['state'] not in ('queued','running') or st.get('stage') in GATE_STAGES:continue
  pid=st.get('pid')
  if not pid and (p/'pid').exists():pid=int((p/'pid').read_text())
  try:mine=bool(pid and str(p).encode() in (Path('/proc')/str(pid)/'cmdline').read_bytes())
  except (OSError,ValueError):mine=False
  found.append((p,_tree(int(pid)) if mine else []))
 procs=[q for _,tree in found for q in tree]
 for sig,wait in ((signal.SIGTERM,8),(signal.SIGKILL,3)):
  for q in procs:
   try:os.kill(q,sig)
   except ProcessLookupError:pass
  deadline=time.time()+wait
  while time.time()<deadline and any(_alive(q) for q in procs):time.sleep(0.2)
  procs=[q for q in procs if _alive(q)]
  if not procs:break
 stopped=[];left=[]
 for p,tree in found:
  req_=json.loads((p/'request.json').read_text()) if (p/'request.json').exists() else {}
  name=f"{req_.get('episode',p.name)} {req_.get('stage','')}".strip()
  if any(_alive(q) for q in tree):left.append(name);continue
  with (p/'launch.lock').open('a') as lock:
   fcntl.flock(lock,fcntl.LOCK_EX)
   write(p/'status.json',{**status(p),'state':'interrupted','error':STOP_MARK+' (강제 업데이트)','stopped_by_user':True,'finished':time.time()})
  with (p/'job.log').open('a') as log:log.write('\n[강제 종료] '+time.strftime('%Y-%m-%d %H:%M:%S')+' 관리자 강제 업데이트\n')
  stopped.append(name)
 return {'stopped':stopped,'left':left}

def skip(req):
 """아직 시작 전인 단계를 '사용자 강제 종료'로 기록합니다(MANO 일괄 실행이 이 에피소드를 건너뜀).
 기존 다시 실행(retry)으로 되돌릴 수 있도록 request.json도 같은 형식으로 남깁니다."""
 JOBS.mkdir(parents=True,exist_ok=True);p=folder(req);p.mkdir(exist_ok=True)
 with (p/'launch.lock').open('a') as lock:
  fcntl.flock(lock,fcntl.LOCK_EX)
  if (p/'request.json').exists() and json.loads((p/'request.json').read_text())!=req:raise ValueError('Request changed for existing job')
  st=status(p)
  if st['state']!='not_started':return st
  write(p/'request.json',req)
  st={'state':'interrupted','stage':req['stage'],'created':time.time(),'finished':time.time(),'error':STOP_MARK,'stopped_by_user':True,'log':str(p/'job.log')}
  write(p/'status.json',st)
  with (p/'job.log').open('a') as log:log.write('[강제 종료] '+time.strftime('%Y-%m-%d %H:%M:%S')+' 시작 전 단계를 건너뜁니다.\n')
  return st

# ---- NAS 업로드 (UI "NAS에 올리기": 지금 작업 폴더의 에피소드를 NAS work/<pc>에 미러링) ----
SYNC=JOBS/'_nas_sync'
def _sync_dir(req):
 if not re.fullmatch('ep_[A-Za-z0-9_]+',req.get('episode','')):raise ValueError('Invalid episode')
 return SYNC/req['episode']
def sync_status(req):
 d=_sync_dir(req)
 if not (d/'status.json').exists():return {'state':'not_started','episode':req['episode']}
 st=json.loads((d/'status.json').read_text())
 if st['state']=='running':
  pid=st.get('pid')
  try:alive=bool(pid and b'sync-run' in (Path('/proc')/str(pid)/'cmdline').read_bytes())
  except OSError:alive=False
  if not alive and time.time()-st.get('started',0)>20:
   st={**st,'state':'failed','error':'업로드 프로세스가 중단되었습니다. 다시 올려 주세요.'};write(d/'status.json',st)
 return st
def sync_start(req):
 d=_sync_dir(req);d.mkdir(parents=True,exist_ok=True)
 with (d/'launch.lock').open('a') as lock:
  fcntl.flock(lock,fcntl.LOCK_EX)
  st=sync_status(req)
  if st['state']=='running':return st
  write(d/'status.json',{'state':'running','episode':req['episode'],'started':time.time()})
  with (d/'sync.log').open('ab') as log:
   proc=subprocess.Popen(['/usr/bin/python3',str(__file__),'sync-run',req['episode']],stdin=subprocess.DEVNULL,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
  write(d/'status.json',{'state':'running','episode':req['episode'],'started':time.time(),'pid':proc.pid})
 return sync_status(req)
def _active_jobs():
 """실행 중(queued/running)인 단계 작업들의 {'episode','state'}. 업로드 후 로컬 삭제 전 확인용."""
 for p in JOBS.iterdir() if JOBS.is_dir() else []:
  if p.name.startswith('_') or not (p/'request.json').is_file():continue
  try:
   if json.loads((p/'status.json').read_text()).get('state') not in ('queued','running'):continue
   yield {'episode':json.loads((p/'request.json').read_text()).get('episode'),'state':status(p)['state']}
  except (OSError,ValueError):continue
def sync_run(episode):
 d=_sync_dir({'episode':episode});started=json.loads((d/'status.json').read_text()).get('started',time.time())
 print('NAS 업로드 시작:',episode,time.strftime('%Y-%m-%d %H:%M:%S'),flush=True)
 try:
  sys.path.insert(0,str(ADAPTER.parent))
  import sync_work
  cfg=json.loads(CONFIG.read_text())
  rec=sync_work.sync_episode(cfg,episode,lambda m:print(m,flush=True))
  # 최종 저장까지 끝난 에피소드면 업로드 후 로컬 사본을 지웁니다(진행 중인 단계 작업이 있으면 유지).
  deleted=[]
  if any(s.get('state') in ('queued','running') and s.get('episode')==episode for s in _active_jobs()):
   print('로컬 작업 파일 유지:',episode,'— 이 에피소드의 단계 작업이 실행 중입니다.',flush=True)
  else:
   try:deleted=sync_work.purge_local(cfg,episode,rec,lambda m:print(m,flush=True))
   except Exception as e:print('로컬 작업 파일 삭제 건너뜀:',e,flush=True)
  write(d/'status.json',{'state':'success','episode':episode,'started':started,'pid':os.getpid(),'finished':time.time(),
                         'bytes':rec.get('bytes'),'paths':rec.get('paths'),'destination':rec.get('destination'),'local_deleted':deleted})
 except Exception as e:
  traceback.print_exc()
  write(d/'status.json',{'state':'failed','episode':episode,'started':started,'pid':os.getpid(),'finished':time.time(),'error':str(e)[-600:]})

# ---- 단계 초기화 (UI '단계 초기화 · 다시 실행'): 끝난 실행의 한 단계부터 결과를 보관하고 작업을 시작 전으로 되돌립니다 ----
# NAS에서 작업 폴더를 되돌려 받을 수 있어(수 GB) 업로드처럼 별도 프로세스로 실행하고 reset-status로 확인합니다.
# 결과 보관 규칙은 dags/stage_reset.py, Airflow 단계 재실행은 UI가 이 작업이 끝난 뒤 요청합니다.
RESET=JOBS/'_reset'
RESET_STARTS=('prepare_raw','sam3','preflight','prepare_reconstruction','hands','validate','publish')
def _reset_dir(req):
 _check_ids(req)
 return RESET/hashlib.sha256((req['run_id']+'\0'+req['episode']).encode()).hexdigest()[:32]
def reset_status(req):
 d=_reset_dir(req)
 if not (d/'status.json').exists():return {'state':'not_started','episode':req['episode']}
 st=json.loads((d/'status.json').read_text())
 if st['state']=='running':
  pid=st.get('pid')
  try:alive=bool(pid and b'reset-run' in (Path('/proc')/str(pid)/'cmdline').read_bytes())
  except OSError:alive=False
  if not alive and time.time()-st.get('started',0)>20:
   st={**st,'state':'failed','error':'초기화 프로세스가 중단되었습니다. 로그를 확인하고 다시 시도하세요.'};write(d/'status.json',st)
 return st
def _run_stages(req):
 return STAGE_ORDER[STAGE_ORDER.index(req['start']):]
def reset_start(req):
 if req.get('start') not in RESET_STARTS:raise ValueError('초기화할 수 없는 단계입니다.')
 d=_reset_dir(req);d.mkdir(parents=True,exist_ok=True)
 with (d/'launch.lock').open('a') as lock:
  fcntl.flock(lock,fcntl.LOCK_EX)
  st=reset_status(req)
  if st['state']=='running':return st
  for stage in _run_stages(req):
   p=_job_dir(req['run_id'],req['episode'],stage)
   if p.exists() and status(p)['state'] in ('queued','running'):raise ValueError(stage+' 단계가 실행 중입니다. 끝나거나 강제 종료한 뒤 초기화하세요.')
  clean={'episode':req['episode'],'run_id':req['run_id'],'start':req['start']}
  write(d/'request.json',clean);write(d/'status.json',{'state':'running',**clean,'started':time.time()})
  with (d/'reset.log').open('ab') as log:
   proc=subprocess.Popen(['/usr/bin/python3',str(__file__),'reset-run',str(d)],stdin=subprocess.DEVNULL,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
  write(d/'status.json',{'state':'running',**clean,'started':time.time(),'pid':proc.pid})
 return reset_status(req)
def drift(req):
 """이 실행의 고정 기록(identity.json)이 지금 코드와 다른지 확인합니다(읽기만). MANO 일괄 실행이 업데이트 전 대기열을 고릅니다."""
 _check_ids(req)
 sys.path.insert(0,str(ADAPTER.parent))
 import stage_reset
 return stage_reset.drift_report(json.loads(CONFIG.read_text()),req['episode'],req['run_id'])
def reset_run(d):
 import contextlib,shutil
 req=json.loads((d/'request.json').read_text());started=json.loads((d/'status.json').read_text()).get('started',time.time())
 base={**req,'started':started,'pid':os.getpid()}
 print('단계 초기화 시작:',req['episode'],req['start'],'부터',time.strftime('%Y-%m-%d %H:%M:%S'),flush=True)
 try:
  sys.path.insert(0,str(ADAPTER.parent))
  import stage_reset
  cfg=json.loads(CONFIG.read_text())
  # NAS에서 되돌려 받기는 잠금 없이 먼저 합니다(오래 걸림). 결과 이동·작업 되돌리기는 단계 작업 시작 잠금을 모두 잡고 합니다.
  stage_reset.sync_work.restore_local(cfg,req['episode'],lambda m:print(m,flush=True))
  with contextlib.ExitStack() as stack:
   jobs=[]
   for stage in _run_stages(req):
    p=_job_dir(req['run_id'],req['episode'],stage)
    if not p.exists():continue
    fcntl.flock(stack.enter_context((p/'launch.lock').open('a')),fcntl.LOCK_EX)
    st=status(p)
    if st['state'] in ('queued','running'):raise RuntimeError(stage+' 단계가 실행 중이라 초기화를 중단했습니다(변경 없음).')
    jobs.append((p,stage,st))
   locks=Path(cfg['project_dir'])/'99_history/locks';locks.mkdir(parents=True,exist_ok=True)
   ep_lock=stack.enter_context((locks/f"{req['episode']}.lock").open('a'))
   fcntl.flock(ep_lock,fcntl.LOCK_EX)  # 같은 에피소드의 검수 저장이 끝날 때까지 기다립니다.
   record=stage_reset.reset_outputs(cfg,req['episode'],req['run_id'],req['start'],lambda m:print(m,flush=True))
   rewound=[]
   for p,stage,st in jobs:
    if st['state']=='not_started':continue
    archive=p/'attempts'/uuid.uuid4().hex;archive.mkdir(parents=True)
    for name in ('request.json','status.json','job.log','pid'):
     if (p/name).exists():shutil.copy2(p/name,archive/name)
    (p/'pid').unlink(missing_ok=True)
    write(p/'status.json',{'state':'not_started','stage':stage,'retry_prepared':time.time(),'reset_from':req['start'],'rewound_from':st['state'],'previous_attempt':str(archive),'log':str(p/'job.log')})
    rewound.append(stage)
  print('작업 상태 되돌림:',', '.join(rewound) or '없음',flush=True)
  write(d/'status.json',{**base,'state':'success','finished':time.time(),'rewound':rewound,'archive':record['archive'],
                         'moved':record['moved'],'restored':record['restored_from_nas'],'published_result':record['published_result'],'repinned':record['repinned']})
 except Exception as e:
  traceback.print_exc()
  write(d/'status.json',{**base,'state':'failed','finished':time.time(),'error':str(e)[-600:]})

# ---- SAM3 분할 대상 저장 (UI 'SAM3 설정 대기' → 이어서 실행) ----
# RAW 전처리만 하고 sam_settings_ready에서 기다리는 실행에, 파이프라인의 sam_local_setup.approve_only로
# 03_prepared/<ep>/sam3_settings.json 을 저장합니다. 다음 확인 때 sam_settings_ready가 통과하고 SAM3가 시작됩니다.
def sam_settings(req):
 ep=req.get('episode','');prompt=str(req.get('prompt') or '').strip()
 if not re.fullmatch('ep_[A-Za-z0-9_]+',ep):raise ValueError('Invalid episode')
 if not 1<=len(prompt)<=200:raise ValueError('SAM3 분할 대상을 1~200자로 입력하세요.')
 cfg=json.loads(CONFIG.read_text());scripts=Path(cfg['scripts_dir'])
 if not (Path(cfg['project_dir'])/'03_prepared'/ep).is_dir():raise ValueError('RAW 전처리가 아직 끝나지 않았습니다: '+ep)
 sys.path.insert(0,str(scripts))
 import importlib.util
 spec=importlib.util.spec_from_file_location('sam_local_setup',scripts/'sam_local_setup.py');mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
 settings=mod.approve_only(Path(cfg['project_dir']),ep,prompt,'2d')
 return {'ok':True,'episode':ep,'settings':settings}

if __name__=='__main__':
 if sys.argv[1]=='run':run(Path(sys.argv[2]))
 elif sys.argv[1]=='sync-run':sync_run(sys.argv[2])
 elif sys.argv[1]=='reset-run':reset_run(Path(sys.argv[2]))
 else:
  req=json.load(sys.stdin);action=sys.argv[1]
  if action=='stop':print(json.dumps(stop(req)))
  elif action=='skip':print(json.dumps(skip(req)))
  elif action=='sync':print(json.dumps(sync_start(req)))
  elif action=='sync-status':print(json.dumps(sync_status(req)))
  elif action=='sam-settings':print(json.dumps(sam_settings(req),ensure_ascii=False))
  elif action=='reset':print(json.dumps(reset_start(req),ensure_ascii=False))
  elif action=='reset-status':print(json.dumps(reset_status(req),ensure_ascii=False))
  elif action=='drift':print(json.dumps(drift(req),ensure_ascii=False))
  elif action=='stop-all':print(json.dumps(stop_all(req),ensure_ascii=False))
  else:
   p=folder(req)
   print(json.dumps(start(req) if action=='start' else retry(req) if action=='retry' else rewind(req) if action=='rewind' else status(p)))
