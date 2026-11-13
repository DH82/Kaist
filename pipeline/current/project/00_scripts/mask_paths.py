"""Stable episode IDs with pending/approved folders and movable relative references."""
import json,os,uuid
from pathlib import Path
from stage_refs import move

def episode_root(project,ep):
 if not ep.startswith('ep_') or Path(ep).name!=ep:raise ValueError('Invalid episode')
 base=Path(project)/'04_masks'
 candidates=[base/'pending'/ep,base/'approved'/ep,base/ep]
 found=[p for p in candidates if p.exists()]
 if len(found)>1:raise ValueError('Duplicate mask episode folders: '+str(found))
 return found[0] if found else candidates[0]

def resolve_input(path):
 path=Path(path).expanduser().absolute()
 if path.parent.name=='04_masks':project=path.parent.parent
 elif path.parent.name in ('pending','approved') and path.parent.parent.name=='04_masks':project=path.parent.parent.parent
 else:raise ValueError('입력: 04_masks/pending/<episode> 또는 04_masks/approved/<episode>')
 return project,episode_root(project,path.name)

def relocate(project,ep,bucket):
 if bucket not in ('pending','approved'):raise ValueError('Invalid mask status folder')
 old=episode_root(project,ep);new=Path(project)/'04_masks'/bucket/ep
 if old==new:return old
 if not old.exists():raise ValueError('Mask episode does not exist: '+str(old))
 links=[];records=[]
 # References from current and archived downstream outputs must follow this move.
 for base in [Path(project)/'05_tracking',Path(project)/'06_reconstruction',Path(project)/'07_deliverables',Path(project)/'99_history']:
  if not base.exists():continue
  for folder,dirs,files in os.walk(base,followlinks=False):
   for name in dirs+files:
    path=Path(folder)/name
    if path.is_symlink():
     target=path.resolve()
     if target.is_relative_to(old):links.append((path,target.relative_to(old)))
   if 'approval_source.json' in files:
    path=Path(folder)/'approval_source.json';data=json.loads(path.read_text());changes={}
    for key in ('mask','approval'):
     if key in data:
      target=(path.parent/data[key]).resolve()
      if target.is_relative_to(old):changes[key]=target.relative_to(old)
    if changes:records.append((path,data,changes))
 move(old,new)
 for path,rel in links:
  tmp=path.with_name(path.name+'.relocate-'+uuid.uuid4().hex)
  tmp.symlink_to(os.path.relpath(new/rel,path.parent),target_is_directory=(new/rel).is_dir());tmp.replace(path)
 for path,data,changes in records:
  for key,rel in changes.items():data[key]=os.path.relpath(new/rel,path.parent)
  tmp=path.with_suffix('.tmp');tmp.write_text(json.dumps(data,ensure_ascii=False,indent=2));tmp.replace(path)
 if (Path(project)/'overview.html').exists():
  import pipeline
  pipeline.overview(Path(project))
 return new
