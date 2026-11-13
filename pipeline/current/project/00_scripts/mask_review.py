"""Separate draft, immutable approval snapshots, and fail-closed downstream access."""
import json,shutil,datetime,uuid,fcntl,os
from pathlib import Path
from contextlib import contextmanager
from PIL import Image
from resume_steps import snapshot
from sam_settings import save
from stage_refs import source,inputs
from mask_paths import episode_root,resolve_input,relocate

@contextmanager
def lock(project):
 with (project/'99_history/pipeline.lock').open('a') as f:
  try:fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)
  except BlockingIOError:raise ValueError('추론 또는 다른 작업이 실행 중입니다. 완료 후 검수하세요.')
  yield

def root(project,ep):
 return episode_root(project,ep)

def prepare_draft(project,ep):
 d=root(project,ep);prepared=source(d);review=d/'review';draft=review/'draft'
 state=json.loads((d/'state.json').read_text())
 if state.get('status')!='mask_review_pending':raise ValueError('SAM3 완료 후 검수할 수 있습니다')
 if draft.exists():
  require_basis(d,draft);return draft
 seed=d/'roi_candidate/mask'
 if not seed.is_dir():raise ValueError('ROI 후보 마스크 없음')
 review.mkdir(exist_ok=True);temp=review/('.draft-'+uuid.uuid4().hex);temp.mkdir()
 try:
  shutil.copytree(seed,temp/'mask',symlinks=False)
  basis={'source_mask':snapshot([seed],d),'prepared':snapshot([prepared/'metadata.json',prepared/'preparation.json'],prepared)}
  save(temp/'basis.json',basis);save(temp/'edits.json',{'events':[]});temp.rename(draft)
 except BaseException:
  if temp.exists():shutil.rmtree(temp)
  raise
 return draft

def require_basis(d,draft):
 basis=json.loads((draft/'basis.json').read_text());prepared=source(d)
 if basis['source_mask']!=snapshot([d/'roi_candidate/mask'],d) or basis['prepared']!=snapshot([prepared/'metadata.json',prepared/'preparation.json'],prepared):raise ValueError('입력 또는 추론 결과가 변경됐습니다. 기존 초안을 자동 혼용하지 않습니다.')

def revoke(d,reason):
 project,d=resolve_input(d)
 pointer=d/'review/approved.json'
 (d/'review').mkdir(exist_ok=True)
 save(d/'review'/('decision-'+uuid.uuid4().hex+'.json'),{'decision':'approval_invalidated_or_held','reason':reason,'time':datetime.datetime.now().astimezone().isoformat()})
 if pointer.exists():
  old=json.loads(pointer.read_text());old.update(revoked_at=datetime.datetime.now().astimezone().isoformat(),reason=reason)
  save(d/'review'/('revoked-'+uuid.uuid4().hex+'.json'),old);pointer.unlink()
 return relocate(project,d.name,'pending')

def write_mask(project,ep,cam,obj,frame,image):
 d=root(project,ep);draft=d/'review/draft';meta=json.loads((source(d)/'metadata.json').read_text())
 if cam not in range(4) or obj not in (0,1) or frame not in range(meta['frame_num']):raise ValueError('Invalid mask coordinates')
 if image.size!=tuple(meta['WH']):raise ValueError('Wrong mask dimensions')
 d=revoke(d,'draft edited');draft=d/'review/draft'
 path=draft/f'mask/{cam}/{obj}/{frame}.png';path.parent.mkdir(parents=True,exist_ok=True);temp=path.with_suffix('.tmp');image.convert('L').point(lambda v:255 if v else 0).save(temp,format='PNG');temp.replace(path)
 events=json.loads((draft/'edits.json').read_text());events['events'].append({'camera':cam,'object':obj,'frame':frame,'time':datetime.datetime.now().astimezone().isoformat()});save(draft/'edits.json',events)

def approve(project,ep,operator,checked,note):
 if not operator.strip() or checked!=[True]*4:raise ValueError('작업자와 4개 카메라 검수 완료 확인이 필요합니다')
 d=root(project,ep);draft=prepare_draft(project,ep);meta=json.loads((source(d)/'metadata.json').read_text());empty=[]
 for c in range(4):
  for o in range(2):
   for f in range(meta['frame_num']):
    path=draft/f'mask/{c}/{o}/{f}.png'
    if not path.exists():empty.append([c,o,f,'missing']);continue
    if path.is_symlink():raise ValueError('Draft masks must be independent files')
    with Image.open(path) as im:
     im.load()
     if im.size!=tuple(meta['WH']):raise ValueError('Mask dimensions invalid')
     if not im.convert('L').getbbox():empty.append([c,o,f,'empty'])
 if empty and not note.strip():raise ValueError(f'누락·빈 마스크 {len(empty)}건이 있습니다. 승인 사유를 기록하세요.')
 version=datetime.datetime.now().strftime('%Y%m%d-%H%M%S')+'-'+uuid.uuid4().hex[:6];dest=d/'review/approved'/version;dest.parent.mkdir(exist_ok=True)
 temp=dest.with_name('.'+version);shutil.copytree(draft,temp);record={'schema_version':2,'episode':ep,'reviewer':{'name':operator.strip()},'status':'approved','operator':operator.strip(),'note':note,'checked_cameras':checked,'approved_at':datetime.datetime.now().astimezone().isoformat(),'exceptions':empty,'files':snapshot([temp/'mask'],temp),'basis':json.loads((draft/'basis.json').read_text())}
 save(temp/'approval.json',record);temp.rename(dest)
 for f in dest.rglob('*'):
  if f.is_file():f.chmod(0o444)
 save(d/'review/approved.json',{'version':version})
 d=relocate(project,ep,'approved');return d/'review/approved'/version

def approved(project,ep):
 d=root(project,ep);pointer=d/'review/approved.json'
 if not pointer.exists():raise ValueError('승인된 마스크가 없습니다. 06_mask_review.py에서 검수·승인하세요.')
 version=json.loads(pointer.read_text())['version']
 if Path(version).name!=version:raise ValueError('Invalid approval version')
 dest=d/'review/approved'/version;r=json.loads((dest/'approval.json').read_text());require_basis(d,d/'review/draft')
 if r.get('status')!='approved' or r['files']!=snapshot([dest/'mask'],dest):raise ValueError('Approved masks changed')
 if r['files']!=snapshot([d/'review/draft/mask'],d/'review/draft'):raise ValueError('승인 이후 초안이 바뀌었습니다. 다시 승인하세요.')
 return dest

def require_tracking_approval(out):
 from tracking_paths import resolve_input
 project,out=resolve_input(out);version=approved(project,out.name);record=json.loads((out/'approval_source.json').read_text())
 if record['version']!=version.name or (out/'mask').resolve()!=(version/'mask').resolve():raise ValueError('추적 입력이 현재 승인본과 다릅니다. 다시 준비하세요.')
 return version/'mask'


def approval_summary(project,ep):
 """Read the current approval only after validating it against the draft."""
 d=root(project,ep)
 if not (d/'review/approved.json').exists():
  history=d/'review/approved'
  return {'status':'needs_approval' if history.exists() and any(history.iterdir()) else 'pending'}
 try:
  dest=approved(project,ep)
  record=json.loads((dest/'approval.json').read_text())
  return {'status':'approved','name':record.get('reviewer',{}).get('name',record['operator']),
          'approved_at':record['approved_at'],'version':dest.name}
 except (OSError,ValueError,KeyError,TypeError) as ex:
  return {'status':'needs_approval','reason':str(ex)}
