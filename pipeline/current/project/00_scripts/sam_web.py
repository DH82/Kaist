import json,re,os,sys,fcntl,subprocess,datetime,uuid,time,getpass
from pathlib import Path
from sam_settings import review_page,save

def episode(project,ep):
 if not isinstance(ep,str) or not re.fullmatch(r'ep_[A-Za-z0-9_-]+',ep):raise ValueError('Invalid episode')
 p=(project/'03_prepared'/ep).resolve()
 if p.parent!=(project/'03_prepared').resolve() or not (p/'preparation.json').is_file():raise ValueError('Prepared episode missing')
 return p

def status(project,ep):
 p=episode(project,ep);rec=json.loads((p/'sam3_settings.json').read_text()) if (p/'sam3_settings.json').exists() else {}
 contract=__import__('mask_paths').episode_root(project,ep)/'_resume/contract.json'
 locked=json.loads(contract.read_text())['settings'] if contract.exists() else None
 defaults=json.loads((project/'episodes.json').read_text()).get(ep,{}) if (project/'episodes.json').exists() else {}
 data={'settings':locked or rec.get('settings',defaults),'locked':bool(locked)}
 job=p/'sam3_job.json'
 if job.exists():
  data['job']=json.loads(job.read_text());log=p/'sam3_console.log'
  if log.exists():
   with log.open('rb') as f:f.seek(max(0,log.stat().st_size-16000));data['log']=f.read().decode(errors='replace')
  if data['job'].get('status')=='running':
   try:os.kill(data['job']['pid'],0)
   except ProcessLookupError:data['job']['status']='interrupted'
 summary=__import__('mask_paths').episode_root(project,ep)/'completion.json'
 if summary.exists():data['completion']=json.loads(summary.read_text())
 from live_log import gpu_rows
 data['gpu']=gpu_rows()
 return data

def launch(project,data):
 raise ValueError('SAM3 실행은 터미널의 05_sam3.py만 지원합니다. QC에서 프롬프트를 저장하세요.')
