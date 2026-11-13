"""Runtime validation and compact local-view data export for stages 10–17."""
import argparse,json,pickle
from pathlib import Path
import numpy as np

def trajectories(case,name,key,T):
 with (case/name).open('rb') as f:d=pickle.load(f)
 p=np.asarray(d[key+'_points']);v=np.asarray(d[key+'_visibilities']).astype(bool)
 if p.ndim!=3 or p.shape[0]!=T or p.shape[2]!=3 or p.shape[1]==0 or v.shape!=p.shape[:2] or not np.isfinite(p[v]).all():raise ValueError('Invalid 3D trajectories: '+name)
 if not v.any():raise ValueError('No visible 3D points: '+name)
 return p,v

def validate(case,stage,T):
 import trimesh
 if stage=='10':
  from PIL import Image
  im=Image.open(case/'shape/masked_image.png').convert('RGBA');a=np.asarray(im)[:,:,3]
  if not (a>0).any() or (a==255).all():raise ValueError('Invalid shape alpha')
 elif stage in ('11','16'):
  path=case/('shape/object.glb' if stage=='11' else 'shape/matching/final_mesh.glb');m=trimesh.load(path,force='mesh')
  if not len(m.vertices) or not len(m.faces) or not np.isfinite(m.vertices).all():raise ValueError('Invalid mesh')
 elif stage=='12':
  for frame in range(T):
   with np.load(case/f'pcd/{frame}.npz') as d:
    if d['points'].shape[0]!=4 or d['points'].shape[-1]!=3 or d['masks'].shape!=d['points'].shape[:-1]:raise ValueError('Invalid RGB-D cloud')
    if not np.isfinite(d['points'][d['masks'].astype(bool)]).all():raise ValueError('Nonfinite depth points')
 elif stage=='13':
  d=pickle.load((case/'processed_masks.pkl').open('rb'))
  if len(d)!=T or any(len(d[i])!=4 for i in range(T)):raise ValueError('Incomplete processed masks')
 elif stage in ('14','15','17'):trajectories(case,{'14':'track_process_data.pkl','15':'hand_track_data.pkl','17':'final_data.pkl'}[stage],'hand' if stage=='15' else 'object',T)

def export(case,T):
 import trimesh
 data={'episode':case.name,'frames':T,'points':{},'meshes':{},'note':'Measured points in meters. Mesh alignment is static at frame 0. Hand points are not MANO.'};report={}
 for name,key in [('track_process_data.pkl','object'),('hand_track_data.pkl','hand')]:
  p,v=trajectories(case,name,key,T);data['points'][key]=[p[i,v[i]][::max(1,int(np.ceil(v[i].sum()/1500)))].round(6).tolist() for i in range(T)]
  report[key]={'frames':T,'tracks':p.shape[1],'visible_min':int(v.sum(1).min()),'visible_max':int(v.sum(1).max())}
 for name,path in [('generated','shape/object.glb'),('aligned','shape/matching/final_mesh.glb')]:
  mesh=trimesh.load(case/path,force='mesh');faces=mesh.faces;ids=np.unique(faces);mapping=np.full(len(mesh.vertices),-1,dtype=int);mapping[ids]=np.arange(len(ids))
  data['meshes'][name]={'vertices':mesh.vertices[ids].round(6).tolist(),'faces':mapping[faces].tolist()}
  report[name]={'vertices':len(mesh.vertices),'faces':len(mesh.faces),'watertight':bool(mesh.is_watertight),'bounds':mesh.bounds.tolist(),'display_faces':len(faces)}
 from sam_settings import save
 (case/'review').mkdir(exist_ok=True);save(case/'review/view3d.json',data);save(case/'review/validation.json',report)
 print(json.dumps(report))

def main():
 p=argparse.ArgumentParser();p.add_argument('action',choices=['check','validate','export']);p.add_argument('--case',type=Path);p.add_argument('--stage');p.add_argument('--frames',type=int);a=p.parse_args()
 if a.action=='check':
  import torch,open3d,trimesh,cv2
  from TRELLIS.trellis.pipelines import TrellisImageTo3DPipeline
  if not torch.cuda.is_available():raise ValueError('CUDA GPU unavailable')
  print(json.dumps({'gpu':torch.cuda.get_device_name(0),'torch':torch.__version__}));return
 if a.action=='validate':validate(a.case,a.stage,a.frames)
 else:export(a.case,a.frames)
if __name__=='__main__':main()
