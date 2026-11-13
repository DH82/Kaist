"""Atomic completion receipts. Existence alone never proves completion."""
import hashlib,json,shutil,datetime,time,os
import episode_progress
from pathlib import Path

def digest(p):
 with p.open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()
def snapshot(paths,root):
 result={}
 for p in paths:
  if not p.exists():raise ValueError('Missing checkpoint output: '+str(p))
  for f in ([Path(folder)/name for folder,dirs,files in os.walk(p,followlinks=True) for name in sorted(files)] if p.is_dir() else [p]):
   if f.is_file():result[str(f.relative_to(root))]=digest(f)
 return result

def step(root,name,action,outputs,quiet=False):
 receipt=root/'_resume'/f'{name}.json';paths=[root/p for p in outputs]
 if receipt.exists():
  try:
   previous=json.loads(receipt.read_text())
   if previous['files']==snapshot(paths,root):
    if episode_progress.CURRENT:episode_progress.CURRENT.finish(name,previous.get('seconds'))
    if not quiet:print('[재개] 완료 구간 건너뜀: '+name,flush=True)
    return
  except (OSError,ValueError,KeyError):pass
 preserve(receipt,root)
 if not quiet:print('[실행] '+name,flush=True)
 if episode_progress.CURRENT:episode_progress.CURRENT.begin(name)
 started=time.monotonic();action();seconds=time.monotonic()-started
 record={'files':snapshot(paths,root),'seconds':seconds};receipt.parent.mkdir(exist_ok=True);tmp=receipt.with_suffix('.tmp');tmp.write_text(json.dumps(record,indent=2));tmp.replace(receipt)
 if episode_progress.CURRENT:episode_progress.CURRENT.finish(name,seconds)

def preserve(path,root):
 if path.exists():
  target=root/'_resume'/'partial'/datetime.datetime.now().strftime('%Y%m%d-%H%M%S-%f')/path.name
  target.parent.mkdir(parents=True);__import__('stage_refs').move(path,target)

def contract(dest,prepared,settings,codepaths):
 episode_progress.CURRENT=episode_progress.Episode()
 current={'settings':settings,'input':snapshot([prepared/'color',prepared/'depth',prepared/'metadata.json',prepared/'preparation.json',prepared/'source_frames.json',prepared/'calibrate.pkl'],prepared),'code':{str(p.name):digest(p) for p in codepaths}}
 path=dest/'_resume/contract.json'
 if path.exists():
  old=json.loads(path.read_text())
  if old!=current:
   compatibility=Path(__file__).parent/'resume_ui_compat.json'
   allowed=json.loads(compatibility.read_text()) if compatibility.exists() else {}
   patched=json.loads(json.dumps(old));old_hash=patched.get('code',{}).get('pipeline.py')
   if allowed.get(old_hash)==current['code'].get('pipeline.py'):patched['code']['pipeline.py']=current['code']['pipeline.py']
   code_compat=Path(__file__).parent/'resume_code_compat.json'
   code_allowed=json.loads(code_compat.read_text()) if code_compat.exists() else {}
   for name,mapping in code_allowed.items():
    prior=patched.get('code',{}).get(name)
    if mapping.get(prior)==current.get('code',{}).get(name):patched['code'][name]=current['code'][name]
   if patched!=current:raise ValueError('Input/settings/code changed; use --new-attempt to preserve and restart')
   shutil.copy2(path,path.with_name('contract.before_settings_ui.json'))
   temp=path.with_suffix('.tmp');temp.write_text(json.dumps(current,indent=2));temp.replace(path)
 else:
  path.parent.mkdir(exist_ok=True);temp=path.with_suffix('.tmp');temp.write_text(json.dumps(current,indent=2));temp.replace(path)
