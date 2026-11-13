"""Tracking episode status buckets and portable downstream references."""
import json,os,uuid
from pathlib import Path
from stage_refs import move
from sam_settings import save

def episode_root(project,ep):
 if not ep.startswith('ep_') or Path(ep).name!=ep:raise ValueError('Invalid episode')
 base=Path(project)/'05_tracking';candidates=[base/'pending'/ep,base/'approved'/ep,base/ep]
 found=[p for p in candidates if p.exists()]
 if len(found)>1:raise ValueError('Duplicate tracking episodes: '+str(found))
 return found[0] if found else candidates[0]
def resolve_input(path):
 path=Path(path).expanduser().absolute()
 if path.parent.name=='05_tracking':project=path.parent.parent
 elif path.parent.name in ('pending','approved') and path.parent.parent.name=='05_tracking':project=path.parent.parent.parent
 else:raise ValueError('05_tracking/pending/<episode> 또는 approved/<episode>를 지정하세요.')
 return project,episode_root(project,path.name)
def relocate(project,ep,bucket):
 if bucket not in ('pending','approved'):raise ValueError('Invalid bucket')
 old=episode_root(project,ep);new=Path(project)/'05_tracking'/bucket/ep
 if old==new:return old
 if not old.exists():raise ValueError('Tracking output missing')
 links=[];records=[]
 own=old/'approval_source.json';own_data=json.loads(own.read_text()) if own.exists() else None
 own_targets={k:(old/own_data[k]).resolve() for k in ('mask','approval') if own_data and k in own_data}
 for base in [Path(project)/n for n in ('06_reconstruction','07_deliverables','99_history')]:
  for folder,dirs,files in os.walk(base,followlinks=False):
   for name in dirs+files:
    p=Path(folder)/name
    if p.is_symlink():
     target=p.resolve()
     if target.is_relative_to(old):links.append((p,target.relative_to(old)))
   if 'tracking_approval.json' in files:
    p=Path(folder)/'tracking_approval.json';data=json.loads(p.read_text());target=(p.parent/data['source']).resolve()
    if target.is_relative_to(old):records.append((p,data,target.relative_to(old)))
 move(old,new)
 if own_data:
  for k,target in own_targets.items():own_data[k]=os.path.relpath(target,new)
  save(new/'approval_source.json',own_data)
 for p,rel in links:
  tmp=p.with_name(p.name+'.move-'+uuid.uuid4().hex);tmp.symlink_to(os.path.relpath(new/rel,p.parent),target_is_directory=(new/rel).is_dir());tmp.replace(p)
 for p,data,rel in records:data['source']=os.path.relpath(new/rel,p.parent);save(p,data)
 return new
