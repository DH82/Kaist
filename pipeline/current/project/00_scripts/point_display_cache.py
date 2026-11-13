"""Original valid point cache for stdlib-only desktop rendering. No filtering writes."""
import argparse,json,os,tempfile,fcntl
from pathlib import Path
import numpy as np
from reconstruction_noise import load,sources,sha,locked,write_json,archive

def build(case):
 with (case/'review/.point_cache.lock').open('a') as lockfile:
  fcntl.flock(lockfile,fcntl.LOCK_EX)
  target=case/'review/point_cache';manifest_path=target/'manifest.json';basis=sources(case)
  if manifest_path.exists():
   manifest=json.loads(manifest_path.read_text())
   if manifest.get('sources')==basis and all((target/name).exists() and sha(target/name)==digest for name,digest in manifest['files'].items()):return
  target.parent.mkdir(exist_ok=True)
  with tempfile.TemporaryDirectory(dir=target.parent,prefix='.point-cache-') as tmp:
   temp=Path(tmp);counts={};files={};bounds={}
   dtype=np.dtype([('id','<u4'),('x','<f4'),('y','<f4'),('z','<f4')])
   for group in ('object','hand'):
    _,points,visible=load(case,group);counts[group]=[];lo=np.full(3,np.inf);hi=-lo
    for frame in range(len(points)):
     ids=np.flatnonzero(visible[frame]);xyz=points[frame,ids];records=np.empty(len(ids),dtype=dtype);records['id']=ids
     for i,k in enumerate(('x','y','z')):records[k]=xyz[:,i]
     if len(xyz):lo=np.minimum(lo,xyz.min(0));hi=np.maximum(hi,xyz.max(0))
     name=f'{group}_{frame}.bin';records.tofile(temp/name);files[name]=sha(temp/name);counts[group].append(len(ids))
    bounds[group]=[lo.tolist(),hi.tolist()] if np.isfinite(lo).all() else None
   if sources(case)!=basis:raise ValueError('Inputs changed during cache export')
   write_json(temp/'manifest.json',{'sources':basis,'files':files,'counts':counts,'bounds':bounds,'format':'<Ifff'})
   if target.exists():archive(target.parent,['point_cache'])
   os.replace(temp,target)
 print('Full valid point display cache ready',flush=True)
if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('case',type=Path);build(p.parse_args().case.resolve())
