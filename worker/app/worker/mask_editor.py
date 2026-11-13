"""Browser mask editing adapter; all actual writes use native mask_review.write_mask."""
import argparse,base64,hashlib,io,json,sys,uuid
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo
from PIL import Image
ROOT=Path(__file__).resolve().parent
sys.path.insert(0,str(ROOT.parent/'review'))
from review_backend import context,fingerprint,episode_lock

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
 # SAM3가 물체를 찾지 못한 프레임은 마스크 파일이 없습니다(승인 시 '누락'으로 기록). 빈 마스크에서 새로 만들 수 있게 허용하고,
 # 심볼릭 링크 등 독립 파일이 아닌 경우만 거부합니다.
 if p.is_symlink() or (p.exists() and not p.is_file()):raise ValueError('편집할 독립 마스크 파일이 없습니다.')
 return d,draft,p,meta
MISSING='missing'  # 파일이 없는 마스크의 기준값 (file_sha256 자리)

def frame_data(review,project,ep,cam,obj,frame):
 with episode_lock(project,ep):
  d,draft,p,meta=target(review,project,ep,cam,obj,frame)
  out=io.BytesIO()
  if p.exists():
   raw=p.read_bytes();file_sha=sha(raw)
   with Image.open(io.BytesIO(raw)) as image:
    if image.size!=tuple(meta['WH']):raise ValueError('Wrong mask dimensions')
    image.convert('L').point(lambda v:255 if v else 0).save(out,format='PNG')
  else:
   file_sha=MISSING;Image.new('L',tuple(meta['WH']),0).save(out,format='PNG')
  return {'missing':file_sha==MISSING,'episode':ep,'cam':cam,'obj':obj,'frame':frame,'width':meta['WH'][0],'height':meta['WH'][1],
          'basis':fingerprint(review,draft),'file_sha256':file_sha,'mask_png':base64.b64encode(out.getvalue()).decode(),
          'approval':review.approval_summary(project,ep),'sam_prompt':sam_prompt(review,d)}

def sam_prompt(review,d):
 # 이 에피소드 SAM3 실행에 쓴 물체 이름(마법봉 기본값)
 try:return str(json.loads((review.source(d)/'sam3_settings.json').read_text())['settings'].get('prompt') or '')
 except (OSError,ValueError,KeyError,TypeError):return ''

def wand_source(review,project,ep,cam,frame):
 # 마법봉 입력: 검수 중인 프레임의 원본 RGB 경로. 계산 중인 에피소드는 거부합니다.
 with episode_lock(project,ep):
  idle(ep)
  d,draft,p,meta=target(review,project,ep,cam,0,frame)
  rgb=review.source(d)/f'color/{cam}/{frame}.png'
  if not rgb.is_file():raise ValueError('원본 RGB 프레임이 없습니다.')
  return {'rgb':str(rgb),'width':meta['WH'][0],'height':meta['WH'][1]}

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
 with episode_lock(project,ep):
  idle(ep)
  cam,obj,frame=data.get('cam'),data.get('obj'),data.get('frame')
  d,draft,p,meta=target(review,project,ep,cam,obj,frame)
  if not data.get('basis') or data['basis']!=fingerprint(review,draft):raise ValueError('편집 중 다른 수정이 저장됐습니다. 다시 불러온 뒤 수정하세요.')
  before=p.read_bytes() if p.exists() else None
  if data.get('file_sha256')!=(sha(before) if before is not None else MISSING):raise ValueError('마스크 파일이 변경됐습니다. 다시 불러오세요.')
  mask=decode_mask(data,meta['WH'])
  if before is None:
   unchanged=not mask.getbbox()  # 없던 마스크에 아무것도 칠하지 않음
  else:
   with Image.open(io.BytesIO(before)) as old:unchanged=old.convert('L').tobytes()==mask.tobytes()
  if unchanged:
   return {'changed':False,'approval':review.approval_summary(project,ep),'message':'변경된 픽셀이 없어 기존 승인 상태를 유지했습니다.'}
  event=uuid.uuid4().hex;history=d/'review/edit_history'/event;history.mkdir(parents=True)
  if before is not None:(history/'before.png').write_bytes(before)
  audit={'event':event,'episode':ep,'camera':cam,'object':obj,'frame':frame,'operator':str(operator).strip(),
         'time':datetime.now(ZoneInfo('Asia/Seoul')).isoformat(),'before_sha256':sha(before) if before is not None else MISSING,'status':'prepared'}
  (history/'edit.json').write_text(json.dumps(audit,indent=2,ensure_ascii=False))
  review.write_mask(project,ep,cam,obj,frame,mask)
  d=review.root(project,ep);saved=d/f'review/draft/mask/{cam}/{obj}/{frame}.png'
  history=d/'review/edit_history'/event
  audit.update(status='saved',after_sha256=sha(saved.read_bytes()))
  (history/'edit.json').write_text(json.dumps(audit,indent=2,ensure_ascii=False))
  return {'changed':True,'episode':ep,'cam':cam,'obj':obj,'frame':frame,'approval':review.approval_summary(project,ep),
          'message':'수정 초안을 저장했습니다. 기존 승인은 해제됐으며 다시 검수·승인해야 합니다. 후속 추적·복원 결과도 새 승인본 기준으로 다시 실행해야 합니다.'}

if __name__=='__main__':
 parser=argparse.ArgumentParser();parser.add_argument('action',choices=['frame','save','wand_source']);parser.add_argument('--config',required=True);parser.add_argument('--episode',required=True)
 parser.add_argument('--cam',type=int);parser.add_argument('--obj',type=int);parser.add_argument('--frame',type=int);args=parser.parse_args()
 try:
  review,project,ep=context(args.config,args.episode)
  if args.action=='frame':value=frame_data(review,project,ep,args.cam,args.obj,args.frame)
  elif args.action=='wand_source':value=wand_source(review,project,ep,args.cam,args.frame)
  else:
   payload=json.load(sys.stdin);value=save_frame(review,project,ep,payload,payload.get('operator',''))
  print(json.dumps(value,ensure_ascii=False))
 except Exception as e:print(str(e),file=sys.stderr);sys.exit(1)
