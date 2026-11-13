"""Validate preprocessing and render complete mask review videos."""
import argparse,json,sys,subprocess
from pathlib import Path
import cv2,numpy as np

def validate(case):
 meta=json.loads((case/'metadata.json').read_text());n=meta['frame_num']
 if n<=0:raise ValueError('No frames remain')
 mapping=json.loads((case/'source_frames.json').read_text())
 if len(mapping['frames'])!=n or [r['output_idx'] for r in mapping['frames']]!=list(range(n)):raise ValueError('Source mapping/frame count mismatch')
 for row in mapping['frames']:
  if len(row['cameras'])!=4 or {c['camera'] for c in row['cameras']}!=set(range(4)) or any(c['raw_idx']<0 for c in row['cameras']):raise ValueError('Invalid source mapping camera/index')
 for cam in range(4):
  for kind,ext in [('color','png'),('depth','npy')]:
   files=list((case/kind/str(cam)).glob('*.'+ext))
   if {p.stem for p in files}!={str(i) for i in range(n)}:raise ValueError(f'Incomplete {kind} camera {cam}')
   for p in files:
    a=cv2.imread(str(p)) if kind=='color' else np.load(p,allow_pickle=False)
    if a is None or a.shape[:2]!=(meta['WH'][1],meta['WH'][0]):raise ValueError(f'Invalid frame: {p}')
 return n

def trim(case,source,out,trim_exposure=False):
 sys.path.insert(0,str(source/'data_process'));from trim_exposure import detect_trim,apply_trim
 n=validate(case);detected_start,detected_end,middle=detect_trim(str(case))
 start,end=(detected_start,detected_end) if trim_exposure else (0,0)
 report={'before':n,'trim_start':int(start),'trim_end':int(end),'after':n-int(start)-int(end),
         'policy':'trim_edges_keep_middle' if trim_exposure else 'keep_all_synced_frames',
         'detected_trim_start':int(detected_start),'detected_trim_end':int(detected_end),
         'middle_bad':list(map(int,middle)),
         'middle_bad_after_trim':[int(f)-int(start) for f in middle],
         'warnings':['Exposure candidates reported; automatic removal disabled'] if not trim_exposure else []}
 (out/'trim.json').write_text(json.dumps(report,indent=2))
 print('Exposure policy:',report['policy'],'frames',n,'->',report['after'],flush=True)
 if trim_exposure:
  if report['after']<=0:raise ValueError('Trim would remove all frames')
  apply_trim(str(case),start,end)
 validate(case)

def masks(case,out):
 n=validate(case);report={'frames':n,'cameras':{}}
 for cam in range(4):
  errors=[];w,h=json.loads((case/'metadata.json').read_text())['WH'];raw=out/f'cam_{cam+1}_overlay.mp4';writer=cv2.VideoWriter(str(raw),cv2.VideoWriter_fourcc(*'mp4v'),30,(w,h))
  if not writer.isOpened():raise ValueError('Preview writer failed')
  for f in range(n):
   im=cv2.imread(str(case/f'color/{cam}/{f}.png')).astype(float)
   for obj,color in [(0,[70,220,30]),(1,[190,45,245])]:
    p=case/f'mask/{cam}/{obj}/{f}.png';mask=cv2.imread(str(p),0) if p.exists() else None
    if mask is None or not np.any(mask):errors.append({'frame':f,'object':obj,'issue':'missing_or_empty'});continue
    if mask.shape!=im.shape[:2]:raise ValueError('Mask dimensions mismatch')
    m=mask>0;im[m]=im[m]*.55+np.array(color)*.45
   writer.write(im.astype('uint8'))
  writer.release();report['cameras'][str(cam+1)]={'exceptions':errors}
  subprocess.run(['ffmpeg','-v','error','-y','-i',str(raw),'-an','-c:v','libvpx-vp9','-deadline','realtime','-cpu-used','8','-crf','30','-b:v','0',str(out/f'cam_{cam+1}_mask.webm')],check=True)
  raw.unlink()
 (out/'mask_validation.json').write_text(json.dumps(report,indent=2))
 if any(v['exceptions'] for v in report['cameras'].values()):print('Mask exceptions recorded; human review required')
if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('action',choices=['validate','trim','masks']);p.add_argument('case',type=Path);p.add_argument('--out',type=Path);p.add_argument('--source',type=Path);p.add_argument('--trim-exposure',action='store_true',help='Explicitly remove leading/trailing exposure candidates; default keeps all synchronized frames');a=p.parse_args()
 if a.action=='validate':print(validate(a.case))
 elif a.action=='trim':trim(a.case,a.source,a.out,a.trim_exposure)
 else:masks(a.case,a.out)
