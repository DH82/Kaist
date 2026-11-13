"""Receipt-based timing estimates; estimates never determine completion."""
import json, math, time
from statistics import mean
from resume_steps import snapshot

CURRENT = None

def duration(seconds):
 seconds=max(0,round(seconds));h,seconds=divmod(seconds,3600);m,s=divmod(seconds,60)
 return f'{h}시간 {m}분 {s}초' if h else f'{m}분 {s}초'

class Progress:
 def __init__(self, root, clock=time.monotonic):
  self.clock=clock;self.started=clock();self.active=None;self.active_start=None
  self.jobs=[f'{g}_camera_{c}' for g in ('cotracker','cotracker_hand') for c in range(4)]+['tracking_review']
  self.done=set();self.times={}
  for name in self.jobs:
   try:
    receipt=json.loads((root/'_resume'/f'{name}.json').read_text())
    seconds=float(receipt['seconds'])
    if math.isfinite(seconds) and seconds>0:self.times[name]=seconds
    if receipt['files'] and receipt['files']==snapshot([root/p for p in receipt['files']],root):self.done.add(name)
   except (OSError,ValueError,KeyError,TypeError):pass
  # Any rerun invalidates the old review video.
  if any(n not in self.done for n in self.jobs[:-1]):self.done.discard('tracking_review')
 def begin(self,name):
  self.active=name;self.active_start=self.clock();self.done.discard(name)
 def finish(self,name):
  if self.active==name:
   self.times[name]=max(.001,self.clock()-self.active_start);self.active=None
  self.done.add(name)
 def estimate(self,name):
  if name in self.times:return self.times[name]
  if name=='tracking_review':return None
  group=name.rsplit('_camera_',1)[0]
  values=[v for k,v in self.times.items() if k.startswith(group+'_camera_')]
  if not values:values=[v for k,v in self.times.items() if k!='tracking_review']
  return mean(values) if values else None
 def text(self):
  pending=[n for n in self.jobs if n not in self.done];remaining=0;unknown=[];over=False
  for name in pending:
   value=self.estimate(name)
   if value is None:unknown.append(name);continue
   if name==self.active:
    elapsed=self.clock()-self.active_start
    if elapsed>=value:over=True
    value=max(0,value-elapsed)
   remaining+=value
  if not pending:eta='완료'
  elif over:eta='예상 초과 · 현재 작업 완료 후 재계산'
  elif any(n!='tracking_review' for n in unknown):eta='계산 중 (첫 카메라 완료 후 추정)'
  elif unknown:eta=f'추적 약 {duration(remaining)} + 검수 영상 시간 측정 전' if remaining else '검수 영상 생성 중 · 시간 측정 전'
  else:eta=f'약 {duration(remaining)}'
  count=len(self.done);bar='■'*count+'·'*(9-count)
  return f'[{bar}] 완료 {count}/9 ({count/9:.0%}, 작업 수 기준) | 경과 {duration(self.clock()-self.started)} | 남은 시간: {eta}'

class Panel:
 def __init__(self,progress,episode):
  from live_log import Display
  self.progress=progress;self.episode=episode;self.display=Display();self.label='입력 준비 완료';self.log='';self.gpu='GPU 조회 중'
 def refresh(self,label=None,log=None,telemetry=False):
  from live_log import gpu_rows,bar
  if label is not None:self.label=label
  if log is not None:self.log=str(log)
  if telemetry:
   rows=gpu_rows()
   self.gpu=' / '.join(f'GPU {r[0]}: {r[1]}% · VRAM {r[2]}/{r[3]} MiB · {r[4]}°C' for r in rows if len(r)>=5) or 'GPU 조회 불가'
  p=self.progress;parts=p.text().split(' | ')
  def state(name):return '✓완료' if name in p.done else '▶진행' if name==p.active else '·대기'
  lines=[f'── CoTracker3 / {self.episode} ──',f'{bar(100*len(p.done)/9)}  완료 {len(p.done)}/9 (작업 수 기준)',*parts[1:],
         '── 카메라별 상태 ──']
  for group,title in [('cotracker','물체'),('cotracker_hand','손')]:
   lines.append(title+': '+' / '.join(f'C{c+1} {state(group+"_camera_"+str(c))}' for c in range(4)))
  lines += ['검수 영상: '+state('tracking_review'),'현재: '+self.label,'── GPU · 실시간 ──',self.gpu,'로그: '+self.log]
  self.display.render(lines)
 def close(self):self.display.clear()
