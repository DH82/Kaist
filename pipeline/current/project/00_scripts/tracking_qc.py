"""QC receipts, auto-pass, and downstream preparation; no NumPy dependency."""
import json,subprocess
from pathlib import Path
from resume_steps import digest
from sam_settings import save
HERE=Path(__file__).resolve().parent

def identity():return {n:digest(HERE/n) for n in ('tracking_qc_worker.py','tracking_qc_policy.json')}
def verified(case,base):
 from tracking_review import fingerprint
 if not (case/'qc/report.json').exists():return None
 receipt=json.loads((case/'qc/receipt.json').read_text());report=json.loads((case/'qc/report.json').read_text())
 if receipt!={'source_token':fingerprint(base),'code':identity(),'report_sha256':digest(case/'qc/report.json')} or report['source_token']!=fingerprint(base):raise ValueError('자동 QC가 오래되었거나 변경됨: 07_track.py를 다시 실행하세요.')
 if report['status'] not in ('passed','needs_review','blocked'):raise ValueError('Invalid QC status')
 if report['status']=='passed' and (report['events'] or report['errors']):raise ValueError('Invalid QC pass')
 return report

def run(case,python,cwd,env):
 from tracking_review import resolve,base_basis,fingerprint,decide,read_decision,require_approved,prepare_reconstruction,status
 case=resolve(case);base=base_basis(case)
 try:report=verified(case,base)
 except (ValueError,OSError,KeyError):report=None
 if report is None:
  from tracking_review import invalidate
  case=invalidate(case,'자동 QC 실행/기준 변경')
  print('자동 QC: 8개 추적 결과와 승인 마스크 검사 중…',flush=True)
  subprocess.run([python,str(HERE/'tracking_qc_worker.py'),'--case',str(case),'--policy',str(HERE/'tracking_qc_policy.json'),'--source-token',fingerprint(base)],cwd=cwd,env=env,check=True)
  save(case/'qc/receipt.json',{'source_token':fingerprint(base),'code':identity(),'report_sha256':digest(case/'qc/report.json')})
  report=verified(case,base)
 old=read_decision(case)
 if old.get('status')=='approved':
  try:require_approved(case)
  except (ValueError,OSError,KeyError):pass
  else:return resolve(case),report,prepare_reconstruction(resolve(case))
 # Explicit human holds must never be silently overwritten by a clean rerun.
 if report['status']=='passed' and old.get('status')!='held':
  decide(case,'approved','Automatic QC',[],'자동 QC 이상 후보 없음',status(case)['token'],mode='automatic')
  case=resolve(case);return case,report,prepare_reconstruction(case)
 return resolve(case),report,None
