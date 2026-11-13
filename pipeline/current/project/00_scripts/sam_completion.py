import json,time,datetime
from resume_steps import snapshot
from episode_progress import PLAN
from sam_settings import save

def finish(dest,started):
 elapsed=round(time.monotonic()-started,1);total=0
 for name in PLAN:
  rec=json.loads((dest/'_resume'/f'{name}.json').read_text())
  if snapshot([dest/p for p in rec['files']],dest)!=rec['files']:raise ValueError('Final output integrity failure: '+name)
  total+=rec.get('seconds',0)
 if not (dest/'index.html').is_file():raise ValueError('Review page missing')
 warnings=0
 for d in [dest,dest/'roi_candidate']:
  report=json.loads((d/'mask_report.json').read_text())
  for camera in report['cameras'].values():
   for obj in camera.values():warnings+=len(obj['empty_or_missing_frames'])
 record={'status':'completed_review_pending','elapsed_seconds':elapsed,'completed_stage_seconds':round(total,1),'warning_count':warnings,'finished_at':datetime.datetime.now().astimezone().isoformat(),'human_approved':False,'verified_stages':len(PLAN)}
 save(dest/'completion.json',record)
 print(f'\n✓ SAM3 전체 완료 — {len(PLAN)}/{len(PLAN)}구간 산출물 검증 통과',flush=True)
 print(f'이번 실행: {elapsed/60:.1f}분 ({elapsed:.1f}초) | 완료 구간 누적 계산: {total/60:.1f}분',flush=True)
 print(f'누락·빈 마스크 기록: {warnings}건 | 자동 추론 완료, 사람 검수 대기',flush=True)
 print('결과 화면: '+str(dest/'index.html'),flush=True)
 return record
