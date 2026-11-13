"""Timestamp matching without assuming identical camera frame indices."""
import csv,math,bisect,json
from pathlib import Path

def timeline(case):
 from sync_mapping import source_mapping
 data=source_mapping(case,[f'cam_{c}' for c in range(1,5)])
 frames=[{'time':r['reference_time_s'],'relative_s':r['reference_time_s']-data['overlap_start'],'cameras':[{'camera':c['camera']+1,'frame':c['raw_idx'],'time':c['host_time_s'],'offset_ms':round(c['residual_ms'],3)} for c in r['cameras']]} for r in data['frames']]
 return {'basis':'host_time_s; preprocessing shared mapping; exposure time unknown','policy':data['policy'],'reference_camera':data['reference_camera']+1,'sync_tol_ms':data['sync_tol_ms'],'start':data['overlap_start'],'end':data['overlap_end'],'duration':data['overlap_end']-data['overlap_start'],'frames':frames}

def build(case,out):
 import cv2,numpy as np
 data=timeline(case);dest=out/'sync_media';dest.mkdir(exist_ok=True)
 for cam in range(1,5):
  wanted={row['cameras'][cam-1]['frame'] for row in data['frames']};cap=cv2.VideoCapture(str(case/f'cam_{cam}.mp4'));idx=0;found=set()
  try:
   while True:
    ok,rgb=cap.read()
    if not ok:break
    if idx in wanted:
     depthfile=case/f'cam_{cam}_depth/frame_{idx:06d}.npy'
     if not depthfile.is_file():raise ValueError(f'Missing depth: {depthfile.name} camera {cam}')
     d=np.load(depthfile,allow_pickle=False);valid=(d>0)&(d<65535)
     im=np.repeat((np.clip(d.astype(float)/2000,0,1)*255).astype('uint8')[...,None],3,axis=2);im[~valid]=(0,0,255)
     for kind,image in [('rgb',rgb),('depth',im)]:
      if not cv2.imwrite(str(dest/f'cam_{cam}_{idx:06d}_{kind}.jpg'),image,[cv2.IMWRITE_JPEG_QUALITY,90]):raise ValueError('Image write failed')
     found.add(idx)
    idx+=1
  finally:cap.release()
  if found!=wanted:raise ValueError(f'Camera {cam} RGB frames missing: {sorted(wanted-found)}')
 (dest/'timeline.tmp').write_text(json.dumps(data));(dest/'timeline.tmp').replace(dest/'timeline.json')
 return data
if __name__=='__main__':
 import sys
 build(Path(sys.argv[1]),Path(sys.argv[2]))
