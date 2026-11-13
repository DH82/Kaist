"""Reversible 3D noise candidates. Run with the PhysTwin Python environment.
No coordinates or track IDs are removed. Filtered copies are opt-in review outputs,
not replacements for reconstruction inputs or a regenerated final_data/mesh.
"""
import argparse, contextlib, datetime, hashlib, json, os, pickle, shutil, tempfile, uuid
from pathlib import Path
import numpy as np

FILES={'object':'track_process_data.pkl','hand':'hand_track_data.pkl'}
DEFAULTS=dict(neighbors=31,std_ratio=8.0,cluster_radius=.02,cluster_min_points=10,cluster_fraction=.05,cluster_separation=.03)

def sha(path):
 h=hashlib.sha256()
 with Path(path).open('rb') as f:
  for b in iter(lambda:f.read(1024*1024),b''):h.update(b)
 return h.hexdigest()

def stamp():return datetime.datetime.now(datetime.timezone.utc).isoformat()
def write_json(path,data):path.write_text(json.dumps(data,ensure_ascii=False,separators=(',',':')))
def sources(case):return {name:sha(case/name) for name in FILES.values()}

def directory_for(case,group=None):
 return case/'noise'/group if group else case/'noise'

def selected(group):
 return {group:FILES[group]} if group else FILES

@contextlib.contextmanager
def locked(case,group=None):
 import fcntl
 directory=directory_for(case,group);directory.mkdir(parents=True,exist_ok=True)
 with (directory/'.lock').open('a') as f:
  try:fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)
  except BlockingIOError:raise ValueError('다른 노이즈 분석/적용 작업이 실행 중입니다.')
  # Same project-wide lock as reconstruction, when running in deployed layout.
  from mask_review import lock
  project=case.parent.parent
  if case.parent.name=='06_reconstruction':
   with lock(project):yield directory
  else:yield directory

def load(case,group):
 with (case/FILES[group]).open('rb') as f:d=pickle.load(f)
 p=np.asarray(d[group+'_points']);v=np.asarray(d[group+'_visibilities']).astype(bool)
 if p.ndim!=3 or p.shape[-1]!=3 or v.shape!=p.shape[:2]:raise ValueError('Invalid trajectory shape: '+group)
 if not np.isfinite(p[v]).all():raise ValueError('Nonfinite visible points: '+group)
 return d,p,v

def candidates(points,settings):
 """Return uint8 reasons in original row order; do not alter coordinates."""
 import open3d as o3d
 n=len(points);reasons=np.zeros(n,dtype=np.uint8)
 if n<max(3,settings['cluster_min_points']):return reasons
 cloud=o3d.geometry.PointCloud();cloud.points=o3d.utility.Vector3dVector(points)
 if n>settings['neighbors']:
  _,keep=cloud.remove_statistical_outlier(nb_neighbors=settings['neighbors'],std_ratio=settings['std_ratio'])
  outside=np.ones(n,dtype=bool);outside[keep]=False;reasons[outside]|=1
 labels=np.asarray(cloud.cluster_dbscan(eps=settings['cluster_radius'],min_points=settings['cluster_min_points'],print_progress=False))
 ids,counts=np.unique(labels[labels>=0],return_counts=True)
 if not len(ids):return reasons  # No trustworthy core: never nominate every point as sparse.
 largest=ids[np.argmax(counts)];core=points[labels==largest]
 if len(core)<max(settings['cluster_min_points'],n*.2):return reasons
 core_pc=o3d.geometry.PointCloud();core_pc.points=o3d.utility.Vector3dVector(core)
 distances=np.asarray(cloud.compute_point_cloud_distance(core_pc))
 # Sparse points are candidates only when separated from the core too.
 reasons[(labels<0)&(distances>settings['cluster_separation'])]|=4
 for label,count in zip(ids,counts):
  if label==largest or count>n*settings['cluster_fraction']:continue
  subset=labels==label
  if distances[subset].min()>settings['cluster_separation']:reasons[subset]|=2
 return reasons

def archive(directory,names):
 existing=[directory/n for n in names if (directory/n).exists()]
 if not existing:return
 target=directory/'history'/uuid.uuid4().hex;target.mkdir(parents=True)
 for path in existing:shutil.move(str(path),str(target/path.name))

def analyze(case,group=None,**settings):
 settings={**DEFAULTS,**settings}
 if settings['neighbors']<2 or settings['cluster_min_points']<2 or settings['std_ratio']<=0 or settings['cluster_radius']<=0 or settings['cluster_separation']<0 or not 0<settings['cluster_fraction']<1:raise ValueError('Invalid noise parameters')
 with locked(case,group) as directory:
  before=sources(case);analysis_id=uuid.uuid4().hex;arrays={};preview={'frames':None,'points':{},'counts':{},'settings':settings,'analysis_id':analysis_id}
  for item_group in selected(group):
   _,points,visible=load(case,item_group);T,N=visible.shape
   if preview['frames'] is not None and preview['frames']!=T:raise ValueError('Frame count mismatch')
   preview['frames']=T;reason=np.zeros((T,N),dtype=np.uint8);frames=[]
   for frame in range(T):
    active=np.flatnonzero(visible[frame]);reason[frame,active]=candidates(points[frame,active],settings)
    bad=active[reason[frame,active]!=0];good=active[reason[frame,active]==0]
    good=good[::max(1,int(np.ceil(len(good)/1500)))];shown=np.concatenate([good,bad])
    frames.append([{'id':int(i),'p':points[frame,i].tolist(),'reason':int(reason[frame,i])} for i in shown])
    if frame%20==0 or frame==T-1:print(f'{item_group}: {frame+1}/{T}',flush=True)
   arrays[item_group]=reason;preview['points'][item_group]=frames
   preview['counts'][item_group]={'visible':int(visible.sum()),'candidates':int((reason!=0).sum()),'sor':int(((reason&1)!=0).sum()),'cluster':int(((reason&2)!=0).sum()),'sparse':int(((reason&4)!=0).sum())}
  if before!=sources(case):raise ValueError('Inputs changed during analysis')
  with tempfile.TemporaryDirectory(dir=directory,prefix='.build-') as tmp:
   temp=Path(tmp);np.savez_compressed(temp/'candidates.npz',**arrays);write_json(temp/'preview.json',preview)
   manifest={'schema':1,'analysis_id':analysis_id,'created_at':stamp(),'sources':before,'settings':settings,'counts':preview['counts'],'files':{n:sha(temp/n) for n in ('candidates.npz','preview.json')},'algorithm':sha(Path(__file__)),'note':'Candidates only. Original arrays/IDs preserved. No final_data or mesh regenerated.'}
   write_json(temp/'analysis.json',manifest)
   archive(directory,['applied','analysis.json','preview.json','candidates.npz'])
   for name in ('candidates.npz','preview.json','analysis.json'):os.replace(temp/name,directory/name)
  return manifest

def verified(case,group=None):
 directory=directory_for(case,group);manifest=json.loads((directory/'analysis.json').read_text())
 if manifest['sources']!=sources(case):raise ValueError('원본 3D 데이터가 변경되었습니다. 노이즈 분석을 다시 실행하세요.')
 if manifest.get('algorithm')!=sha(Path(__file__)):raise ValueError('노이즈 분석 코드가 변경되었습니다. 다시 분석하세요.')
 if any(sha(directory/n)!=h for n,h in manifest['files'].items()):raise ValueError('노이즈 후보 파일이 변경되었습니다. 다시 분석하세요.')
 return manifest

def apply(case,reviewer,analysis_id=None,group=None):
 if not reviewer.strip():raise ValueError('검수자 이름을 입력하세요.')
 with locked(case,group) as directory:
  manifest=verified(case,group)
  if analysis_id is not None and analysis_id!=manifest['analysis_id']:raise ValueError('표시 중인 분석이 변경되었습니다. 다시 불러오세요.')
  with np.load(directory/'candidates.npz',allow_pickle=False) as f:reasons={g:f[g].copy() for g in selected(group)}
  with tempfile.TemporaryDirectory(dir=directory,prefix='.apply-') as tmp:
   temp=Path(tmp)
   for item_group,name in selected(group).items():
    data,points,visibility=load(case,item_group);valid=reasons[item_group]==0
    if valid.shape!=visibility.shape:raise ValueError('Candidate shape mismatch')
    # Keep original visibility in the derived file as well; spatial_valid concerns
    # observations. motions_valid concerns transitions, and must retain that meaning.
    data[item_group+'_original_visibilities']=visibility.copy();data[item_group+'_spatial_valid']=valid
    effective=visibility&valid;data[item_group+'_visibilities']=effective
    motion_key=item_group+'_motions_valid'
    if motion_key in data:
     original=np.asarray(data[motion_key]).copy();data[item_group+'_original_motions_valid']=original
     transitions=effective.copy();transitions[:-1]&=effective[1:];transitions[-1]=False
     data[motion_key]=original&transitions
    data[item_group+'_noise_reasons']=reasons[item_group]
    with (temp/name).open('wb') as f:pickle.dump(data,f,protocol=pickle.HIGHEST_PROTOCOL)
   verified(case,group)
   decision={'status':'applied','reviewer':reviewer.strip(),'applied_at':stamp(),'analysis_id':manifest['analysis_id'],'sources':manifest['sources'],'settings':manifest['settings'],'counts':manifest['counts'],'files':{n:sha(temp/n) for n in selected(group).values()},'note':'Derived trajectories only; original tracks, meshes and final_data unchanged. Downstream must explicitly consume these reviewed files.'}
   write_json(temp/'decision.json',decision);archive(directory,['applied']);os.replace(temp,directory/'applied')
  return decision

def check(case,group=None):
 with locked(case,group):
  manifest=verified(case,group);decision_path=directory_for(case,group)/'applied/decision.json'
  if decision_path.exists():
   decision=json.loads(decision_path.read_text())
   if decision['analysis_id']!=manifest['analysis_id'] or decision['sources']!=manifest['sources']:raise ValueError('Applied decision is stale')
   if any(sha(decision_path.parent/name)!=digest for name,digest in decision['files'].items()):raise ValueError('Applied result changed')
 return manifest

def resolve_noise_tracks(case):
 """Only return per-group outputs after both groups have explicit valid decisions."""
 if not any((directory_for(case,g)/"analysis.json").exists() for g in FILES):
  check(case)
  if not (case/"noise/applied/decision.json").exists():raise ValueError("Noise candidates have not been applied")
  return {g:case/"noise/applied"/name for g,name in FILES.items()}
 result={}
 for group,name in FILES.items():
  check(case,group)
  applied=directory_for(case,group)/'applied'
  if not (applied/'decision.json').exists():raise ValueError(group+' candidates have not been applied')
  result[group]=applied/name
 return result

def reset(case,group=None):
 with locked(case,group) as directory:archive(directory,['applied'])
 return {'status':'not_applied'}

def main():
 parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('action',choices=['analyze','apply','reset','verify']);parser.add_argument('--case',required=True,type=Path);parser.add_argument('--reviewer',default='');parser.add_argument('--analysis-id')
 for key,value in DEFAULTS.items():parser.add_argument('--'+key.replace('_','-'),type=type(value),default=value)
 parser.add_argument('--group',choices=['object','hand','both'],default='both')
 args=parser.parse_args();case=args.case.resolve()
 try:
  result={}
  for group in (FILES if args.group=='both' else [args.group]):
   if args.action=='verify' and not (directory_for(case,group)/'analysis.json').exists():continue
   result[group]=analyze(case,group=group,**{key:getattr(args,key) for key in DEFAULTS}) if args.action=='analyze' else apply(case,args.reviewer,args.analysis_id,group) if args.action=='apply' else check(case,group) if args.action=='verify' else reset(case,group)
  print(json.dumps(result,ensure_ascii=False),flush=True)
 except Exception as ex:parser.exit(1,'중단: '+str(ex)+'\n')
if __name__=='__main__':main()
