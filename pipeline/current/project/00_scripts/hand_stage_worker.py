"""Runtime preflight, validation and four-camera MANO preview generation."""
import argparse,json,pickle,subprocess
from pathlib import Path
import numpy as np

def check(mono):
 import torch,cv2,mediapipe,pytorch3d,smplx,detectron2
 if not torch.cuda.is_available():raise ValueError('CUDA GPU unavailable')
 required=['data/mano_faces.pkl','data_process/Hamba/ckpts/hamba/checkpoints/hamba.ckpt','data_process/Hamba/_DATA/data/mano/MANO_RIGHT.pkl','data_process/Hamba/_DATA/data/mano/MANO_LEFT.pkl','data_process/Hamba/_DATA/vitpose_ckpts/vitpose+_huge/wholebody.pth']
 for name in required:
  if not (mono/name).is_file():raise ValueError('Model missing: '+str(mono/name))
 print('GPU: '+torch.cuda.get_device_name(0)+'; HaMBA/MANO core files available')

def validate_hamba(folder,T,sides,cameras=range(4),require_initial=True):
 import torch,cv2
 availability={side:[] for side in sides}
 for cam in cameras:
  d=torch.load(folder/str(cam)/'results.pt',map_location='cpu',weights_only=False)
  for f in range(T):
   if cv2.imread(str(folder/str(cam)/f'{f}_all.jpg')) is None:raise ValueError(f'Incomplete HaMBA camera {cam} frame {f}')
  for key,pred in d.items():
   if pred is None:continue
   f,side=map(int,key.split('_'))
   if not 0<=f<T:raise ValueError('HaMBA frame out of range')
   v=np.asarray(pred['pred_vertices'].detach().cpu() if torch.is_tensor(pred['pred_vertices']) else pred['pred_vertices'])
   if v.shape[-2:]!=(778,3) or not np.isfinite(v).all():raise ValueError('Invalid HaMBA vertices')
   if f==0 and side in sides:availability[side].append(cam)
 for side,cams in availability.items():
  if require_initial and not cams:raise ValueError(f'No frame-0 prediction for selected hand {side}. Confirm hand side / capture; fitting stopped.')
 return availability

class CPUUnpickler(pickle.Unpickler):
 def find_class(self,module,name):
  if module=='torch.storage' and name=='_load_from_bytes':
   import torch,io
   return lambda b:torch.load(io.BytesIO(b),map_location='cpu',weights_only=False)
  return super().find_class(module,name)

def read_fit(path):
 with path.open('rb') as f:return CPUUnpickler(f).load()

def validate_fit(case,T,sides):
 from mano_integrity import validate_fit_integrity
 report=validate_fit_integrity(case,case/'hand_v1',Path.cwd(),T,['right' if x==1 else 'left' for x in sides])
 (case/'hand_v1/integrity.json').write_text(json.dumps(report,indent=2))
 d=read_fit(case/'hand_v1/hand_reg.pkl');params=read_fit(case/'hand_v1/hand_reg_params.pkl')
 expected={'right' if s==1 else 'left' for s in sides}
 if set(d)!=set(range(T)) or set(params)!=set(range(T)):raise ValueError('Incomplete MANO frame sequence')
 for f,row in d.items():
  if set(row)!=expected:raise ValueError('MANO hand side mismatch')
  for side,v in row.items():
   p=np.asarray(v)
   if p.shape!=(778,3) or not np.isfinite(p).all():raise ValueError('Invalid MANO vertices')
 return d

def preview(out,case,T,sides):
 import cv2
 validate_fit(case,T,sides);folder=out/'review';folder.mkdir(exist_ok=True);frames=folder/'frames';frames.mkdir(exist_ok=True)
 debug=out/'_runtime/debug/_hand_reg_mp/hand_v1'/case.name
 proc=subprocess.Popen(['ffmpeg','-nostdin','-v','error','-y','-f','rawvideo','-pix_fmt','bgr24','-s','1272x720','-r','15','-i','-','-an','-c:v','libx264','-crf','20','-pix_fmt','yuv420p',str(folder/'mano_review.mp4')],stdin=subprocess.PIPE)
 try:
  for f in range(T):
   im=cv2.imread(str(debug/f'{f:03d}_1.png'))
   if im is None or im.shape[0]%4:raise ValueError(f'Missing four-camera fit projection: frame {f}')
   height=im.shape[0]//4;tiles=[]
   for cam in range(4):
    tile=cv2.resize(im[cam*height:(cam+1)*height],(636,360));cv2.rectangle(tile,(0,0),(636,30),(20,20,20),-1);cv2.putText(tile,f'MANO | CAM {cam+1} | frame {f}',(8,22),cv2.FONT_HERSHEY_SIMPLEX,.6,(255,255,255),1);tiles.append(tile)
   mosaic=np.vstack([np.hstack(tiles[:2]),np.hstack(tiles[2:])]);proc.stdin.write(mosaic.tobytes());cv2.imwrite(str(frames/f'{f:06d}.jpg'),mosaic)
  proc.stdin.close()
  if proc.wait():raise ValueError('Preview video encode failed')
 except BaseException:
  proc.kill();proc.wait();raise
 (folder/'validation.json').write_text(json.dumps({'frames':T,'sides':['right' if s==1 else 'left' for s in sides],'vertices_per_hand':778,'checks':'complete frames, expected sides, finite vertices, SO3, CPU parameters-to-mesh reproduction, all four-camera projection images','algorithm':json.loads((out/'settings.json').read_text()).get('algorithm','legacy-mano'),'integrity':json.loads((case/'hand_v1/integrity.json').read_text()),'accuracy_approved':False},indent=2))

if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('action',choices=['check','hamba','camera','fit','preview']);p.add_argument('--camera',type=int);p.add_argument('--mono',type=Path);p.add_argument('--out',type=Path);p.add_argument('--case',type=Path);p.add_argument('--frames',type=int);p.add_argument('--side',choices=['right','both'],default='right');a=p.parse_args();sides=[1] if a.side=='right' else [0,1]
 if a.action=='check':check(a.mono)
 elif a.action=='hamba':print(validate_hamba(a.out/'hamba'/a.case.name,a.frames,sides))
 elif a.action=='camera':validate_hamba(a.out/'hamba'/a.case.name,a.frames,sides,[a.camera],False)
 elif a.action=='fit':validate_fit(a.case,a.frames,sides)
 else:preview(a.out,a.case,a.frames,sides)
