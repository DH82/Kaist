"""Read-only progress instrumentation; yielded SAM results are unchanged."""
import json,time

def install(module):
 build=module.build_sam3_predictor
 class Observed:
  def __init__(self,p):self._p=p;self.sessions={}
  def __getattr__(self,n):return getattr(self._p,n)
  def handle_request(self,*args,**kwargs):
   req=kwargs.get('request',args[0] if args else {})
   if req.get('type')=='start_session':print('SAM: 영상 로드 '+str(req.get('resource_path')),flush=True)
   result=self._p.handle_request(*args,**kwargs)
   if req.get('type')=='start_session':
    import cv2
    cap=cv2.VideoCapture(req['resource_path']);n=int(cap.get(cv2.CAP_PROP_FRAME_COUNT));cap.release()
    self.sessions[result['session_id']]={'video':req['resource_path'],'frames':n,'prompt':'unknown'}
   elif req.get('type')=='add_prompt':
    state=self.sessions.get(req.get('session_id'),{});state['prompt']=req.get('text','points');print('SAM: 프롬프트 '+str(state['prompt']),flush=True)
   return result
  def handle_stream_request(self,*args,**kwargs):
   req=kwargs.get('request',args[0] if args else {});state=self.sessions.get(req.get('session_id'),{});start=time.monotonic();last=0;seen=set()
   def emit():
    elapsed=time.monotonic()-start;n=state.get('frames',0);done=len(seen)
    eta=elapsed/done*max(0,n-done) if done and n else None
    print('SAM_PROGRESS '+json.dumps({**state,'done':done,'elapsed_s':round(elapsed,1),'eta_s':round(eta,1) if eta is not None else None},ensure_ascii=False),flush=True)
   for result in self._p.handle_stream_request(*args,**kwargs):
    seen.add(result['frame_index']);now=time.monotonic()
    if now-last>=5:emit();last=now
    yield result
   emit()
 def wrapped(*args,**kwargs):return Observed(build(*args,**kwargs))
 module.build_sam3_predictor=wrapped
