"""Render real CoTracker results as browser videos and validate trajectory arrays."""
import argparse, json, subprocess
from pathlib import Path
import cv2
import numpy as np


def main():
 p=argparse.ArgumentParser();p.add_argument('--case',type=Path,required=True);a=p.parse_args()
 from stage_refs import source as input_source
 case=a.case.resolve();prepared=input_source(case);out=case/'review';out.mkdir(parents=True,exist_ok=True)
 episode=case.name
 report={'episode':episode,'coordinate_order':'y,x','note':'Mask agreement is a diagnostic, not tracking ground truth. Red points are predicted occluded positions.','groups':{}}
 for group,mid in [('cotracker',0),('cotracker_hand',1)]:
  source=case/group
  if group=='cotracker_hand' and not (source/'0.npz').exists():source=case/'cotracker/hand'
  arrays=[np.load(source/f'{c}.npz') for c in range(4)];T=arrays[0]['tracks'].shape[0];stats=[]
  for c,d in enumerate(arrays):
   tr,vs,qf=d['tracks'],d['visibility'],d['query_frame'];N=len(qf)
   assert tr.shape==(T,N,2) and vs.shape==(T,N) and N>0 and np.isfinite(tr).all()
   assert ((qf>=0)&(qf<T)).all()
   stats.append({'camera':c,'frames':T,'tracks':N,'finite':True,'visible_samples':0,'visible_out_of_bounds':0,'visible_on_mask':0,'active_samples':0})
  video=out/f'{group}_review.mp4'
  proc=subprocess.Popen(['ffmpeg','-y','-v','error','-f','rawvideo','-pix_fmt','bgr24','-s','1272x720','-r','15','-i','-','-an','-c:v','libx264','-crf','20','-pix_fmt','yuv420p','-movflags','+faststart',str(video)],stdin=subprocess.PIPE)
  contacts=[]
  try:
   for f in range(T):
    panels=[]
    for c,d in enumerate(arrays):
     img=cv2.imread(str(prepared/'color'/str(c)/f'{f}.png'));assert img is not None
     H,W=img.shape[:2];tr=d['tracks'][f];active=d['query_frame']<=f;vis=d['visibility'][f]&active
     y=np.rint(tr[:,0]).astype(int);x=np.rint(tr[:,1]).astype(int);inside=(y>=0)&(y<H)&(x>=0)&(x<W)
     mask=cv2.imread(str(case/'mask'/str(c)/str(mid)/f'{f}.png'),0);assert mask is not None
     s=stats[c];s['active_samples']+=int(active.sum());s['visible_samples']+=int(vis.sum());s['visible_out_of_bounds']+=int((vis&~inside).sum());ii=vis&inside;s['visible_on_mask']+=int((mask[y[ii],x[ii]]>127).sum())
     for flag,col in [(active&~vis&inside,(40,40,240)),(vis&inside,(70,255,60))]:
      for j in np.flatnonzero(flag):cv2.circle(img,(x[j],y[j]),1,col,-1)
     cv2.rectangle(img,(0,0),(W,42),(18,18,18),-1);cv2.putText(img,f'{"OBJECT" if mid==0 else "HAND"} CAM {c+1} | frame {f}/{T-1} | visible {vis.sum()}/{active.sum()}',(12,28),cv2.FONT_HERSHEY_SIMPLEX,.65,(255,255,255),1,cv2.LINE_AA)
     panels.append(cv2.resize(img,(636,360)))
    frame=np.vstack([np.hstack(panels[:2]),np.hstack(panels[2:])]);proc.stdin.write(frame.tobytes())
    if f==0:cv2.imwrite(str(out/f'{group}_poster.jpg'),frame)
    if f in [0,T//2,T-1]:contacts.append(cv2.resize(frame,(848,480)))
  finally:proc.stdin.close()
  assert proc.wait()==0
  cv2.imwrite(str(out/f'{group}_contact.jpg'),np.vstack(contacts))
  for s in stats:
   s['visible_on_mask_fraction']=s['visible_on_mask']/max(1,s['visible_samples']);s['visible_fraction_of_active']=s['visible_samples']/max(1,s['active_samples'])
  report['groups'][group]=stats
 (out/'tracking_report.json').write_text(json.dumps(report,indent=2))
 from html import escape
 sections=[]
 for group,title in [('cotracker','물체'),('cotracker_hand','손')]:
  sections.append(f'<h2>{title} · 4캠 추적</h2><video controls preload="metadata" poster="{group}_poster.jpg" src="{group}_review.mp4"></video>')
 (out/'index.html').write_text('<!doctype html><meta charset="utf-8"><title>Tracking review</title><style>body{font:16px system-ui;max-width:1300px;margin:24px auto;background:#15202b;color:white}video{width:100%}</style><h1>'+escape(episode)+' · 추적 검수</h1><p>초록: 보이는 추적점 / 빨강: 가림으로 예측한 위치. 물체·손 점이 배경으로 이동하거나 표면에서 미끄러지는지, 가림 후 재등장을 확인하세요. 마스크 일치율은 진단값이며 자동 합격 기준이 아닙니다.</p>'+''.join(sections)+'<p><a href="tracking_report.json">배열 검사·진단 결과</a></p>')
 print(json.dumps(report,indent=2))
if __name__=='__main__':main()
