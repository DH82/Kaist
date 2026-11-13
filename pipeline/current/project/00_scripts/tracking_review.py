"""Human tracking decisions bound to exact validated outputs and upstream approval."""
import datetime,json,uuid,hashlib,os
from pathlib import Path
from zoneinfo import ZoneInfo
from resume_steps import snapshot
from sam_settings import save
from mask_review import require_tracking_approval

from tracking_paths import resolve_input,relocate

def resolve(path):
 _,case=resolve_input(path)
 if not case.is_dir():raise ValueError('추적 폴더가 없습니다: '+str(case))
 return case

def base_basis(case):
 case=resolve(case)
 approved_mask=require_tracking_approval(case)
 contract=json.loads((case/'_resume/contract.json').read_text())
 from stage_refs import source
 prepared=source(case)
 from tracking_contract import current_contract
 current=current_contract(prepared,approved_mask.parent,Path(__file__).resolve().parent)
 if contract!=current:raise ValueError('추적 입력/코드/모델 계약이 변경되었습니다. 07_track.py --new-attempt로 기존 결과를 보관하고 다시 실행하세요.')
 if contract['input']!=snapshot([prepared/'metadata.json',prepared/'color',prepared/'source_frames.json'],prepared):raise ValueError('전처리 입력이 변경되었습니다. 재추적하세요.')
 groups=[f'{g}_camera_{c}' for g in ('cotracker','cotracker_hand') for c in range(4)]+['tracking_review']
 expected={f'{g}_camera_{c}':{f'{g}/{c}.npz'} for g in ('cotracker','cotracker_hand') for c in range(4)}
 expected['tracking_review']={'review/index.html','review/tracking_report.json'}|{f'review/{g}_{suffix}' for g in ('cotracker','cotracker_hand') for suffix in ('review.mp4','poster.jpg','contact.jpg')}
 files={}
 for name in groups:
  r=json.loads((case/'_resume'/f'{name}.json').read_text())
  if set(r['files'])!=expected[name]:raise ValueError('완료 기록이 불완전합니다: '+name)
  current=snapshot([case/f for f in sorted(expected[name])],case)
  if r['files']!=current:raise ValueError('추적 결과/검수 영상이 변경되었습니다. 07_track.py를 다시 실행하세요: '+name)
  files.update(current)
 return {'contract':contract,'files':files}

def basis(case):
 case=resolve(case);base=base_basis(case)
 from tracking_qc import verified
 qc=verified(case,base)
 if qc is not None:
  if qc['status']=='blocked':raise ValueError('자동 QC 구조 오류: qc/report.json 확인 후 재추적하세요. 사람 승인으로 우회할 수 없습니다.')
  return {**base,'qc':snapshot([case/'qc/report.json',case/'qc/receipt.json'],case)}
 return base

def fingerprint(value):return hashlib.sha256(json.dumps(value,sort_keys=True).encode()).hexdigest()
def read_decision(case):
 case=resolve(case)
 p=case/'review/decision.json'
 return json.loads(p.read_text()) if p.exists() else {'status':'pending'}
def status(case):
 case=resolve(case)
 r=read_decision(case)
 try:
  b=basis(case);token=fingerprint(b)
  if r.get('status')=='approved' and r.get('basis')!=b:r={**r,'status':'needs_review','reason':'승인 후 결과가 변경됨'}
  return {k:v for k,v in r.items() if k!='basis'}|{'token':token,'ready':True}
 except (ValueError,OSError,KeyError) as ex:
  return {k:v for k,v in r.items() if k!='basis'}|{'status':'needs_review','ready':False,'reason':str(ex),'token':None}

def decide(case,decision,reviewer,checked,note,token,mode="human"):
 case=resolve(case)
 if decision not in ('approved','held'):raise ValueError('Invalid decision')
 reviewer=reviewer.strip();note=note.strip()
 if not reviewer:raise ValueError('검수자 이름을 입력하세요.')
 from tracking_qc import verified
 qc=verified(case,base_basis(case));expected=len(qc['events']) if qc else 8
 if mode not in ('human','automatic'):raise ValueError('Invalid review mode')
 if mode=='automatic' and (not qc or qc['status']!='passed' or decision!='approved'):raise ValueError('자동 통과 조건 불충족')
 if qc and qc['status']=='blocked':raise ValueError('구조 오류는 승인할 수 없습니다.')
 if len(checked)!=expected or any(type(v) is not bool for v in checked):raise ValueError('QC 이상 구간 확인 값 오류')
 if decision=='approved' and qc and qc['events'] and not note:raise ValueError('이상 후보를 허용한 사유를 기록하세요.')
 if decision=='approved' and not all(checked):raise ValueError('표시된 이상 후보 구간을 모두 확인하세요.')
 if decision=='held' and not note:raise ValueError('보류 사유와 카메라/프레임 구간을 기록하세요.')
 b=basis(case)
 if token!=fingerprint(b):raise ValueError('화면을 연 이후 결과가 변경됐습니다. 다시 열어 검수하세요.')
 now=datetime.datetime.now(ZoneInfo('Asia/Seoul')).isoformat();record={'schema_version':2,'approval_type':mode,'episode':case.name,'status':decision,'reviewer':reviewer,'decided_at':now,'approved_at':now if decision=='approved' else None,'checked':checked,'note':note,'basis':b,'id':uuid.uuid4().hex}
 history=case/'review/decisions';history.mkdir(exist_ok=True)
 save(history/(record['id']+'.json'),record);save(case/'review/decision.json',record)
 project,_=resolve_input(case);relocate(project,case.name,'approved' if decision=='approved' else 'pending')
 return {k:v for k,v in record.items() if k!='basis'}

def require_approved(case):
 case=resolve(case);r=read_decision(case)
 if r.get('status')!='approved':raise ValueError('추적 검수 승인이 필요합니다. 09_review_tracking.py를 실행하세요.')
 if r.get('basis')!=basis(case):raise ValueError('승인 이후 추적/입력이 변경되었습니다. 다시 검수·승인하세요.')
 return r

def prepare_reconstruction(case):
 case=resolve(case);project,_=resolve_input(case)
 r=require_approved(case);out=project/'06_reconstruction'/case.name
 from stage_refs import inputs,link,source
 if (out/'tracking_approval.json').exists():
  old=json.loads((out/'tracking_approval.json').read_text())
  if old['decision_id']!=r['id']:
   allowed={'manifest.json','tracking_approval.json','cotracker','cotracker_hand','mask'}
   if any(x.name not in allowed for x in out.iterdir()) or any(not (out/g).is_symlink() for g in ('cotracker','cotracker_hand','mask')):
    raise ValueError('기존 3D 결과는 다른 승인본입니다. 기존 작업을 보관한 뒤 새로 준비하세요.')
   from stage_refs import move
   target=project/'99_history/06_reconstruction'/case.name/uuid.uuid4().hex
   old_source=(out/old['source']).resolve();move(out,target)
   old['source']=os.path.relpath(old_source,target);save(target/'tracking_approval.json',old)
 out.mkdir(parents=True,exist_ok=True);inputs(out,source(case))
 for group in ('cotracker','cotracker_hand','mask'):link(out/group,case/group)
 save(out/'tracking_approval.json',{'decision_id':r['id'],'source':os.path.relpath(case,out),'reviewer':r['reviewer'],'approved_at':r['approved_at']})
 return out

def require_reconstruction_input(out):
 ref=json.loads((out/'tracking_approval.json').read_text());r=require_approved(out/ref['source'])
 if ref['decision_id']!=r['id']:raise ValueError('3D 입력 승인 버전이 변경됨')
 case=(out/ref['source']).resolve()
 from stage_refs import source
 if source(out)!=source(case):raise ValueError('3D 전처리 입력 참조가 변경됨')
 for group in ('cotracker','cotracker_hand','mask'):
  if (out/group).resolve()!=(case/group).resolve():raise ValueError('3D 입력 참조가 승인본과 다름: '+group)
 return case

def invalidate(case,reason):
 case=resolve(case)
 r=read_decision(case)
 if r.get('status')!='approved':
  project,_=resolve_input(case);return relocate(project,case.name,'pending')
 r={**r,'status':'needs_review','reason':reason,'invalidated_at':datetime.datetime.now(ZoneInfo('Asia/Seoul')).isoformat()}
 history=case/'review/decisions';history.mkdir(exist_ok=True)
 save(history/(uuid.uuid4().hex+'.json'),r);save(case/'review/decision.json',r)
 project,_=resolve_input(case);return relocate(project,case.name,'pending')
