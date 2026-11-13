import argparse,json,traceback
from pathlib import Path
import cv2
import numpy as np
from capture_qc import run_capture_qc
from run_capture_qc import SCENE_QC_PRESETS
p=argparse.ArgumentParser();p.add_argument('case',type=Path);p.add_argument('out',type=Path);p.add_argument('--scene',choices=SCENE_QC_PRESETS,required=True);a=p.parse_args();a.out.mkdir(parents=True,exist_ok=True)
try:
 result=run_capture_qc(str(a.case),**SCENE_QC_PRESETS[a.scene])
except Exception as e:
 result={'ok':False,'overall':'ERROR','error':str(e),'checks':[]};traceback.print_exc()
result['scene']=a.scene;result['thresholds']=SCENE_QC_PRESETS[a.scene]
result['previews']=[]
for cam in range(1,5):
 cap=cv2.VideoCapture(str(a.case/f'cam_{cam}.mp4'));n=int(cap.get(cv2.CAP_PROP_FRAME_COUNT));images=[]
 for f in sorted(set([0,max(0,n//2),max(0,n-1)])):
  cap.set(cv2.CAP_PROP_POS_FRAMES,f);ok,im=cap.read()
  if not ok:continue
  im=cv2.resize(im,(424,240));cv2.rectangle(im,(0,0),(424,27),(0,0,0),-1);cv2.putText(im,f'CAM {cam} | native frame {f}',(8,19),cv2.FONT_HERSHEY_SIMPLEX,.5,(255,255,255),1);images.append(im)
 cap.release()
 if images:
  name=f'cam_{cam}_rgb.jpg';cv2.imwrite(str(a.out/name),np.hstack(images));result['previews'].append({'camera':cam,'file':name,'kind':'RGB first/middle/last','frames':n})
 depths=sorted((a.case/f'cam_{cam}_depth').glob('*.npy'))
 if depths:
  d=np.load(depths[len(depths)//2],allow_pickle=False);valid=(d>0)&(d<65535);im=np.repeat((np.clip(d.astype(float)/2000,0,1)*255).astype('uint8')[...,None],3,axis=2);im[~valid]=(0,0,255);name=f'cam_{cam}_depth.jpg';cv2.imwrite(str(a.out/name),im);result['previews'].append({'camera':cam,'file':name,'kind':'depth 0–2m, invalid red','source':depths[len(depths)//2].name})
(a.out/'qc.json').write_text(json.dumps(result,ensure_ascii=False,indent=2));print(json.dumps(result,ensure_ascii=False,indent=2))
raise SystemExit(0 if result.get('ok') and result.get('overall')!='FAIL' else 1)
