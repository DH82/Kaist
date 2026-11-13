"""Read-only tracking screening. Run with the PhysTwin Python (NumPy/OpenCV)."""
import argparse,json,math,datetime
from zoneinfo import ZoneInfo
from pathlib import Path
import numpy as np
import cv2
from tracking_worker import validate

def intervals(flags,minimum):
 result=[];start=None
 for i,value in enumerate(list(flags)+[False]):
  if value and start is None:start=i
  if not value and start is not None:
   if i-start>=minimum:result.append((start,i-1))
   start=None
 return result

def inspect(case,policy):
 from stage_refs import source
 prepared=source(case);meta=json.loads((prepared/'metadata.json').read_text());T=int(meta['frame_num']);W,H=meta['WH'];events=[];errors=[];metrics={}
 for group,obj in [('cotracker',0),('cotracker_hand',1)]:
  for camera in range(4):
   try:
    validate(case/group/f'{camera}.npz',T)
    with np.load(case/group/f'{camera}.npz',allow_pickle=False) as d:tr=d['tracks'].copy();vis=d['visibility'].astype(bool);q=d['query_frame'].copy()
    values={k:[] for k in ['outside_mask','outside_image','jump','low_visibility','low_coverage']}
    previous=None
    for f in range(T):
     mask=cv2.imread(str(case/f'mask/{camera}/{obj}/{f}.png'),0)
     if mask is None or mask.shape!=(H,W):raise ValueError(f'mask missing or wrong dimensions: frame {f}')
     active=q<=f;visible=vis[f]&active;n=int(visible.sum());active_n=int(active.sum());point=tr[f];inside=(point[:,0]>=0)&(point[:,0]<H)&(point[:,1]>=0)&(point[:,1]<W)
     foreground=int((mask>127).sum())>=policy['minimum_mask_pixels']
     radius=policy['mask_tolerance_px'];expanded=cv2.dilate((mask>127).astype('uint8'),np.ones((2*radius+1,2*radius+1),np.uint8))
     idx=np.flatnonzero(visible&inside);yx=np.floor(point[idx]).astype(int);on=int(expanded[yx[:,0],yx[:,1]].sum())
     outside_image=float((visible&~inside).sum()/max(1,n));outside_mask=1-on/max(1,n) if n else 0
     eligible=visible&previous if previous is not None else np.zeros_like(visible)
     delta=np.linalg.norm(point-tr[f-1],axis=1) if f else np.zeros(len(q))
     jump=float((eligible&(delta>policy['jump_diagonal_fraction']*math.hypot(W,H))).sum()/max(1,int(eligible.sum())))
     values['outside_mask'].append(outside_mask if n>=policy['minimum_visible_points'] else 0)
     values['outside_image'].append(outside_image)
     values['jump'].append(jump)
     values['low_visibility'].append(bool(foreground and active_n and n/active_n<policy['low_visibility_fraction']))
     values['low_coverage'].append(bool(foreground and n<policy['minimum_visible_points']))
     previous=visible
    rules={'outside_mask':policy['outside_mask_fraction'],'outside_image':policy['outside_image_fraction'],'jump':policy['jump_points_fraction'],'low_visibility':.5,'low_coverage':.5}
    for reason,threshold in rules.items():
     flags=[v>threshold for v in values[reason]]
     for start,end in intervals(flags,1 if reason=='jump' else min(T,policy['minimum_run_frames'])):
      events.append({'id':f'{group}_{camera}_{reason}_{start}_{end}','group':group,'camera':camera+1,'start_frame':max(0,start-1) if reason=='jump' else start,'end_frame':end,'reason':reason,'peak':float(max(values[reason][start:end+1])),'threshold':threshold})
    metrics[f'{group}/{camera}']={k:{'maximum':float(max(v)),'flagged_frames':sum(x>rules[k] for x in v)} for k,v in values.items()}
   except Exception as ex:errors.append({'group':group,'camera':camera+1,'error':str(ex)})
 return {'status':'blocked' if errors else 'needs_review' if events else 'passed','events':events,'errors':errors,'metrics':metrics}

def main():
 p=argparse.ArgumentParser();p.add_argument('--case',type=Path,required=True);p.add_argument('--policy',type=Path,required=True);p.add_argument('--source-token',required=True);a=p.parse_args();policy=json.loads(a.policy.read_text());report=inspect(a.case,policy);report.update(policy=policy,source_token=a.source_token,schema_version=1,evaluated_at=datetime.datetime.now(ZoneInfo('Asia/Seoul')).isoformat())
 from sam_settings import save
 (a.case/'qc').mkdir(exist_ok=True);save(a.case/'qc/report.json',report)
 print('자동 QC:',report['status'],'| 이상 구간',len(report['events']),'| 구조 오류',len(report['errors']))
if __name__=='__main__':main()
