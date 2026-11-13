"""Connect the delivered static hand ROI pilot without replacing baseline masks."""
import json,shutil,hashlib,tempfile
from pathlib import Path

def preflight(prepared,settings,here):
 cfg=json.loads((here/'sam_roi_bundle/configs/sam_roi_candidate_v1.json').read_text())
 meta=json.loads((prepared/'metadata.json').read_text())
 if settings['scene'].lower()!='2d':raise ValueError('ROI pilot currently supports scene 2d only')
 if not 0<int(meta['frame_num'])<=cfg['max_frames']:raise ValueError('ROI pilot requires 1..300 frames; no automatic trim')
 if int(meta.get('num_cameras',4))!=4:raise ValueError('This review pipeline requires 4 cameras')
 for cam in range(4):
  if not (prepared/f'color/{cam}.mp4').is_file():raise ValueError(f'Camera {cam} video missing')
 return cfg

def run(dest,here,python,preview_python,source,env,execute):
 candidate=dest/'roi_candidate';bundle=here/'sam_roi_bundle'
 from resume_steps import step,preserve
 candidate.mkdir(exist_ok=True)
 for cam in range(4):
  def infer(cam=cam):
   with tempfile.TemporaryDirectory(prefix='wfm-roi-') as temp:
    infer_camera(cam,Path(temp)/'candidate')
  def infer_camera(cam,work):
   preserve(dest/'_resume/roi_preview.json',dest)
   preserve(work,dest)
   execute([python,str(here/'run_hand_roi.py'),'--source-script',str(source/'data_process/segment_object_sam3.py'),'--case',str(dest),'--out',str(work),'--profile',str(bundle/'configs/sam_roi_candidate_v1.json'),'--cameras',str(cam)],dest/'05_roi.log',source,env)
   data=json.loads((work/'candidate_record.json').read_text())
   if len(data['results'])!=1 or data['results'][0]['camera']!=cam:raise ValueError('Incomplete camera record')
   target=candidate/'mask'/str(cam);preserve(target,dest);target.parent.mkdir(exist_ok=True)
   if (work/'mask'/str(cam)).exists():shutil.move(str(work/'mask'/str(cam)),target)
   else:target.mkdir()
   (candidate/f'camera_{cam}.json').write_text(json.dumps(data))
   object_link=target/'0'
   if object_link.is_symlink():object_link.unlink()
   from stage_refs import link
   if (dest/f'mask/{cam}/0').exists():link(object_link,dest/f'mask/{cam}/0')
  step(dest,f'roi_camera_{cam}',infer,[f'roi_candidate/mask/{cam}',f'roi_candidate/camera_{cam}.json'])
 records=[json.loads((candidate/f'camera_{c}.json').read_text()) for c in range(4)]
 combined=dict(records[0]);combined['results']=[r['results'][0] for r in records]
 (candidate/'candidate_record.json').write_text(json.dumps(combined))
 record=json.loads((candidate/'candidate_record.json').read_text())
 if len(record['results'])!=4 or record['human_approved'] is not False:raise ValueError('Incomplete ROI candidate record')
 # Verify that the candidate did not modify object masks.
 base=list((dest/'mask').glob('*/0/*.png'))
 if not base:raise ValueError('No baseline object masks')
 for f in base:
  other=candidate/f.relative_to(dest)
  if not other.is_file() or f.read_bytes()!=other.read_bytes():raise ValueError('ROI changed an object mask')
 from stage_refs import inputs
 inputs(candidate,__import__('stage_refs').source(dest))
 def previews():
  execute([preview_python,str(here/'process_input_worker.py'),'masks',str(candidate),'--out',str(candidate)],dest/'06_roi.log',source,env)
  execute([preview_python,str(here/'inspect_masks.py'),'--case',str(candidate),'--out',str(candidate)],dest/'06_roi.log',source,env)
 step(dest,'roi_preview',previews,['roi_candidate/mask_validation.json','roi_candidate/mask_report.json','roi_candidate/mask_contact.jpg']+[f'roi_candidate/cam_{c}_mask.webm' for c in range(1,5)])
 record['integration']={'baseline_preserved':True,'object_masks_verified_identical':True,'grounding_batch_size':4,'runner_sha256':hashlib.sha256((bundle/'tools/sam_roi_candidate.py').read_bytes()).hexdigest()}
 (candidate/'candidate_record.json').write_text(json.dumps(record,ensure_ascii=False,indent=2))

def comparison_body():
 body='<p>손 ROI 4배 확대 후보 · GPU 개선 효과 미검증 · 검수 전 후단 적용 금지</p><p>왼쪽: 전체 화면 기준선 / 오른쪽: ROI 후보. 초록: 물체, 분홍: 손. 기준선을 재생·탐색하면 후보도 같은 시점으로 이동합니다.</p><p>손목 손실, 손가락 누락, 팔 혼입, 가림·재등장, 양손, 물체와의 중첩을 확인하세요. IoU는 기준선과의 일치도이며 정확도가 아닙니다. 후보에는 기준선의 형태학·면적 급증 후처리가 없습니다.</p>'
 for cam in range(1,5):
  body+=f'<h2>Camera {cam}</h2><div class="grid"><div>Baseline<video id="b{cam}" controls src="cam_{cam}_mask.webm"></video></div><div>ROI candidate<video id="r{cam}" controls src="roi_candidate/cam_{cam}_mask.webm"></video></div></div>'
 body+='<p><a href="roi_candidate/candidate_record.json">프레임별 변경·fallback 기록</a> · <a href="mask_report.json">기준선 누락 검사</a> · <a href="roi_candidate/mask_report.json">후보 누락 검사</a></p>'
 body+='''<script>for(let c=1;c<=4;c++){let b=document.getElementById('b'+c),r=document.getElementById('r'+c);b.onplay=()=>{r.currentTime=b.currentTime;r.play().catch(()=>{});};b.onpause=()=>r.pause();b.onseeking=()=>{r.currentTime=b.currentTime;};b.ontimeupdate=()=>{if(Math.abs(r.currentTime-b.currentTime)>.12)r.currentTime=b.currentTime;};b.onratechange=()=>r.playbackRate=b.playbackRate;}</script>'''
 return body
