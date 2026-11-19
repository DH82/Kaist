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

def track_source(review,project,ep,cam,frame):
 # 추적 일괄 교체 입력: 이 카메라의 원본 RGB 폴더·프레임 수. 계산 중인 에피소드는 거부합니다.
 with episode_lock(project,ep):
  idle(ep)
  d,draft,p,meta=target(review,project,ep,cam,0,frame)
  color=review.source(d)/f'color/{cam}'
  if not (color/f'{frame}.png').is_file():raise ValueError('원본 RGB 프레임이 없습니다.')
  return {'color_dir':str(color),'frames':int(meta['frame_num']),'fps':meta.get('fps',30),'width':meta['WH'][0],'height':meta['WH'][1]}

def apply_track(review,project,ep,data,operator):
 """추적 일괄 교체: 구간 [from,to]의 이 카메라·대상 마스크를 추적 결과(dir/<프레임>.png, 없으면 빈 마스크)로 바꿉니다.
 기존 마스크가 있어도 교체합니다. 바뀐 프레임의 이전 파일은 edit_history/<event>/before/ 에 보관하고 이력 한 건으로 남깁니다."""
 if not str(operator).strip():raise ValueError('검수자 정보가 필요합니다.')
 cam,obj,a,b=data.get('cam'),data.get('obj'),data.get('from'),data.get('to')
 src=Path(str(data.get('dir') or ''))
 if not src.is_dir() or src.resolve().parent!=(ROOT/'wand_tracks').resolve():raise ValueError('추적 결과 폴더가 잘못되었습니다.')
 with episode_lock(project,ep):
  idle(ep)
  d,draft,p,meta=target(review,project,ep,cam,obj,a)
  target(review,project,ep,cam,obj,b)
  if a>b:raise ValueError('구간이 잘못되었습니다.')
  W,H=meta['WH'];event=uuid.uuid4().hex;history=None;changed=[];same=0;emptied=[]
  for f in range(a,b+1):
   d=review.root(project,ep);path=d/f'review/draft/mask/{cam}/{obj}/{f}.png'
   if path.is_symlink() or (path.exists() and not path.is_file()):raise ValueError('편집할 독립 마스크 파일이 없습니다: 프레임 '+str(f+1))
   new_file=src/f'{f}.png'
   if new_file.exists():
    with Image.open(new_file) as im:
     if im.size!=(W,H):raise ValueError('추적 마스크 크기가 원본과 다릅니다.')
     mask=im.convert('L').point(lambda v:255 if v else 0)
   else:
    mask=Image.new('L',(W,H),0)
   before=path.read_bytes() if path.exists() else None
   if before is None:
    unchanged=not mask.getbbox()
   else:
    with Image.open(io.BytesIO(before)) as old:unchanged=old.convert('L').point(lambda v:255 if v else 0).tobytes()==mask.tobytes()
   if unchanged:same+=1;continue
   if history is None:history=d/'review/edit_history'/event;(history/'before').mkdir(parents=True)
   if before is not None:(history/'before'/f'{f}.png').write_bytes(before)
   review.write_mask(project,ep,cam,obj,f,mask);changed.append(f)
   if not mask.getbbox():emptied.append(f)
  if history is not None:
   d=review.root(project,ep);history=d/'review/edit_history'/event
   audit={'event':event,'kind':'track_replace','episode':ep,'camera':cam,'object':obj,'frames':[a,b],'start_frame':data.get('start'),
          'points':data.get('points'),'text':data.get('text'),'changed_frames':changed,'emptied_frames':emptied,'unchanged':same,
          'operator':str(operator).strip(),'time':datetime.now(ZoneInfo('Asia/Seoul')).isoformat(),'status':'saved'}
   (history/'edit.json').write_text(json.dumps(audit,indent=2,ensure_ascii=False))
  return {'changed':len(changed),'unchanged':same,'emptied':len(emptied),'approval':review.approval_summary(project,ep)}

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

def write_one(review,project,ep,cam,obj,frame,file_sha,mask,operator):
 # 호출하는 쪽에서 episode_lock과 idle()을 잡은 상태여야 합니다. 저장할 때마다 승인 해제로 폴더가 바뀔 수 있어 대상을 매번 다시 찾습니다.
 d,draft,p,meta=target(review,project,ep,cam,obj,frame)
 before=p.read_bytes() if p.exists() else None
 if file_sha!=(sha(before) if before is not None else MISSING):raise ValueError('마스크 파일이 변경됐습니다. 다시 불러오세요.')
 if mask.size!=tuple(meta['WH']):raise ValueError('원본과 같은 해상도의 PNG 마스크가 필요합니다.')
 if before is None:
  unchanged=not mask.getbbox()  # 없던 마스크에 아무것도 칠하지 않음
 else:
  with Image.open(io.BytesIO(before)) as old:unchanged=old.convert('L').tobytes()==mask.tobytes()
 if unchanged:
  return {'changed':False,'cam':cam,'obj':obj,'frame':frame,'message':'변경된 픽셀이 없어 기존 승인 상태를 유지했습니다.'}
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
 return {'changed':True,'episode':ep,'cam':cam,'obj':obj,'frame':frame,
         'message':'수정 초안을 저장했습니다. 기존 승인은 해제됐으며 다시 검수·승인해야 합니다. 후속 추적·복원 결과도 새 승인본 기준으로 다시 실행해야 합니다.'}

def save_frame(review,project,ep,data,operator):
 if not str(operator).strip():raise ValueError('검수자 정보가 필요합니다.')
 with episode_lock(project,ep):
  idle(ep)
  cam,obj,frame=data.get('cam'),data.get('obj'),data.get('frame')
  d,draft,p,meta=target(review,project,ep,cam,obj,frame)
  if not data.get('basis') or data['basis']!=fingerprint(review,draft):raise ValueError('편집 중 다른 수정이 저장됐습니다. 다시 불러온 뒤 수정하세요.')
  result=write_one(review,project,ep,cam,obj,frame,data.get('file_sha256'),decode_mask(data,meta['WH']),operator)
  result['approval']=review.approval_summary(project,ep)
  return result

def save_many(review,project,ep,data,operator):
 # 한 프레임의 여러 카메라 수정을 요청 한 번으로 저장합니다(화면에서 카메라마다 조회+저장을 왕복하지 않도록).
 # 마스크마다 불러올 때의 file_sha256이 지금 파일과 같아야 하며, 하나라도 다르면 아무것도 쓰지 않습니다.
 if not str(operator).strip():raise ValueError('검수자 정보가 필요합니다.')
 items=data.get('masks')
 if not isinstance(items,list) or not 1<=len(items)<=8 or any(not isinstance(x,dict) for x in items):raise ValueError('Invalid mask request')
 with episode_lock(project,ep):
  idle(ep);ready=[];seen=set()
  for x in items:
   cam,obj,frame=x.get('cam'),x.get('obj'),x.get('frame')
   d,draft,p,meta=target(review,project,ep,cam,obj,frame)
   if (cam,obj,frame) in seen:raise ValueError('Invalid mask request')
   seen.add((cam,obj,frame))
   before=p.read_bytes() if p.exists() else None
   if x.get('file_sha256')!=(sha(before) if before is not None else MISSING):raise ValueError(f'카메라 {cam+1} 마스크가 다른 곳에서 변경됐습니다. 수정을 버리고 다시 불러오세요.')
   ready.append((cam,obj,frame,x.get('file_sha256'),decode_mask(x,meta['WH'])))
  results=[write_one(review,project,ep,*item,operator) for item in ready]
  d=review.root(project,ep)
  return {'results':results,'changed':any(r['changed'] for r in results),'basis':fingerprint(review,d/'review/draft'),'approval':review.approval_summary(project,ep)}

def mask_areas(review,project,ep,data):
 # 프레임 구간의 마스크 픽셀 수를 한 번에 돌려줍니다(-1 = 파일 없음). '빈 마스크 찾기'·'빈 마스크 일괄 적용'이 프레임마다 PNG를 받지 않도록 합니다.
 obj,a,b=data.get('obj'),data.get('from'),data.get('to');cams=data.get('cams',[0,1,2,3])
 if any(type(v) is not int for v in (obj,a,b)) or not isinstance(cams,list) or not cams or any(type(c) is not int for c in cams) or a>b:raise ValueError('Invalid mask coordinates')
 with episode_lock(project,ep):
  d,draft,p,meta=target(review,project,ep,cams[0],obj,a)
  if b not in range(meta['frame_num']) or any(cam not in range(4) for cam in cams):raise ValueError('Invalid mask coordinates')
 def area(path):
  if not path.is_file():return -1
  with Image.open(path) as im:return sum(im.convert('L').histogram()[1:])
 return {'obj':obj,'from':a,'to':b,'areas':{str(cam):[area(draft/f'mask/{cam}/{obj}/{f}.png') for f in range(a,b+1)] for cam in cams}}

if __name__=='__main__':
 parser=argparse.ArgumentParser();parser.add_argument('action',choices=['frame','save','wand_source','track_source','apply_track']);parser.add_argument('--config',required=True);parser.add_argument('--episode',required=True)
 parser.add_argument('--cam',type=int);parser.add_argument('--obj',type=int);parser.add_argument('--frame',type=int);args=parser.parse_args()
 try:
  review,project,ep=context(args.config,args.episode)
  if args.action=='frame':value=frame_data(review,project,ep,args.cam,args.obj,args.frame)
  elif args.action=='wand_source':value=wand_source(review,project,ep,args.cam,args.frame)
  elif args.action=='track_source':value=track_source(review,project,ep,args.cam,args.frame)
  elif args.action=='apply_track':
   payload=json.load(sys.stdin);value=apply_track(review,project,ep,payload,payload.get('operator',''))
  else:
   # 저장 요청(POST /edit)의 op로 묶음 저장·픽셀 수 조회를 구분합니다(워커 서비스 경로를 늘리지 않습니다).
   payload=json.load(sys.stdin);op=payload.get('op') if isinstance(payload,dict) else None
   if op=='areas':value=mask_areas(review,project,ep,payload)
   elif op=='save_many':value=save_many(review,project,ep,payload,payload.get('operator',''))
   else:value=save_frame(review,project,ep,payload,payload.get('operator',''))
  print(json.dumps(value,ensure_ascii=False))
 except Exception as e:print(str(e),file=sys.stderr);sys.exit(1)
