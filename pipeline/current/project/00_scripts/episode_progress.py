"""Episode-wide estimate from completed work; unseen stages remain provisional."""
import time,statistics
PLAN=[*(f'baseline_camera_{c}' for c in range(4)),'baseline_preview',*(f'roi_camera_{c}' for c in range(4)),'roi_preview']
def kind(name):return 'roi' if name.startswith('roi_camera') else 'baseline' if name.startswith('baseline_camera') else 'preview'
class Episode:
 def __init__(self):self.start=time.monotonic();self.done=set();self.samples={};self.active=None;self.fraction=0;self.prompts=[];self.stage_start=self.start
 def begin(self,name):self.active=name;self.fraction=0;self.prompts=[];self.stage_start=time.monotonic()
 def finish(self,name,seconds=None):
  self.done.add(name)
  if seconds and seconds>0:self.samples.setdefault(kind(name),[]).append(seconds)
  if self.active==name:self.active=None;self.fraction=0
 def update(self,data):
  if not self.active:return
  prompt=data.get('prompt','')
  if prompt not in self.prompts:self.prompts.append(prompt)
  count=2 if kind(self.active)=='roi' else 3
  fraction=data.get('done',0)/max(1,data.get('frames',1))
  self.fraction=max(self.fraction,min(.95,.1+.8*(len(self.prompts)-1+fraction)/count))
 def status(self):
  base=statistics.median(self.samples.get('baseline',[60]))
  estimates={'baseline':base,'roi':base*2,'preview':base*.5}
  for k,values in self.samples.items():estimates[k]=statistics.median(values)
  if self.active and self.fraction>.15:
   k=kind(self.active);observed=(time.monotonic()-self.stage_start)/self.fraction
   estimates[k]=max(estimates[k],observed) if k not in self.samples else (estimates[k]+observed)/2
  total=sum(estimates[kind(n)] for n in PLAN)
  completed=sum(estimates[kind(n)] for n in self.done if n in PLAN)
  if self.active:completed+=estimates[kind(self.active)]*self.fraction
  ready=bool(self.samples) or self.fraction>.15
  remaining=0
  for n in PLAN:
   if n in self.done:continue
   expected=estimates[kind(n)]
   remaining+=max(expected*(1-self.fraction),expected-(time.monotonic()-self.stage_start),1) if n==self.active else expected
  provisional=any(kind(n) not in self.samples for n in PLAN if n not in self.done)
  return min(100,completed/total*100),remaining if ready else None,provisional,len(self.done)
CURRENT=None
