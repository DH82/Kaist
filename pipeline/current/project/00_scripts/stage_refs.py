"""Relative input references, portable project moves, archive-safe links."""
import os,json,shutil
from pathlib import Path
INPUTS=['color','depth','metadata.json','calibrate.pkl','world_reorient_M.npy','source_frames.json']
def link(path,target):
 if not target.exists():raise ValueError('Reference target missing: '+str(target))
 if path.is_symlink():
  if path.resolve()!=target.resolve():raise ValueError('Reference target mismatch: '+str(path))
  return
 if path.exists():raise ValueError('Migrate existing input copy first: '+str(path))
 path.parent.mkdir(parents=True,exist_ok=True);path.symlink_to(os.path.relpath(target,path.parent),target_is_directory=target.is_dir())
def inputs(dest,prepared):
 for name in INPUTS:
  if (dest/name).exists() or (dest/name).is_symlink():raise ValueError('Remove old input aliases with migration first: '+str(dest/name))
 manifest={'layout':'manifest_input_v2','input':os.path.relpath(prepared,dest),'input_files':{n:os.path.relpath(prepared/n,dest) for n in INPUTS if (prepared/n).exists()},'note':'Inputs live in prepared; model adapters use disposable views without copying.'}
 (dest/'manifest.json').write_text(json.dumps(manifest,indent=2))

def move(path,target):
 """Rebase links when archiving; preserve references internal to the moved tree."""
 path=Path(path);target=Path(target);pairs=[];manifests=[]
 if path.is_dir() and not path.is_symlink():
  for m in path.rglob('manifest.json'):
   try:
    data=json.loads(m.read_text())
    if data.get('layout')=='manifest_input_v2':manifests.append((m.relative_to(path),data,(m.parent/data['input']).resolve()))
   except (ValueError,OSError):pass
 for f in ([path] if path.is_symlink() else path.rglob('*') if path.is_dir() else []):
  if f.is_symlink():pairs.append((f.relative_to(path),f.resolve()))
 target.parent.mkdir(parents=True,exist_ok=True);shutil.move(str(path),str(target))
 for rel,data,input_target in manifests:
  m=target/rel
  mapped=target/input_target.relative_to(path) if input_target.is_relative_to(path) else input_target
  data['input']=os.path.relpath(mapped,m.parent);data['input_files']={n:os.path.relpath(mapped/n,m.parent) for n in data.get('input_files',{})}
  m.write_text(json.dumps(data,indent=2))
 for rel,old_target in pairs:
  new=target/rel
  mapped=target/old_target.relative_to(path) if old_target.is_relative_to(path) else old_target
  new.unlink();new.symlink_to(os.path.relpath(mapped,new.parent),target_is_directory=mapped.is_dir())

from contextlib import contextmanager
import tempfile

def source(case):
 record=json.loads((case/'manifest.json').read_text())
 target=(case/record['input']).resolve()
 if not (target/'metadata.json').is_file():raise ValueError('Prepared input missing: '+str(target))
 return target

@contextmanager
def command_view(command):
 """Legacy model scripts see a disposable input/output view, never copied data."""
 cmd=list(command);case=None;position=None;baseline=False
 if '--base_path' in cmd and '--case_name' in cmd:
  case=Path(cmd[cmd.index('--base_path')+1])/cmd[cmd.index('--case_name')+1];baseline=True
 elif '--case' in cmd:
  position=cmd.index('--case')+1;case=Path(cmd[position])
 else:
  for i,arg in enumerate(cmd[:-1]):
   if arg in ('validate','masks'):
    position=i+1;case=Path(cmd[position]);break
 if case is None:
  yield cmd;return
 if case.parent.name=='06_reconstruction' or (case/'tracking_approval.json').exists():
  from tracking_review import require_reconstruction_input
  require_reconstruction_input(case)
 if not (case/'manifest.json').is_file():
  yield cmd;return
 prepared=source(case);case=case.resolve()
 with tempfile.TemporaryDirectory(prefix='wfm-stage-view-') as tmp:
  view=Path(tmp)/case.name;view.mkdir()
  for name in INPUTS:
   if (prepared/name).exists():link(view/name,prepared/name)
  (case/'mask').mkdir(exist_ok=True);link(view/'mask',case/'mask')
  if (case/'tracking_approval.json').exists():
   for group in ('cotracker','cotracker_hand'):link(view/group,case/group)
  if baseline:cmd[cmd.index('--base_path')+1]=str(Path(tmp))
  else:cmd[position]=str(view)
  yield cmd
