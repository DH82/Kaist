"""Tee subprocess logs with GPU telemetry; no model or GPU library dependency."""
import subprocess,threading,time,queue,json,csv,shlex,sys,re,shutil,unicodedata,os
import episode_progress

def gpu_rows():
 try:
  r=subprocess.run(['nvidia-smi','--query-gpu=index,utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw','--format=csv,noheader,nounits'],capture_output=True,text=True,timeout=3)
  if r.returncode:return []
  return list(csv.reader(r.stdout.strip().splitlines(),skipinitialspace=True))
 except (OSError,subprocess.TimeoutExpired):return []

def duration(value):
 if value is None:return '계산 중'
 return f'{int(value)//60}분 {int(value)%60:02d}초'

def bar(percent,width=20):
 filled=round(max(0,min(100,percent))*width/100)
 return '█'*filled+'░'*(width-filled)

def detail_lines(episode,progress,log,start,now,last_progress,activity):
 active=episode.active if episode else None
 labels={'baseline_preview':'전체 화면 결과 미리보기 생성','roi_preview':'ROI 결과 미리보기 생성'}
 label=labels.get(active,activity)
 if active and '_camera_' in active:
  camera=int(active.rsplit('_',1)[1])+1
  label=('전체 화면 추론' if active.startswith('baseline') else '손 ROI 4배 확대 추론')+f' · 카메라 {camera}/4'
 lines=[f'SAM3.1  /  {log.parent.name}', '── 전체 에피소드 ──────────────────────────────']
 if episode:
  pct,eta,provisional,done=episode.status()
  lines += [f'{bar(pct)}  {pct:5.1f}%  · 완료 {done}/10구간',
            f'경과 {duration(now-episode.start)}  /  남음 약 {duration(eta)}'+(' · 초기 추정(미측정 구간 포함)' if provisional else ' · 실측 보정')]
  lines.append('── 카메라별 상태 ──────────────────────────────')
  def state(name):return '✓완료' if name in episode.done else '▶진행' if name==active else '·대기'
  for prefix,title in [('baseline','전체 화면'),('roi','ROI')]:
   lines.append(title+': '+' / '.join(f'C{c+1} {state(prefix+"_camera_"+str(c))}' for c in range(4))+f' | 미리보기 {state(prefix+"_preview")}')
 lines.extend(['── 현재 작업 ──────────────────────────────────','현재: '+label])
 if progress:
  done=progress['done'];n=progress['frames'];elapsed=progress.get('elapsed_s',0);speed=done/elapsed if elapsed else 0
  prompt=progress['prompt'];meaning={'hand':'손 영역','human forearm':'전완 영역 (손에서 제거용)'}.get(prompt,'물체 영역')
  lines.append(f'대상: {meaning} [{prompt}]')
  lines.append(f'{bar(100*done/max(1,n))}  {done}/{n} ({100*done/max(1,n):.1f}%)')
  lines.append(f'처리속도 {speed:.1f}프레임/초')
  lines.append(f'현재 대상 남음 약 {duration(progress.get("eta_s"))} | 진행 수신 {int(now-last_progress)}초 전')
 else:lines.append('세부 작업: '+activity+' | 프레임 진행 수신 대기')
 return lines

class Display:
 def __init__(self):self.tty=sys.stdout.isatty();self.lines=0;self.last=0;self.color=self.tty and 'NO_COLOR' not in os.environ
 def clear(self):
  if self.lines:
   sys.stdout.write('\x1b[1A\r\x1b[2K'*self.lines);sys.stdout.flush();self.lines=0
 def render(self,lines):
  if not self.tty:
   if os.environ.get('WFM_WEB_RUN')=='1':lines=[s for s in lines if not s.startswith(('GPU','── GPU'))]
   if time.monotonic()-self.last<30:return
   print(' | '.join(lines),flush=True);self.last=time.monotonic();return
  self.clear();width=max(20,shutil.get_terminal_size().columns-1)
  for line in lines[:max(3,shutil.get_terminal_size().lines-3)]:
   out='';used=0
   for c in line:
    size=2 if unicodedata.east_asian_width(c) in 'WF' else 1
    if used+size>width:break
    out+=c;used+=size
   if self.color:
    if out.startswith('──'):out='\x1b[36m'+out+'\x1b[0m'
    elif out.startswith('SAM3.1'):out='\x1b[1;36m'+out+'\x1b[0m'
    elif '█' in out or '░' in out:out='\x1b[1;36m'+out+'\x1b[0m'
    else:
     for word,color in [('✓완료','32'),('▶진행','1;33'),('·대기','90')]:out=out.replace(word,'\x1b['+color+'m'+word+'\x1b[0m')
   sys.stdout.write(out+'\n')
  self.lines=min(len(lines),max(3,shutil.get_terminal_size().lines-3));sys.stdout.flush()

def execute(cmd,log,source,env):
 from stage_refs import command_view
 with command_view(cmd) as adapted:return _execute(adapted,log,source,env)

def _execute(cmd,log,source,env):
 messages=queue.Queue();start=time.monotonic();next_tick=start;progress=None;display=Display();gpu='GPU 조회 중';last_progress=start;activity='모델 로딩 또는 작업 준비'
 with log.open('a') as f:
  def say(line):display.clear();print(line,flush=True);f.write(line+'\n');f.flush()
  say('[시작] '+log.name+' | '+shlex.join(cmd))
  process=subprocess.Popen(cmd,cwd=source,env=env,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,errors='replace',bufsize=1)
  def read():
   try:
    for line in process.stdout:messages.put(line.rstrip('\n'))
   finally:messages.put(None)
  thread=threading.Thread(target=read,daemon=True);thread.start();finished=False
  try:
   while not finished:
    try:
     line=messages.get(timeout=.25)
     if line is None:finished=True
     else:
      f.write(line+'\n');f.flush()
      if 'SAM_PROGRESS ' in line:
       try:progress=json.JSONDecoder().raw_decode(line.split('SAM_PROGRESS ',1)[1])[0]
       except ValueError:progress=None
       if progress:
        last_progress=time.monotonic()
        if episode_progress.CURRENT:episode_progress.CURRENT.update(progress)
      elif line.startswith('SAM:'):
       activity=line[4:].strip();progress=None
      elif line.strip() and not re.search(r'propagate_in_video:|\d+%\||^INFO |^DEBUG ',line):
       display.clear();print(line,flush=True)
    except queue.Empty:pass
    now=time.monotonic()
    if now>=next_tick:
     rows=gpu_rows();parts=[]
     for row in rows:
      if len(row)!=6:continue
      parts.append(f'GPU {row[0]} {row[1]}% | VRAM {row[2]}/{row[3]} MiB | {row[4]}°C | {row[5]} W')
     gpu=' ; '.join(parts) if parts else 'GPU 정보 조회 불가'
     episode=episode_progress.CURRENT
     display.render(detail_lines(episode,progress,log,start,now,last_progress,activity)+['── GPU · 실시간 / 저장 안 함 ──────────────────',gpu])
     next_tick=now+2
   code=process.wait()
  except BaseException:
   process.terminate()
   try:process.wait(timeout=10)
   except subprocess.TimeoutExpired:process.kill();process.wait()
   raise
  finally:display.clear();process.stdout.close();thread.join(timeout=2)
  say(f'[{"완료" if code==0 else "실패"}] {log.name} | 경과 {duration(time.monotonic()-start)} | 종료코드 {code}')
  if code:raise subprocess.CalledProcessError(code,cmd)
