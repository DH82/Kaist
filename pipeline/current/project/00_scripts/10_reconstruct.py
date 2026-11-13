#!/usr/bin/env python3
"""Approved tracking -> stages 10–17 -> native 3D viewer. MANO is a later stage."""
import argparse,json,os,sys,subprocess,tempfile,time
from pathlib import Path
from contextlib import contextmanager
import pipeline
from mask_review import lock
from tracking_review import resolve,require_approved,prepare_reconstruction,require_reconstruction_input
from tracking_paths import resolve_input
from stage_refs import source,link,move,INPUTS
from resume_steps import snapshot,digest,step,preserve
from sam_settings import save
from live_log import Display,gpu_rows
HERE=Path(__file__).resolve().parent
SCRIPTS=['make_shape_rgba','shape_prior','data_process_pcd','data_process_mask','data_process_track','export_hand_track_data','align','data_process_sample']

def current_contract(approval,prepared,cwd):
 return {'approval':approval['id'],'tracking':approval['basis'],'depth_calibration':snapshot([prepared/'depth',prepared/'calibrate.pkl',prepared/'source_frames.json']+([prepared/'world_reorient_M.npy'] if (prepared/'world_reorient_M.npy').exists() else []),prepared),'code':{n:digest(cwd/'data_process'/f'{n}.py') for n in [*SCRIPTS,'motion_contract','export_provenance']},'worker':digest(HERE/'reconstruction_worker.py'),'adapters':{n:digest(HERE/n) for n in ['10_reconstruct.py','stage_refs.py','resume_steps.py']},'settings':{'reference_frame':0,'simplify':.95,'arap_iters':1,'controller':'hand'}}

@contextmanager
def view(out,tracking):
 prepared=source(out)
 with tempfile.TemporaryDirectory(prefix='wfm-3d-') as tmp:
  case=Path(tmp)/out.name;case.mkdir()
  for name in INPUTS:
   if (prepared/name).exists():link(case/name,prepared/name)
  for group in ('cotracker','cotracker_hand'):link(case/group,tracking/group)
  (case/'mask').mkdir()
  for c in range(4):
   link(case/'mask'/str(c),tracking/'mask'/str(c))
   (case/f'mask/mask_info_{c}.json').write_text(json.dumps({'0':'object','1':'hand'}))
  for name in ('pcd','shape'):
   (out/name).mkdir(exist_ok=True);link(case/name,out/name)
  for name in ('track_process_data.pkl','hand_track_data.pkl','final_data.pkl'):
   (case/name).symlink_to(os.path.relpath(out/name,case))
  (case/'mask/processed_masks.pkl').symlink_to(os.path.relpath(out/'processed_masks.pkl',case/'mask'))
  yield case

def logged(cmd,cwd,env,path,title,display):
 path.parent.mkdir(exist_ok=True);start=time.monotonic()
 with path.open('a') as f:
  f.write('\n=== '+title+' ===\n');f.flush()
  child=subprocess.Popen(cmd,cwd=cwd,env=env,stdout=f,stderr=subprocess.STDOUT,start_new_session=True)
  try:
   while True:
    try:code=child.wait(timeout=2);break
    except subprocess.TimeoutExpired:
     rows=gpu_rows();gpu=' | '.join(f'GPU {r[0]} {r[1]}% · VRAM {r[2]}/{r[3]} MiB' for r in rows)
     display.render(['── 3D reconstruction ──',title,f'경과 {time.monotonic()-start:.0f}초 · 단계별 작업량이 달라 ETA는 제공하지 않습니다.',gpu,'로그: '+str(path)])
   if code:raise RuntimeError(title+' 실패 · '+str(path))
  except BaseException:
   import signal
   if child.poll() is None:
    os.killpg(child.pid,signal.SIGTERM)
    try:child.wait(timeout=10)
    except subprocess.TimeoutExpired:os.killpg(child.pid,signal.SIGKILL);child.wait()
   raise
 display.clear();print(title+' 완료',flush=True)

def process(case,check=False,new=False,open_view=True):
 case=resolve(case);project,_=resolve_input(case)
 with lock(project):
  approval=require_approved(case);root,cwd,env=pipeline.runtime();python=str(root/'phystwin-env/bin/python')
  env['PYTHONPATH']=str(cwd/'data_process')+os.pathsep+str(cwd)+os.pathsep+env.get('PYTHONPATH','');env['HF_HUB_OFFLINE']='1';env['TRANSFORMERS_OFFLINE']='1'
  env.pop('WAYLAND_DISPLAY',None);env['XDG_SESSION_TYPE']='x11'
  import shutil
  if not shutil.which('xvfb-run'):raise ValueError('xvfb-run 필요')
  subprocess.run([python,str(HERE/'reconstruction_worker.py'),'check'],cwd=cwd,env=env,check=True)
  prepared=source(case)
  for item in ['depth','calibrate.pkl','metadata.json']:
   if not (prepared/item).exists():raise ValueError('3D 입력 누락: '+item)
  if check:print('3D 실행 환경과 승인 입력 확인 완료');return
  out=project/'06_reconstruction'/case.name
  if new and out.exists():
   old_ref=json.loads((out/'tracking_approval.json').read_text());old_source=(out/old_ref['source']).resolve()
   target=project/'99_history/06_reconstruction'/case.name/pipeline.stamp();move(out,target)
   old_ref['source']=os.path.relpath(old_source,target);save(target/'tracking_approval.json',old_ref)
  out=prepare_reconstruction(case);T=int(json.loads((prepared/'metadata.json').read_text())['frame_num'])
  contract=current_contract(approval,prepared,cwd)
  cp=out/'_resume/contract.json';cp.parent.mkdir(exist_ok=True)
  if cp.exists() and json.loads(cp.read_text())!=contract:raise ValueError('3D 입력/승인/코드 변경: --new-attempt로 기존 결과를 보관하고 실행하세요.')
  if not cp.exists():save(cp,contract)
  display=Display();start=time.monotonic();save(out/'state.json',{'status':'reconstructing'})
  try:
   with view(out,case) as work:
    base=['--base_path',str(work.parent),'--case_name',case.name]
    commands=[['--case',str(work),'--frame','0'],['--img_path',str(work/'shape/masked_image.png'),'--output_dir',str(work/'shape')],base,base+['--controller_name','hand'],base+['--no_viz'],base,base+['--controller_name','hand','--ref_frame','0'],base+['--no_viz','--shape_prior']]
    outputs=[['shape/masked_image.png'],['shape/object.glb','shape/object.ply','shape/visualization.mp4'],[f'pcd/{f}.npz' for f in range(T)],['processed_masks.pkl'],['track_process_data.pkl'],['hand_track_data.pkl'],['shape/matching/final_mesh.glb'],['final_data.pkl']]
    for i,(script,args,expected) in enumerate(zip(SCRIPTS,commands,outputs),10):
     def action(i=i,script=script,args=args,expected=expected):
      require_reconstruction_input(out)
      for later in range(i+1,18):preserve(out/'_resume'/f'{later}.json',out)
      preserve(out/'_resume/review.json',out)
      # These are output-only paths; approved input folders are never writable views.
      for n in expected:preserve(out/n,out)
      if i==16:preserve(out/'shape/matching',out)
      logged(['xvfb-run','-a',python,'-u',str(cwd/'data_process'/f'{script}.py'),*args],cwd,env,out/'logs'/f'{i}_{script}.log',f'{i} · {script}',display)
      subprocess.run([python,str(HERE/'reconstruction_worker.py'),'validate','--case',str(out),'--stage',str(i),'--frames',str(T)],check=True,cwd=cwd,env=env)
     step(out,str(i),action,expected)
    def preview():
     require_reconstruction_input(out)
     logged([python,str(HERE/'reconstruction_worker.py'),'export','--case',str(out),'--frames',str(T)],cwd,env,out/'logs/review.log','3D 로컬 표시 데이터 생성',display)
    step(out,'review',preview,['review/view3d.json','review/validation.json'])
   require_reconstruction_input(out);save(out/'state.json',{'status':'reconstruction_review_pending','seconds':round(time.monotonic()-start,2)})
   print(f'3D 복원 완료 · {time.monotonic()-start:.1f}초\n결과: {out}\n로컬 확인: python3 {HERE / "11_review_3d.py"} {out}')
  except BaseException as ex:save(out/'state.json',{'status':'reconstruction_interrupted','error':str(ex)});raise
  finally:display.clear()
 if open_view and os.environ.get('DISPLAY'):subprocess.Popen([sys.executable,str(HERE/'11_review_3d.py'),str(out)],start_new_session=True)

def verify_outputs(out):
 require_reconstruction_input(out)
 if json.loads((out/'state.json').read_text()).get('status')!='reconstruction_review_pending':raise ValueError('3D 복원이 완료되지 않았습니다.')
 contract=json.loads((out/'_resume/contract.json').read_text());prepared=source(out)
 current=require_approved(require_reconstruction_input(out))
 _,cwd,_=pipeline.runtime()
 if contract!=current_contract(current,prepared,cwd):raise ValueError('3D 입력/코드/설정 계약 변경: 기존 결과는 보존하고 10_reconstruct.py --new-attempt로 재생성하세요.')
 for name in [str(i) for i in range(10,18)]+['review']:
  receipt=json.loads((out/'_resume'/f'{name}.json').read_text())
  if receipt['files']!=snapshot([out/p for p in receipt['files']],out):raise ValueError('3D 결과 변경: '+name)
 return out

def main():
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('input',type=Path);p.add_argument('--check',action='store_true');p.add_argument('--new-attempt',action='store_true');p.add_argument('--no-view',action='store_true');a=p.parse_args()
 if a.check and a.new_attempt:p.error('--check와 --new-attempt 동시 사용 불가')
 try:process(a.input,a.check,a.new_attempt,not a.no_view)
 except KeyboardInterrupt:p.exit(130,'중단됨. 같은 명령으로 완료 단계를 재사용해 재개합니다.\n')
 except Exception as ex:p.exit(1,'중단: '+str(ex)+'\n')
if __name__=='__main__':main()
