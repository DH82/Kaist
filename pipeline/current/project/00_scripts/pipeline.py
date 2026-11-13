"""Shallow operator layout; previous data is archived, never overwritten."""
import argparse,datetime,fcntl,hashlib,html,json,os,shlex,shutil,subprocess,sys,uuid
from pathlib import Path
import raw_io as raw
from mask_paths import episode_root,relocate
HERE=Path(__file__).resolve().parent
PROJECT=HERE.parent

def load(p):return json.loads(p.read_text())
def save(p,d):
 p.parent.mkdir(parents=True,exist_ok=True);temp=p.with_suffix('.tmp');temp.write_text(json.dumps(d,ensure_ascii=False,indent=2));temp.replace(p)
def sha(p):
 with p.open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()
def stamp():return datetime.datetime.now().strftime('%Y%m%d-%H%M%S')+'-'+uuid.uuid4().hex[:6]
def init(p):
 for d in ['01_raw','02_qc','03_prepared','04_masks','05_tracking','06_reconstruction','07_deliverables','99_history']:(p/d).mkdir(parents=True,exist_ok=True)
 if (p/'project.json').exists() and load(p/'project.json').get('layout_version')!=4:raise ValueError('Migrate this project before using 00_scripts/')
 save(p/'project.json',{'layout_version':4,'raw':'01_raw','qc':'02_qc','prepared':'03_prepared','masks':'04_masks','history':'99_history'})
def archive(p,folder):
 if folder.exists():
  target=p/'99_history'/folder.parent.name/folder.name/stamp();target.parent.mkdir(parents=True,exist_ok=True);__import__('stage_refs').move(folder,target)
def overview(p):
 rows=[]
 for d in sorted((p/'01_raw').glob('ep_*')):
  ep=d.name;q=load(p/'02_qc'/ep/'qc.json') if (p/'02_qc'/ep/'qc.json').exists() else {};prep=(p/'03_prepared'/ep/'preparation.json').exists();mask=(episode_root(p,ep)/'state.json').exists() and load(episode_root(p,ep)/'state.json').get('status')=='mask_review_pending'
  links=f'<a href="02_qc/{ep}/report.html">QC</a>' if q else 'QC 미실행'
  links+=(f' · <a href="03_prepared/{ep}/sam3_setup.html">RGB · 프롬프트 설정</a>' if prep else ' · 전처리 미완료')
  links+=(f' · <a href="{episode_root(p,ep).relative_to(p)}/index.html">마스크</a>' if mask else ' · 마스크 미생성')
  rows.append(f'<tr><td>{html.escape(ep)}</td><td>{q.get("overall","미실행")}</td><td>{"완료" if prep else "미완료"}</td><td>{("승인 완료" if (episode_root(p,ep)/"review/approved.json").exists() else "검수 대기") if mask else "미실행"}</td><td>{links}</td></tr>')
 (p/'overview.html').write_text(page('WFM KAIST · 작업 현황','<p>RAW → 자동 QC → 전처리에서 정지 → 작업자가 SAM3 별도 실행 → 마스크 검수</p><table><tr><th>Episode</th><th>QC</th><th>전처리</th><th>SAM3</th><th>화면</th></tr>'+''.join(rows)+'</table><pre>python3 00_scripts/01_copy_raw.py &lt;촬영 폴더&gt;\npython3 00_scripts/02_capture_qc.py 01_raw/&lt;episode&gt;\npython3 00_scripts/05_sam3.py 03_prepared/&lt;episode&gt;</pre>'))
def page(title,body):return '<!doctype html><html lang="ko"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>'+html.escape(title)+'</title><style>body{font:16px/1.7 system-ui;max-width:1300px;margin:auto;padding:24px;background:#f2f5f8;color:#17283c}table{width:100%;border-collapse:collapse}td,th{padding:12px;border:1px solid #cbd5e1}video,img{width:100%}.grid{display:grid;grid-template-columns:1fr 1fr;gap:16px}pre{white-space:pre-wrap;overflow-wrap:anywhere}</style><a href="/overview.html">전체 현황</a><h1>'+html.escape(title)+'</h1>'+body+'</html>'
def manifest(p,ep):return p/'99_history/manifests'/f'{ep}.json'
def verify_raw(p,ep):
 rec=load(manifest(p,ep));raw.verify(p/'01_raw'/ep,rec);return rec['fingerprint']
def input_fingerprint(target):
 raw.validate(target['case']);return raw.inventory(target['case'])['fingerprint']
def review_report(out,decision,note,operator=None,checklist=None):
 meta=load(out/'provenance.json');res=load(out/'qc.json');project=(out/meta['project_relative']).resolve()
 if sha(out/'qc.json')!=meta['qc_sha256']:raise ValueError('QC result changed')
 if decision=='accept':
  if res.get('overall') not in ('PASS','WARN') or not res.get('ok'):raise ValueError('FAIL/ERROR cannot be approved')
  if verify_raw(project,meta['episode'])!=meta['input_fingerprint']:raise ValueError('RAW changed')
 record={'decision':decision,'note':note,'operator':operator,'checklist':checklist or {},'time':datetime.datetime.now().astimezone().isoformat(),'qc_sha256':meta['qc_sha256']}
 old=load(out/'operator_review.json').get('history',[]) if (out/'operator_review.json').exists() else []
 save(out/'operator_review.json',{**record,'history':old+[record]});overview(project)
def gate(p,ep):
 fp=verify_raw(p,ep);out=p/'02_qc'/ep;res=load(out/'qc.json');meta=load(out/'provenance.json')
 required={'frame_continuity','color_quality','depth_completeness','cross_cam_residual','world_frame','sync'}
 if {c['name'] for c in res.get('checks',[])}!=required or any(c['status'] not in ('PASS','WARN') for c in res['checks']):raise ValueError('QC missing check / failure')
 if not res.get('ok') or res['overall'] not in ('PASS','WARN'):raise ValueError('QC failure')
 if meta['qc_sha256']!=sha(out/'qc.json') or meta['input_fingerprint']!=fp:raise ValueError('QC/RAW mismatch')
 if (out/'operator_review.json').exists() and load(out/'operator_review.json')['decision']=='reject':raise ValueError('Operator hold')
 return fp

def runtime():
 config=load(HERE/'runtime.local.json');root=Path(config['pipeline_root']);env=os.environ.copy()
 for line in (root/'kaist-full/config.env').read_text().splitlines():
  if line and not line.startswith('#'):
   k,v=line.split('=',1);env[k]=' '.join(shlex.split(v))
 env['PYTHONUNBUFFERED']='1';env['PYTORCH_ALLOC_CONF']='expandable_segments:True'
 return root,root/'kaist-full/PhysTwin',env

from live_log import execute

def copy_raw(p,inputs):
 for source in inputs:
  scan=source/'01_raw' if (source/'project.json').exists() else source
  for src in raw.discover(scan,p/'99_history'):
   rec=raw.inventory(src);dest=p/'01_raw'/src.name
   if dest.exists():
    if raw.inventory(dest)['fingerprint']!=rec['fingerprint']:raise ValueError('Same episode name has different RAW: '+str(dest))
   else:raw.stage_copy(src,dest,rec)
   save(manifest(p,src.name),rec);print('RAW:',dest)
 overview(p)
def run_qc(p,ep,new=False,open_setup=True):
 from qc_review_ui import page as qc_page
 rec=verify_raw(p,ep);out=p/'02_qc'/ep
 if (out/'operator_review.json').exists() and load(out/'operator_review.json')['decision']=='reject':raise ValueError('Operator hold: resolve the review before rerunning')
 archive(p,out);out.mkdir(parents=True)
 cfg=load(HERE/'runtime.local.json');python=cfg['qc_python'];source=p/'01_raw'/ep
 try:
  with (out/'output.log').open('w') as f:r=subprocess.run([python,str(HERE/'qc_backend/worker.py'),str(source),str(out),'--scene','2D'],stdout=f,stderr=subprocess.STDOUT)
  res=load(out/'qc.json') if (out/'qc.json').exists() else {'ok':False,'overall':'ERROR','checks':[]}
  if r.returncode and res.get('overall') not in ('FAIL','ERROR'):res.update(ok=False,overall='ERROR')
 except Exception as ex:res={'ok':False,'overall':'ERROR','checks':[],'error':str(ex)}
 # Keep the existing QC thresholds; record the exact preprocessing timeline separately.
 from sync_mapping import source_mapping
 try:
  mapping=source_mapping(source,[f'cam_{c}' for c in range(1,5)])
  save(out/'source_frames_pretrim.json',mapping)
  res['timeline']={'policy':mapping['policy'],'reference_camera':mapping['reference_camera'],'frames':len(mapping['frames']),'mapping':'source_frames_pretrim.json','sync_tol_ms':mapping['sync_tol_ms'],'exposure_semantics':mapping['exposure_semantics']}
  res['sync_gate_semantics']='Legacy sampled host-offset diagnostic; thresholds unchanged. See shared preprocessing mapping for exported frame identities. PASS does not certify simultaneous exposure.'
 except (ValueError,RuntimeError,OSError) as ex:res.update(ok=False,overall='ERROR',mapping_error=str(ex))
 save(out/'qc.json',res);save(out/'provenance.json',{'episode':ep,'mode':'raw','root_relative':'../..','case_relative':f'01_raw/{ep}','project_relative':'../..','qc_sha256':sha(out/'qc.json'),'input_fingerprint':rec})
 (out/'report.html').write_text(qc_page(ep,source,out,res));overview(p);print('QC:',res['overall'],out/'report.html')
 if res['overall'] not in ('PASS','WARN'):raise ValueError('QC failed. Preprocessing not started.')
 prepare(p,ep,new)
 if not open_setup:return
 command=['/usr/bin/python3',str(HERE/'sam_local_setup.py'),str(p/'03_prepared'/ep),'--confirm-only']
 result=subprocess.run(command)
 if result.returncode:raise ValueError('QC 프롬프트 입력을 완료하지 않았습니다. 입력·전처리 결과는 보존됩니다.')
 print('QC 및 프롬프트 저장 완료. SAM3는 터미널에서 05_sam3.py로 실행하세요.')
def prepared_page(p,ep):
 from sam_settings import review_page
 settings=load(p/'episodes.json').get(ep,{}) if (p/'episodes.json').exists() else {}
 cfg=p/'03_prepared'/ep/'sam3_settings.json'
 if cfg.exists():settings=load(cfg).get('settings',settings)
 review_page(p/'03_prepared'/ep,settings)
 print(f'다음 작업(사람): python3 {HERE}/sam_local_setup.py {p}/03_prepared/{ep}')
 d=p/'03_prepared'/ep;r=load(d/'preparation.json');tr=load(d/'trim.json')
 (d/'index.html').write_text(page(ep+' · 전처리 완료',f'<p>{r["frames"]}프레임 · SAM3 실행 전</p><p>앞 {tr["trim_start"]} / 뒤 {tr["trim_end"]} 제거. 중간 후보 {tr["middle_bad"]} 유지.</p><p><a href="sam3_setup.html">다음: 대표 RGB 확인 · 물체 프롬프트 / 장면 유형 설정</a></p><a href="trim.json">노출 처리 기록</a> · <a href="preparation.json">전처리 기록</a>'))
def prepare(p,ep,new=False):
 fp=gate(p,ep);dest=p/'03_prepared'/ep
 root,source,env=runtime()
 from preparation_contract import contract,outputs,verify
 expected=contract(fp,HERE,source)
 if (dest/'preparation.json').exists() and not new:
  verify(dest,expected)
  prepared_page(p,ep);print('Existing prepared:',dest);return
 root,source,env=runtime();P=str(root/'phystwin-env/bin/python');job=stamp();stage=p/'99_history/staging'/job;case=stage/ep;stage.mkdir(parents=True)
 try:
  execute([P,'data_process/preprocess_rgbd.py','--src',str(p/'01_raw'/ep),'--out',str(stage),'--case_name',ep],stage/'03.log',source,env)
  execute([P,str(HERE/'process_input_worker.py'),'trim',str(case),'--source',str(source),'--out',str(case)],stage/'04.log',source,env)
  execute([P,str(HERE/'process_input_worker.py'),'validate',str(case)],stage/'04.log',source,env)
  save(case/'preparation.json',{'status':'preprocessed','episode':ep,'input_fingerprint':fp,'job':job,'frames':load(case/'metadata.json')['frame_num'],'contract':expected,'outputs':outputs(case)})
  for n in ['03.log','04.log']:shutil.copy2(stage/n,case/n)
  archive(p,dest);case.rename(dest);prepared_page(p,ep);overview(p);print('Prepared:',dest,'; SAM3 not started')
 except (Exception,KeyboardInterrupt) as ex:save(stage/'failure.json',{'error':str(ex),'episode':ep});raise

def sam3(p,ep,new=False):
 import time
 started=time.monotonic()
 fp=gate(p,ep);prepared=p/'03_prepared'/ep;rec=load(prepared/'preparation.json')
 if rec['status']!='preprocessed' or rec['input_fingerprint']!=fp:raise ValueError('Prepared data not valid')
 from preparation_contract import contract,verify
 _,source,_=runtime();verify(prepared,contract(fp,HERE,source))
 dest=episode_root(p,ep)
 if (dest/'review/approved.json').exists() and not new:
  print('이미 승인된 에피소드입니다. 다음 단계: 07_track.py',dest);return
 from sam_settings import choose
 settings=choose(p,ep,new)
 from roi_workflow import preflight,run as run_roi,comparison_body
 preflight(prepared,settings,HERE)
 root,source,env=runtime();P=str(root/'phystwin-env/bin/python');S=str(root/'sam-env/bin/python')
 from resume_steps import step,contract,preserve
 if dest.exists() and (new or not (dest/'_resume/contract.json').exists()):
  print('기존 결과 보존 후 시작: 완료 체크포인트가 없거나 새 실행 요청')
  archive(p,dest)
 if not dest.exists():dest=p/'04_masks/pending'/ep
 elif dest.parent.name!='pending':dest=relocate(p,ep,'pending')
 dest.mkdir(parents=True,exist_ok=True)
 contract(dest,prepared,settings,[HERE/n for n in ['pipeline.py','roi_workflow.py','roi_camera.py','run_hand_roi.py','stage_refs.py']]+[source/'data_process/segment_object_sam3.py',HERE/'sam_roi_bundle/configs/sam_roi_candidate_v1.json'])
 try:
  from stage_refs import inputs
  inputs(dest,prepared)
  save(dest/'state.json',{'status':'running','input_fingerprint':fp,'settings':settings,'version':'sam3.1'})
  execute([P,str(HERE/'process_input_worker.py'),'validate',str(dest)],dest/'05.log',source,env)
  for cam in range(4):
   def baseline(cam=cam):
    for receipt in (dest/'_resume').glob('*.json'):
     if receipt.stem.startswith('roi_') or receipt.stem=='baseline_preview':preserve(receipt,dest)
    preserve(dest/f'mask/{cam}',dest)
    execute([S,str(HERE/'run_sam_progress.py'),str(source/'data_process/segment_object_sam3.py'),'--base_path',str(dest.parent),'--case_name',ep,'--object_prompts',settings['prompt'],'--object_label',settings['label'],'--scene_type',settings['scene'],'--version','sam3.1','--chunk_frames','300','--grounding_batch_size','4','--cameras',str(cam)],dest/'05.log',source,env)
    (dest/f'mask/{cam}').mkdir(parents=True,exist_ok=True)
   step(dest,f'baseline_camera_{cam}',baseline,[f'mask/{cam}',f'mask/mask_info_{cam}.json'])
  def previews():
   execute([P,str(HERE/'process_input_worker.py'),'masks',str(dest),'--out',str(dest)],dest/'06.log',source,env)
   execute([P,str(HERE/'inspect_masks.py'),'--case',str(dest),'--out',str(dest)],dest/'06.log',source,env)
  step(dest,'baseline_preview',previews,['mask_validation.json','mask_report.json','mask_contact.jpg']+[f'cam_{c}_mask.webm' for c in range(1,5)])
  run_roi(dest,HERE,S,P,source,env,execute)
  save(dest/'state.json',{'status':'mask_review_pending','method':'full_frame_plus_static_hand_roi_pilot_v1','human_approved':False,'candidate_mask':'roi_candidate/mask','baseline_mask':'mask','input_fingerprint':fp,'settings':settings,'version':'sam3.1'})
  (dest/'index.html').write_text(page(ep+' · 기준선 / ROI 손 마스크 비교',comparison_body()))
  from sam_completion import finish
  finish(dest,started)
  overview(p);print('Mask review:',dest/'index.html')
 except (Exception,KeyboardInterrupt) as ex:save(dest/'state.json',{'status':'interrupted_or_failed','error':str(ex)});raise

def targets(p,values,stage):
 base=p/('03_prepared' if stage=='05' else '01_raw');found=[]
 for v in values or [base]:
  v=v.expanduser().resolve()
  if v==base.resolve():candidates=sorted(base.glob('ep_*'))
  elif v.parent==base.resolve() and raw.EP.fullmatch(v.name):candidates=[v]
  else:raise ValueError(f'Use {base}/<episode> or {base}')
  for d in candidates:
   if not d.is_dir():raise ValueError('Input missing: '+str(d))
   if stage=='05' and not (d/'preparation.json').exists():raise ValueError('Preprocessing not complete')
   if d.name not in found:found.append(d.name)
 if not found:raise ValueError('No input episodes found')
 return found

def main(stage):
 parser=argparse.ArgumentParser(description={'01':'RAW 보존 복사','02':'QC 후 전처리까지만 실행','03':'전처리만 실행','05':'SAM3.1 별도 실행'}[stage]);parser.add_argument('input',nargs='*',type=Path);parser.add_argument('--project',type=Path,default=PROJECT);parser.add_argument('--new-attempt',action='store_true');parser.add_argument('--list-only',action='store_true');a=parser.parse_args();p=a.project.expanduser().resolve()
 try:
  if a.list_only:
   if stage=='01':
    for v in a.input:print(v.resolve())
   else:
    for ep in targets(p,a.input,stage):print('INPUT',p/('03_prepared' if stage=='05' else '01_raw')/ep,'OUTPUT',(episode_root(p,ep) if stage=='05' else p/'03_prepared'/ep))
   return 0
  init(p)
  with (p/'99_history/pipeline.lock').open('a') as lock:
   fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
   if stage=='01':
    if not a.input:raise ValueError('RAW input required')
    copy_raw(p,a.input)
   else:
    for ep in targets(p,a.input,stage):{'02':run_qc,'03':prepare,'05':sam3}[stage](p,ep,a.new_attempt)
  return 0
 except (ValueError,OSError,KeyError,subprocess.SubprocessError) as ex:print('중단:',ex,file=sys.stderr);return 1
