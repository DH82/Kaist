"""Independent CPU MANO rotation and parameters-to-world-mesh validator."""
from pathlib import Path
import sys,pickle,json,io
import numpy as np
class CPUUnpickler(pickle.Unpickler):
 def find_class(self,module,name):
  if module=='torch.storage' and name=='_load_from_bytes':
   import torch
   return lambda b:torch.load(io.BytesIO(b),map_location='cpu',weights_only=False)
  return super().find_class(module,name)
def read(path):
 with Path(path).open('rb') as f:return CPUUnpickler(f).load()
def arr(x):return x.detach().cpu().numpy() if hasattr(x,'detach') else np.asarray(x)
def validate_fit_integrity(case,folder,mono,frames=None,sides=('right',),strict=True):
 import torch
 case,folder,mono=map(Path,(case,folder,mono));sys.path.insert(0,str(mono/'data_process'))
 from models.mano_wrapper import MANO
 model=MANO(model_path=str(mono/'data_process/Hamba/_DATA/data/mano'),gender='neutral',num_hand_joints=15,create_body_pose=False).cpu()
 mesh=read(folder/'hand_reg.pkl');params=read(folder/'hand_reg_params.pkl');refs=read(folder/'hand_reg_cidx.pkl');cal=read(case/'calibrate.pkl')
 expected=set(range(frames)) if isinstance(frames,int) else set(frames if frames is not None else mesh)
 if set(mesh)!=expected or set(params)!=expected:raise ValueError('Incomplete MANO frame/parameter sequence')
 orth=det=error=0.;rows=[]
 for f in sorted(expected):
  if set(mesh[f])!=set(sides) or set(params[f])!=set(sides):raise ValueError('MANO side mismatch')
  for side in sides:
   p=params[f][side]
   for key in ['global_orient','hand_pose','betas','transl']:
    if not np.isfinite(arr(p[key])).all():raise ValueError('Nonfinite MANO parameter '+key)
   v=arr(mesh[f][side]);rots=np.concatenate([arr(p[k]).reshape(-1,3,3) for k in ['global_orient','hand_pose']])
   if v.shape!=(778,3) or not np.isfinite(v).all() or not np.isfinite(rots).all():raise ValueError('Invalid/nonfinite MANO data')
   oe=float(abs(rots.transpose(0,2,1)@rots-np.eye(3)).max());de=float(abs(np.linalg.det(rots)-1).max())
   with torch.no_grad():local=model(**{k:torch.as_tensor(arr(p[k]),dtype=torch.float32) for k in ['global_orient','hand_pose','betas']},pose2rot=False).vertices[0].numpy()
   if side=='left':local[:,0]*=-1
   local=local+arr(p['transl']);ref=refs[1 if side=='right' else 0];world=(np.c_[local,np.ones(778)]@arr(cal[ref]).T)[:,:3]
   if not np.isfinite(world).all():raise ValueError('Nonfinite MANO forward mesh')
   fe=float(abs(world-v).max());orth=max(orth,oe);det=max(det,de);error=max(error,fe);rows.append({'frame':f,'side':side,'orthogonality_error':oe,'det_error':de,'forward_error_m':fe})
 passed=orth<1e-4 and det<1e-4 and error<1e-5
 result={'frames':len(expected),'sides':list(sides),'SO3_pass':orth<1e-4 and det<1e-4,'orthogonality_max':orth,'det_error_max':det,'params_forward_max_error_m':error,'pass':passed,'accuracy_approved':False,'per_frame':rows}
 if strict and not passed:raise ValueError('MANO structural validation failed: '+json.dumps({k:v for k,v in result.items() if k!='per_frame'}))
 return result
