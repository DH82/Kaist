"""Full-frame baseline -> isolated ROI hand candidate. Never approves or replaces baseline.
ROI derives only from the full-frame hand masks (no ground-truth leakage).
"""
import argparse,importlib.util,json,shutil,subprocess,time,hashlib,os
from pathlib import Path
import numpy as np
from PIL import Image

def bounding_roi(mask,pad,width,height):
 ys,xs=np.where(mask)
 if not len(xs):return None
 dx=max(16,int((xs.max()-xs.min()+1)*pad));dy=max(16,int((ys.max()-ys.min()+1)*pad))
 return [max(0,int(xs.min())-dx),max(0,int(ys.min())-dy),min(width,int(xs.max())+1+dx),min(height,int(ys.max())+1+dy)]
def paste_roi(base,crop,box):
 x0,y0,x1,y1=box;out=base.copy()
 resized=np.asarray(Image.fromarray(crop.astype('uint8')*255).resize((x1-x0,y1-y0),Image.Resampling.NEAREST))>0
 out[y0:y1,x0:x1]=resized;return out
def loadmask(path,wh):
 return np.asarray(Image.open(path).convert('L'))>0 if path.exists() else np.zeros((wh[1],wh[0]),bool)
def main():
 p=argparse.ArgumentParser();p.add_argument('--source-script',type=Path,required=True);p.add_argument('--case',type=Path,required=True);p.add_argument('--out',type=Path,required=True);p.add_argument('--profile',type=Path,required=True);p.add_argument('--cameras',type=int,nargs='+');a=p.parse_args()
 cfg=json.loads(a.profile.read_text());case=a.case.resolve();out=a.out.resolve()
 if out.exists():raise ValueError('Output exists')
 if not (case/'mask').exists():raise ValueError('Run full-frame SAM first')
 meta=json.loads((case/'metadata.json').read_text());w,h=meta['WH'];n=int(meta['frame_num']);fps=meta.get('fps',30)
 if n>cfg['max_frames']:raise ValueError('Pilot maximum exceeded; chunk/track ROI design required for longer clips')
 spec=importlib.util.spec_from_file_location('source_sam',a.source_script);sam=importlib.util.module_from_spec(spec);spec.loader.exec_module(sam)
 predictor=sam.build_sam3_predictor(version=cfg['model_version'],use_fa3=False)
 out.mkdir(parents=True);records=[]
 for cam in (a.cameras if a.cameras is not None else range(meta.get('num_cameras',4))):
  camera_out=out/f'mask/{cam}';camera_out.mkdir(parents=True)
  if (case/f'mask/{cam}/0').exists():(camera_out/'0').symlink_to(os.path.relpath(case/f'mask/{cam}/0',camera_out),target_is_directory=True)
  if (case/f'mask/{cam}/1').exists():shutil.copytree(case/f'mask/{cam}/1',camera_out/'1')
  baselines=[loadmask(case/f'mask/{cam}/1/{f}.png',(w,h)) for f in range(n)]
  union=np.logical_or.reduce(baselines);box=bounding_roi(union,cfg['padding_fraction'],w,h)
  if box is None:
   records.append({'camera':cam,'status':'no_baseline_detection_roi_not_available'});continue
  work=out/f'roi_camera_{cam}';work.mkdir();x0,y0,x1,y1=box
  for f in range(n):
   im=Image.open(case/f'color/{cam}/{f}.png').convert('RGB').crop(tuple(box))
   im.resize((im.width*cfg['scale'],im.height*cfg['scale']),Image.Resampling.LANCZOS).save(work/f'{f}.png')
  video=work/'input.mp4'
  subprocess.run(['ffmpeg','-v','error','-n','-framerate',str(fps),'-i',str(work/'%d.png'),'-frames:v',str(n),'-c:v','libx264rgb','-crf','0','-preset','fast',str(video)],check=True)
  start=time.perf_counter();sid=predictor.handle_request(request={'type':'start_session','resource_path':str(video),'offload_video_to_cpu':True})['session_id']
  try:
   masks={};first=True
   for prompt in cfg['hand_prompts']:
    m=sam.union_concept(predictor,sid,prompt,0,first,cfg['threshold']);first=False
    for f,v in m.items():masks[f]=v if f not in masks else masks[f]|v
   for prompt in cfg['subtract_prompts']:
    m=sam.union_concept(predictor,sid,prompt,0,False,cfg['threshold'])
    for f,v in m.items():
     if f in masks:masks[f]&=~v
  finally:predictor.handle_request(request={'type':'close_session','session_id':sid})
  rows=[]
  for f,base in enumerate(baselines):
   if f in masks and masks[f].any():
    outmask=paste_roi(base,masks[f],box);mode='replace_inside_roi_preserve_outside'
   else:outmask=base;mode='empty_roi_fallback_to_baseline'
   path=out/f'mask/{cam}/1/{f}.png';path.parent.mkdir(parents=True,exist_ok=True)
   Image.fromarray(outmask.astype('uint8')*255).save(path)
   union_count=np.logical_or(base,outmask).sum()
   rows.append({'frame':f,'mode':mode,'baseline_area':int(base.sum()),'candidate_area':int(outmask.sum()),'difference_pixels':int(np.logical_xor(base,outmask).sum()),'agreement_iou_not_accuracy':float(np.logical_and(base,outmask).sum()/union_count) if union_count else None})
  records.append({'camera':cam,'roi_xyxy':box,'frames':rows,'wall_s':time.perf_counter()-start});print('ROI_COMPLETE',cam,flush=True)
  (out/'progress.json').write_text(json.dumps({'completed_cameras':cam+1,'total_cameras':meta.get('num_cameras',4)},indent=2))
 (out/'candidate_record.json').write_text(json.dumps({'profile':cfg,'source_script_sha256':hashlib.sha256(a.source_script.read_bytes()).hexdigest(),'human_approved':False,'candidate_only':True,'note':'No unseen second hand recovery guarantee. Hand ROI only; object masks are unchanged. No production adoption.','results':records},indent=2))
if __name__=='__main__':main()
