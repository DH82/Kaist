"""Read-only output listing and RGB preview. Never unpickle output files."""
import json,mimetypes,re,subprocess
from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path
from fastapi import APIRouter,HTTPException
from pydantic import BaseModel,Field
from output_roots import read_registry,register_folder,select_folder,browse_folders,protected
from fastapi.responses import FileResponse,Response
ROOT=Path(__file__).resolve().parent.parent
router=APIRouter(prefix='/worker/outputs')
STAGES={'02_qc':'품질 검사','03_prepared':'전처리','04_masks':'SAM3 마스크','05_tracking':'추적','06_reconstruction':'3D 복원·MANO','07_deliverables':'최종 결과'}
IMAGES={'.png','.jpg','.jpeg','.webp','.gif','.avif'}
VIDEOS={'.mp4','.webm','.mov','.m4v'}
TEXT={'.json','.txt','.log','.csv','.yaml','.yml','.md','.html','.xml','.obj','.ply'}
def cfg():return json.loads((ROOT/'config/gpu_4090_a.json').read_text())
def roots():
 values={'project':{'label':'파이프라인 결과','date_basis':'촬영일','path':Path(cfg()['project_dir']).resolve()},
         'compared':{'label':'비교실험 결과','date_basis':'결과 폴더 생성·수정일','path':(ROOT.parent/'Compared').resolve()}}
 for item in read_registry()['roots']:
  values[item['id']]={'label':item['label'],'date_basis':'폴더 생성·수정일','path':Path(item['path'])}
 return values

def root_items():
 return [{'id':k,'label':v['label'],'path':str(v['path']),'custom':k.startswith('custom_'),
          'available':v['path'].is_dir() and not protected(v['path'].resolve()),'date_basis':v['date_basis']} for k,v in roots().items()]

class FolderRegistration(BaseModel):
 path:str=Field(min_length=1,max_length=4096)
 label:str=Field(default='',max_length=80)
class FolderSelection(BaseModel):
 root:str

@router.get('/roots')
def registered_roots():
 return {'roots':root_items(),'selected':read_registry()['selected'],'pc':'PC 1 · RTX 4090','server':'192.168.200.17'}
@router.post('/roots')
def register_output_folder(data:FolderRegistration):
 entry=register_folder(data.path,data.label)
 return {'registered':entry,'selected':entry['id'],'roots':root_items()}
@router.post('/roots/select')
def select_output_folder(data:FolderSelection):
 return select_folder(data.root,roots())
@router.get('/picker')
def folder_picker(path:str=''):
 return browse_folders(path)

def locate(root,path=''):
 settings=roots()
 if root not in settings:raise HTTPException(404,'등록되지 않은 결과 저장소입니다.')
 base=settings[root]['path'];rel=Path(path)
 if rel.is_absolute() or '..' in rel.parts:raise HTTPException(403,'허용되지 않은 경로입니다.')
 if root=='project' and rel.parts and rel.parts[0] not in STAGES:raise HTTPException(403,'결과 폴더만 조회할 수 있습니다.')
 p=(base/rel).resolve()
 if protected(p):raise HTTPException(403,'설정·비밀 파일은 결과 탐색 대상이 아닙니다.')
 if not p.is_relative_to(base):raise HTTPException(403,'결과 저장소 밖의 링크입니다.')
 if not p.exists():raise HTTPException(404,'파일이 아직 생성되지 않았습니다.')
 return base,p
def kind(p):
 ext=p.suffix.lower()
 return 'image' if ext in IMAGES else 'video' if ext in VIDEOS else 'text' if ext in TEXT else 'pdf' if ext=='.pdf' else 'binary'
def record(base,p):
 try:
  target=p.resolve();allowed=target.is_relative_to(base) and not protected(target)
  stat=p.stat();directory=p.is_dir()
  return {'name':p.name,'path':str(p.relative_to(base)),'directory':directory,'kind':'folder' if directory else kind(p),
          'size':None if directory else stat.st_size,'modified':stat.st_mtime,'accessible':allowed}
 except (OSError,ValueError):return None
@router.get('/catalog')
def catalog(root:str='project'):
 options=roots()
 if root not in options:raise HTTPException(404)
 base=options[root]['path'];groups={}
 def add(date,episode,label,p):
  groups.setdefault(date,{}).setdefault(episode,[]).append({'label':label,'path':str(p.relative_to(base)),'root':root})
 if root=='project':
  for stage,label in STAGES.items():
   parent=base/stage
   scopes=[parent/'pending',parent/'approved'] if stage in ('04_masks','05_tracking') else [parent]
   for scope in scopes:
    if not scope.is_dir():continue
    for ep in sorted(scope.glob('ep_*')):
     if not ep.is_dir():continue
     m=re.match(r'ep_(\d{4})(\d{2})(\d{2})_',ep.name)
     date='-'.join(m.groups()) if m else '날짜 미상'
     suffix=' · '+{'pending':'승인 전','approved':'승인 완료'}[scope.name] if scope.name in ('pending','approved') else ''
     add(date,ep.name,label+suffix,ep)
 else:
  if base.is_dir():
   for experiment in sorted(base.iterdir()):
    if not experiment.is_dir() or experiment.name.startswith('.') or protected(experiment.resolve()):continue
    date=datetime.fromtimestamp(experiment.stat().st_mtime,ZoneInfo('Asia/Seoul')).strftime('%Y-%m-%d')
    episodes=[p for p in experiment.glob('ep_*') if p.is_dir()]
    if episodes:
     for ep in episodes:add(date,ep.name,experiment.name,ep)
    else:add(date,experiment.name,'실험 결과' if root=='compared' else '폴더 열기',experiment)
 return {'roots':root_items(),
         'root':root,'date_basis':options[root]['date_basis'],'groups':[{'date':d,'episodes':[{'episode':ep,'folders':folders} for ep,folders in sorted(eps.items())]} for d,eps in sorted(groups.items(),reverse=True)]}
@router.get('/list')
def listing(root:str,path:str=''):
 base,p=locate(root,path)
 if not p.is_dir():raise HTTPException(400,'폴더를 선택하세요.')
 values=[record(base,c) for c in p.iterdir() if not c.name.startswith('.') and (root!='project' or p!=base or c.name in STAGES)]
 values=sorted([x for x in values if x],key=lambda x:(not x['directory'],x['name']))
 return {'root':root,'path':str(p.relative_to(base)) if p!=base else '', 'entries':values[:2000],'truncated':len(values)>2000,'total':len(values)}
@router.get('/text')
def text(root:str,path:str):
 _,p=locate(root,path)
 if not p.is_file() or kind(p)!='text':raise HTTPException(415,'텍스트 미리보기를 지원하지 않는 형식입니다.')
 with p.open('rb') as f:raw=f.read(262145)
 return {'text':raw[:262144].decode('utf-8',errors='replace'),'truncated':len(raw)>262144,'size':p.stat().st_size}
@router.get('/file')
def file(root:str,path:str,download:bool=False):
 _,p=locate(root,path)
 if not p.is_file():raise HTTPException(400)
 k=kind(p);media=mimetypes.guess_type(p.name)[0] or 'application/octet-stream'
 if k not in ('image','video','pdf'):download=True
 return FileResponse(p,media_type=media,filename=p.name,content_disposition_type='attachment' if download else 'inline',
                     headers={'X-Content-Type-Options':'nosniff','Cache-Control':'no-store'})

@router.get('/preview-info')
def preview_info(episode:str):
 if not re.fullmatch(r'ep_[A-Za-z0-9_]+',episode):raise HTTPException(400)
 c=cfg();raw=(Path(c['raw_root'])/episode).resolve()
 if raw.parent!=Path(c['raw_root']).resolve() or not raw.is_dir():raise HTTPException(404)
 prepared=Path(c['project_dir'])/'03_prepared'/episode
 meta=prepared/'metadata.json'
 return {'episode':episode,'cameras':[1,2,3,4],'source':'prepared' if meta.exists() else 'raw',
         'label':'전처리 RGB · SAM3 입력 이미지' if meta.exists() else 'RAW RGB · 동기화 전 미리보기',
         'frames':json.loads(meta.read_text())['frame_num'] if meta.exists() else None}

@router.get('/preview')
def preview(episode:str,cam:int,position:float=.5):
 if cam not in (1,2,3,4) or not 0<=position<=1:raise HTTPException(400)
 info=preview_info(episode);c=cfg()
 if info['source']=='prepared':
  index=round((info['frames']-1)*position)
  p=Path(c['project_dir'])/'03_prepared'/episode/'color'/str(cam-1)/(str(index)+'.png')
  if not p.is_file():raise HTTPException(404,'해당 카메라 이미지가 없습니다.')
  return FileResponse(p,media_type='image/png',headers={'Cache-Control':'no-store','X-Frame-Index':str(index)})
 video=Path(c['raw_root'])/episode/f'cam_{cam}.mp4'
 if not video.is_file():raise HTTPException(404,'RAW RGB 영상이 없습니다.')
 r=subprocess.run(['/usr/bin/ffprobe','-v','error','-show_entries','format=duration','-of','json',str(video)],capture_output=True,text=True,timeout=15,check=True)
 duration=float(json.loads(r.stdout)['format']['duration']);timestamp=max(0,min(duration-.1,duration*position))
 r=subprocess.run(['/usr/bin/ffmpeg','-v','error','-ss',str(timestamp),'-i',str(video),'-frames:v','1','-vf','scale=848:-2','-f','image2pipe','-vcodec','mjpeg','pipe:1'],capture_output=True,timeout=25,check=True)
 return Response(r.stdout,media_type='image/jpeg',headers={'Cache-Control':'no-store'})
