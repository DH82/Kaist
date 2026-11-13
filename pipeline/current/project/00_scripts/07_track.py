#!/usr/bin/env python3
"""Approved SAM3 masks -> object/hand CoTracker -> four-camera review. Stops there."""
import argparse,json,os,subprocess,sys,tempfile,time
from contextlib import nullcontext
from pathlib import Path
import pipeline
import tracking_progress
from mask_review import approved,lock
from mask_paths import resolve_input
from stage_refs import source,link,move
from tracking_inputs import prepare
from tracking_review import invalidate,require_approved
from tracking_qc import run as run_qc
from tracking_paths import episode_root
from resume_steps import digest,snapshot,step,preserve
from sam_settings import save

HERE=Path(__file__).resolve().parent

def run_logged(command,cwd,env,log,label):
 log.parent.mkdir(parents=True,exist_ok=True);start=time.monotonic()
 panel=getattr(tracking_progress.CURRENT,'panel',None)
 if panel:panel.refresh(label,log,telemetry=True)
 with log.open('a') as stream:
  stream.write('\n=== '+time.strftime('%Y-%m-%d %H:%M:%S')+' ===\n');stream.flush()
  process=subprocess.Popen(command,cwd=cwd,env=env,stdout=stream,stderr=subprocess.STDOUT)
  try:
   while True:
    try:code=process.wait(timeout=2);break
    except subprocess.TimeoutExpired:
     if panel:panel.refresh(telemetry=True)
   if code:raise RuntimeError(f'{label} 실패 (exit={code}). 로그 확인: {log}')
  except BaseException:
   process.terminate()
   try:process.wait(timeout=10)
   except subprocess.TimeoutExpired:process.kill();process.wait()
   raise
 if panel:panel.refresh(label+' · 결과 확인 중')

def make_contract(prepared,approval,script,repo,checkpoint):
 from tracking_contract import make_contract as build
 return build(prepared,approval,script,repo,checkpoint,HERE)


def require_current_contract(out,current):
 path=out/'_resume/contract.json'
 if not path.is_file() or json.loads(path.read_text())!=current:
  raise ValueError('입력/승인본/코드/모델 변경 또는 이전 계약 없음: --new-attempt로 이전 결과를 보관하고 새로 실행하세요.')


def process_case(case,check_only=False,new=False):
 project,case=resolve_input(case)
 with (nullcontext() if check_only else lock(project)):
  approval=approved(project,case.name);prepared=source(case)
  root,cwd,env=pipeline.runtime();python=str(root/'phystwin-env/bin/python');script=cwd/'data_process/track_object.py';repo=Path(env.get('KP_COTRACKER_REPO',''))
  meta=json.loads((prepared/'metadata.json').read_text());frames=int(meta['frame_num'])
  if frames<1:raise ValueError('No input frames')
  for c in range(4):
   if not (prepared/f'color/{c}.mp4').is_file():raise ValueError(f'전처리 RGB 영상 누락: color/{c}.mp4')
  if not script.is_file():raise ValueError('Tracking script missing: '+str(script))
  checked=subprocess.run([python,str(HERE/'tracking_worker.py'),'check','--repo',str(repo)],env=env,cwd=cwd,check=True,capture_output=True,text=True)
  runtime=json.loads(checked.stdout.strip().splitlines()[-1]);print('실행 환경:',json.dumps(runtime,ensure_ascii=False))
  out=episode_root(project,case.name)
  print(f'승인본: {approval.name} | 입력 {frames}프레임 × 4캠 | 출력: {out}')
  if check_only:print('사전 확인 완료 · 추론/출력 변경 없음');return
  contract=make_contract(prepared,approval,script,repo,Path(runtime['checkpoint']))
  if not new and out.exists():
   require_current_contract(out,contract)
  if new and out.exists():move(out,project/'99_history/05_tracking'/case.name/pipeline.stamp())
  if out.exists() and out.parent.name=='approved' and not new:
   try:require_approved(out)
   except (ValueError,OSError,KeyError):out=invalidate(out,'추적 승인 무효 또는 입력 변경')
   else:
    out,qc,next_input=run_qc(out,python,cwd,env)
    print('QC: '+qc['status']+' · 다음 단계 입력: '+str(next_input));return
  out=prepare(project,case.name)
  contract_path=out/'_resume/contract.json'
  if contract_path.exists() and json.loads(contract_path.read_text())!=contract:raise ValueError('입력/승인본/코드/모델이 변경됐습니다. --new-attempt로 이전 결과를 보관하고 새로 실행하세요.')
  if not contract_path.exists():
   if any((out/g).exists() and any((out/g).iterdir()) for g in ['cotracker','cotracker_hand']):raise ValueError('기존 추적 결과의 실행 기록이 없습니다. --new-attempt를 사용하세요.')
   contract_path.parent.mkdir(parents=True,exist_ok=True);save(contract_path,contract)
  start=time.monotonic();save(out/'state.json',{'status':'tracking_running','approval_version':approval.name})
  progress=tracking_progress.Progress(out);tracking_progress.CURRENT=progress
  panel=tracking_progress.Panel(progress,case.name);progress.panel=panel
  panel.refresh()
  try:
   for obj,group in [(0,'cotracker'),(1,'cotracker_hand')]:
    (out/group).mkdir(exist_ok=True)
    for camera in range(4):
     name=f'{group}_camera_{camera}';target=out/group/f'{camera}.npz';ordinal=obj*4+camera+1
     def infer(obj=obj,group=group,camera=camera,target=target,name=name,ordinal=ordinal):
      invalidate(out,'추적 재실행')
      progress.begin(name)
      preserve(out/'_resume/tracking_review.json',out)
      work=out/'_work';work.mkdir(exist_ok=True)
      with tempfile.TemporaryDirectory(prefix='tracking-',dir=work) as tmp:
       view=Path(tmp)/case.name;view.mkdir()
       for item in ['color','depth','metadata.json']:
        if (prepared/item).exists():link(view/item,prepared/item)
       link(view/'mask',approval/'mask')
       command=[python,'-u',str(script),'--base_path',tmp,'--case_name',case.name,'--obj_id',str(obj),'--out_dir',group,'--cameras',str(camera),'--cotracker_repo',str(repo),'--no_viz']
       run_logged(command,cwd,env,out/'logs'/f'{name}.log',f'{ordinal}/8 {group} Camera {camera+1}')
       result=view/group/f'{camera}.npz'
       subprocess.run([python,str(HERE/'tracking_worker.py'),'validate','--path',str(result),'--frames',str(frames)],check=True,env=env,capture_output=True,text=True)
       if target.exists():preserve(target,out)
       os.replace(result,target)
     step(out,name,infer,[f'{group}/{camera}.npz'],quiet=True)
     progress.finish(name);panel.refresh(name+' 완료')
   def previews():
    invalidate(out,'검수 영상 재생성')
    progress.begin('tracking_review')
    # Recheck the approval before publishing review outputs.
    if approved(project,case.name).name!=approval.name:raise ValueError('Approval changed during tracking')
    run_logged([python,'-u',str(HERE/'tracking_preview.py'),'--case',str(out)],cwd,env,out/'logs/review.log','9/9 추적 검수 영상')
   expected=['review/index.html','review/tracking_report.json']+[f'review/{g}_{suffix}' for g in ['cotracker','cotracker_hand'] for suffix in ['review.mp4','poster.jpg','contact.jpg']]
   step(out,'tracking_review',previews,expected,quiet=True)
   progress.finish('tracking_review');panel.refresh('전체 완료');panel.close()
   out,qc,next_input=run_qc(out,python,cwd,env)
   save(out/'state.json',{'status':'reconstruction_inputs_ready' if next_input else 'tracking_qc_blocked' if qc['status']=='blocked' else 'tracking_review_pending','approval_version':approval.name,'last_run_seconds':round(time.monotonic()-start,2)})
   print(f'추적·QC 완료 · 이번 실행 {time.monotonic()-start:.1f}초\nQC: {out / "qc/report.json"}')
   if next_input:print(f'승인 통과 · 다음 단계 입력 준비 완료: {next_input} · 실행: python3 {HERE / "10_reconstruct.py"} {out}')
   elif qc['status']=='blocked':print('구조 오류로 중단했습니다. qc/report.json을 확인하세요.')
   else:print(f'이상 후보 {len(qc["events"])}개 또는 기존 보류 기록 확인 필요 · python3 {HERE / "09_review_tracking.py"} {out}')
  except BaseException as ex:
   out=episode_root(project,case.name)
   save(out/'state.json',{'status':'tracking_interrupted','approval_version':approval.name,'error':str(ex),'last_run_seconds':round(time.monotonic()-start,2)})
   raise
  finally:
   panel.close()
   tracking_progress.CURRENT=None

def main():
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('input',type=Path);p.add_argument('--check',action='store_true',help='승인본·모델·GPU 확인만 수행');p.add_argument('--new-attempt',action='store_true',help='이전 출력 보관 후 새 추적')
 a=p.parse_args()
 if a.check and a.new_attempt:p.error('--check와 --new-attempt는 함께 사용할 수 없습니다')
 try:process_case(a.input,a.check,a.new_attempt)
 except KeyboardInterrupt:print('중단됨. 같은 명령으로 완료된 카메라부터 건너뛰고 재개합니다.',file=sys.stderr);return 130
 except subprocess.CalledProcessError as ex:print('중단: '+str(ex)+'\n'+(ex.stderr or ''),file=sys.stderr);return 1
 except Exception as ex:print('중단: '+str(ex),file=sys.stderr);return 1
 return 0
if __name__=='__main__':raise SystemExit(main())
