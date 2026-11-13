"""Browser mask editing adapter; all actual writes use native mask_review.write_mask."""
import argparse,base64,hashlib,io,json,sys,uuid
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo
from PIL import Image
ROOT=Path(__file__).resolve().parent
sys.path.insert(0,str(ROOT.parent/'review'))
from review_backend import context,fingerprint

def sha(data):return hashlib.sha256(data).hexdigest()
def idle(episode):
 computing={'prepare_raw','sam3','preflight','track','prepare_reconstruction','reconstruct','hands','validate','publish'}
 for job in (ROOT/'jobs').glob('*'):
  if not (job/'request.json').exists() or not (job/'status.json').exists():continue
  req=json.loads((job/'request.json').read_text());status=json.loads((job/'status.json').read_text())
  if req.get('episode')==episode and req.get('stage') in computing and status.get('state') in ('queued','running','interrupted'):
   raise ValueError('이 에피소드의 계산이 진행 중이거나 상태 확인이 필요합니다. 완료 후 마스크를 수정하세요.')
def target(review,project,ep,cam,obj,frame):
 if any(type(v) is not int for v in (cam,obj,frame)):raise ValueError('Invalid mask coordinates')
 draft=review.prepare_draft(project,ep);d=review.root(project,ep)
 meta=json.loads((review.source(d)/'metadata.json').read_text())
 if cam not in range(4) or obj not in (0,1) or frame not in range(meta['frame_num']):raise ValueError('Invalid mask coordinates')
 p=draft/f'mask/{cam}/{obj}/{frame}.png'
 if not p.is_file() or p.is_symlink():raise ValueError('편집할 독립 마스크 파일이 없습니다.')
 return d,draft,p,meta

def frame_data(review,project,ep,cam,obj,frame):
 with review.lock(project):
  d,draft,p,meta=target(review,project,ep,cam,obj,frame);raw=p.read_bytes()
  with Image.open(io.BytesIO(raw)) as image:
   if image.size!=tuple(meta['WH']):raise ValueError('Wrong mask dimensions')
   out=io.BytesIO();image.convert('L').point(lambda v:255 if v else 0).save(out,format='PNG')
  return {'episode':ep,'cam':cam,'obj':obj,'frame':frame,'width':meta['WH'][0],'height':meta['WH'][1],
          'basis':fingerprint(review,draft),'file_sha256':sha(raw),'mask_png':base64.b64encode(out.getvalue()).decode(),
          'approval':review.approval_summary(project,ep)}

def decode_mask(data,expected_size):
 value=data.get('mask_png','')
 if not isinstance(value,str) or len(value)>2500000:raise ValueError('마스크 파일 크기가 너무 큽니다.')
 try:raw=base64.b64decode(value,validate=True)
 except Exception:raise ValueError('Invalid mask encoding')
 with Image.open(io.BytesIO(raw)) as im:
  if im.format!='PNG' or im.size!=tuple(expected_size):raise ValueError('원본과 같은 해상도의 PNG 마스크가 필요합니다.')
  im.load()
  if 'A' in im.getbands() and im.getchannel('A').getextrema()!=(255,255):raise ValueError('불투명한 이진 마스크가 필요합니다.')
  mask=im.convert('L');colors=mask.getcolors(3)
  if colors is None or any(v not in (0,255) for count,v in colors):raise ValueError('마스크는 배경 0과 영역 255로 저장해야 합니다.')
  return mask.copy()

def save_frame(review,project,ep,data,operator):
 if not str(operator).strip():raise ValueError('검수자 정보가 필요합니다.')
 with review.lock(project):
  idle(ep)
  cam,obj,frame=data.get('cam'),data.get('obj'),data.get('frame')
  d,draft,p,meta=target(review,project,ep,cam,obj,frame)
  if not data.get('basis') or data['basis']!=fingerprint(review,draft):raise ValueError('편집 중 다른 수정이 저장됐습니다. 다시 불러온 뒤 수정하세요.')
  before=p.read_bytes()
  if data.get('file_sha256')!=sha(before):raise ValueError('마스크 파일이 변경됐습니다. 다시 불러오세요.')
  mask=decode_mask(data,meta['WH'])
  with Image.open(io.BytesIO(before)) as old:
   if old.convert('L').tobytes()==mask.tobytes():
    return {'changed':False,'approval':review.approval_summary(project,ep),'message':'변경된 픽셀이 없어 기존 승인 상태를 유지했습니다.'}
  event=uuid.uuid4().hex;history=d/'review/edit_history'/event;history.mkdir(parents=True)
  (history/'before.png').write_bytes(before)
  audit={'event':event,'episode':ep,'camera':cam,'object':obj,'frame':frame,'operator':str(operator).strip(),
         'time':datetime.now(ZoneInfo('Asia/Seoul')).isoformat(),'before_sha256':sha(before),'status':'prepared'}
  (history/'edit.json').write_text(json.dumps(audit,indent=2,ensure_ascii=False))
  review.write_mask(project,ep,cam,obj,frame,mask)
  d=review.root(project,ep);saved=d/f'review/draft/mask/{cam}/{obj}/{frame}.png'
  history=d/'review/edit_history'/event
  audit.update(status='saved',after_sha256=sha(saved.read_bytes()))
  (history/'edit.json').write_text(json.dumps(audit,indent=2,ensure_ascii=False))
  return {'changed':True,'episode':ep,'cam':cam,'obj':obj,'frame':frame,'approval':review.approval_summary(project,ep),
          'message':'수정 초안을 저장했습니다. 기존 승인은 해제됐으며 다시 검수·승인해야 합니다. 후속 추적·복원 결과도 새 승인본 기준으로 다시 실행해야 합니다.'}

if __name__=='__main__':
 parser=argparse.ArgumentParser();parser.add_argument('action',choices=['frame','save']);parser.add_argument('--config',required=True);parser.add_argument('--episode',required=True)
 parser.add_argument('--cam',type=int);parser.add_argument('--obj',type=int);parser.add_argument('--frame',type=int);args=parser.parse_args()
 try:
  review,project,ep=context(args.config,args.episode)
  if args.action=='frame':value=frame_data(review,project,ep,args.cam,args.obj,args.frame)
  else:
   payload=json.load(sys.stdin);value=save_frame(review,project,ep,payload,payload.get('operator',''))
  print(json.dumps(value,ensure_ascii=False))
 except Exception as e:print(str(e),file=sys.stderr);sys.exit(1)
