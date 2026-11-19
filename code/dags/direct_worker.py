"""Central Airflow worker client; SSH tunnel to GPU PC, no remote Airflow API."""
import json,os,re,subprocess,threading,time
from pathlib import Path
import httpx
import sys
sys.path.insert(0,str(Path(__file__).parent))
from workers import worker,DEFAULT_WORKER
BASE=worker()['url']  # Older callers retain PC1 default; new paths always pass worker_id.
_SECRETS={}

def token(worker_id=DEFAULT_WORKER):
 pc=worker(worker_id)
 key=(worker_id,pc['token_file'])
 if key not in _SECRETS:
  if pc['token_file']:_SECRETS[key]=Path(pc['token_file']).read_text().strip()
  elif worker_id==DEFAULT_WORKER:
   # Existing Mac pilot only. NAS and additional PCs must configure token files.
   r=subprocess.run(['ssh','-S','/tmp/rira_v5_result.sock','-o','BatchMode=yes','-o','ConnectTimeout=10','cw-wfm@192.168.200.17','cat /home/cw-wfm/Desktop/rira/airflow/worker/secret'],text=True,capture_output=True,timeout=15,check=True)
   _SECRETS[key]=r.stdout.strip()
  else:raise ValueError('GPU PC token file is not configured: '+worker_id)
 return _SECRETS[key]

def call(method,path,worker_id=DEFAULT_WORKER,**kwargs):
 pc=worker(worker_id)
 with httpx.Client(base_url=pc['url'],trust_env=False,timeout=150) as client:
  r=client.request(method,path,headers={'Authorization':'Bearer '+token(worker_id)},**kwargs)
  r.raise_for_status();return r.json()

def request_for(stage,run_id,params):
 req={'stage':stage,'episode':params['episode_id'],'run_id':run_id,'sam_prompt':params.get('sam_prompt',''),
         'sam_scene':params.get('sam_scene','2d'),'sam_confirmed':params.get('sam_settings_confirmed',False)}
 if params.get('raw_episode_id') and params['raw_episode_id']!=params['episode_id']:req['raw_episode']=params['raw_episode_id']
 # NAS 결과 폴더(results/pc1~pc6): 실행한 사용자(PC1~PC6)의 폴더. publish 단계에만 넣어 다른 단계의 작업 요청은 그대로 둡니다.
 pc=re.sub(r'[\s_-]+','',str(params.get('operator_id') or '')).lower()
 if stage=='publish' and re.fullmatch(r'pc[1-6]',pc):req['result_pc']=pc
 # 처리 fps: 15fps 실행만 키를 넣습니다. 30fps(옵션이 생기기 전 실행 포함)는 작업 요청이 그대로입니다.
 if params.get('target_fps')==15:req['target_fps']=15
 # Optional SAM3 object mode. Default text mode adds no keys, so existing job requests are unchanged.
 if params.get('sam_object_mode','text')=='point_track':
  req.update(sam_object_mode='point_track',sam_object_points=params.get('sam_object_points'))
 elif params.get('sam_object_mode','text')!='text':
  req.update(sam_object_mode=params['sam_object_mode'],sam_object_bboxes=params.get('sam_object_bboxes'))
 return req

POKE_WAIT_SECONDS=8  # 한 번 확인할 때 짧은 작업의 완료를 기다리는 최대 시간

def poll(stage,worker_id=DEFAULT_WORKER,**ctx):
 worker(worker_id)
 if ctx['params'].get('worker_id',worker_id)!=worker_id:raise ValueError('DAG and worker mismatch')
 req=request_for(stage,ctx['run_id'],ctx['params'])
 result=call('POST','/worker/jobs/start',json=req,worker_id=worker_id)
 # 승인 확인처럼 몇 초면 끝나는 작업은 이번 확인 안에서 결과를 기다려, 다음 확인 주기까지 기다리지 않고 바로 넘어갑니다.
 import time
 deadline=time.monotonic()+POKE_WAIT_SECONDS
 while result['state'] in ('queued','running') and time.monotonic()<deadline:
  time.sleep(1);result=call('POST','/worker/jobs/status',json=req,worker_id=worker_id)
 if result['state'] in ('failed','interrupted'):
  log=call('POST','/worker/log',json=req,worker_id=worker_id)['log'];print(log)
  raise RuntimeError('GPU worker failed at '+stage+'; see worker log above')
 print(json.dumps({'stage':stage,'state':result['state'],'remote_log':result.get('log'),'progress':result.get('progress')}))
 return result['state']=='success'


MANO_STAGES = ('hands', 'validate', 'publish')

MANO_LABELS = {'hands': 'MANO 피팅', 'validate': '결과 검사', 'publish': '최종 저장'}

# 업데이트 전 대기열 3D 복원 다시(일괄 실행 conf redo_3d): 업데이트 전 코드로 고정된 에피소드만 3D 복원부터 다시 합니다.
# 그대로 두면 MANO 피팅이 'Code/config changed during run'으로 실패합니다. 업데이트 뒤에 만든 실행은 그대로 MANO만 합니다.
REDO_START = 'prepare_reconstruction'
REDO_STAGES = ('prepare_reconstruction', 'reconstruct')
REDO_LABELS = {'prepare_reconstruction': '3D 복원 준비(다시)', 'reconstruct': '3D 복원(다시)'}

# 3D 복원 일괄 실행: 낮 실행(첫 실행)은 추적 검수까지만 하므로, 일괄 실행이 에피소드마다 아래 단계를 순서대로 합니다.
# 이미 3D 복원까지 끝난 에피소드(예전 'MANO 대기')는 끝난 단계를 건너뛰고 MANO부터 합니다.
BATCH_STAGES = REDO_STAGES + MANO_STAGES
BATCH_LABELS = {'prepare_reconstruction': '3D 복원 준비', 'reconstruct': '3D 복원', **MANO_LABELS}


def started_ts(run_obj):
    when = run_obj.start_date or run_obj.queued_at or run_obj.logical_date
    if isinstance(when, str):
        import datetime as dt
        when = dt.datetime.fromisoformat(when.replace('Z', '+00:00'))
    return when.timestamp() if when else 0


CLOCK_SLACK = 600  # 일괄 실행 시작 시각(Airflow)과 Worker 초기화 기록 시각 사이의 시계 차이 허용(초)


def redo_started(reset, since):
    """이번 일괄 실행이 시작한 '3D 복원 다시' 초기화 기록인지(같은 실행의 예전 수동 초기화와 구분)."""
    return reset['state'] != 'not_started' and reset.get('start') == REDO_START and reset.get('started', 0) >= since - CLOCK_SLACK


def redo_3d(request, worker_call, run, conf, reset, since):
    """한 에피소드의 '3D 복원 다시'를 한 걸음 진행합니다.
    반환: ('done', 메모) 다시 할 필요 없음/다시 끝남 → MANO 진행 · ('pending', 메모) 다음 확인에 이어서 · ('skip', 이유).
    reset: 이 실행의 Worker 초기화 상태(reset-status). 이번 일괄 실행이 시작한 초기화 기록이 있으면 그 결과를 따르므로,
    다시 고정한 뒤에는 코드 비교를 다시 하지 않습니다."""
    ids = {'episode': conf['episode_id'], 'run_id': run}
    hands = request('hands', 'status')
    if hands['state'] == 'interrupted' and hands.get('stopped_by_user'):
        return 'skip', 'MANO 피팅 강제 종료됨'
    if not redo_started(reset, since):
        if hands['state'] != 'not_started':
            return 'done', ''                                   # MANO를 이미 시작한 에피소드는 그대로 이어갑니다.
        drift = worker_call('drift', ids)
        if not drift.get('changed'):
            # 코드가 그대로면 MANO만 합니다. 앞선 일괄 실행에서 다시 고정한 뒤 3D 복원 다시가 실패·중단됐으면 그 단계부터 잇습니다.
            if request('reconstruct', 'status')['state'] == 'success' or reset['state'] != 'success' or reset.get('start') != REDO_START:
                return 'done', ''
            return redo_stages(request)
        if not drift.get('pinned') or drift.get('transfer'):
            return 'skip', '업데이트 전 코드로 만든 3D 결과 · 이 PC에서 처음부터 만든 실행이 아니라 3D 복원을 다시 할 수 없음'
        if not drift.get('code_only'):
            return 'skip', '업데이트로 설정이 바뀜(' + ', '.join(drift['changed'][:4]) + ') · 3D 복원부터 다시 할 수 없어 새 실행 필요'
        worker_call('reset', {**ids, 'start': REDO_START})
        return 'pending', '업데이트 전 3D 결과 → 3D 복원 다시 준비(이전 결과 보관) · 바뀐 코드: ' + ', '.join(drift['changed'][:4]) + \
            (' 외' if len(drift['changed']) > 4 else '')
    if reset['state'] == 'running':
        return 'pending', '3D 복원 다시 준비 중(이전 결과 보관)'
    if reset['state'] != 'success':
        return 'skip', '3D 복원 다시 준비 실패: ' + str(reset.get('error', ''))[-200:]
    return redo_stages(request)


def redo_stages(request):
    for stage in REDO_STAGES:
        current = request(stage, 'status')
        if current['state'] == 'interrupted' and current.get('stopped_by_user'):
            return 'skip', REDO_LABELS[stage] + ' 강제 종료됨'
        result = request(stage, 'start')
        state = result['state']
        if state in ('failed', 'interrupted'):
            return 'skip', REDO_LABELS[stage] + (' 강제 종료됨' if result.get('stopped_by_user') else ' ' + state)
        if state != 'success':
            progress = result.get('progress') or {}
            detail = f" · {progress.get('label')} {progress.get('done')}/{progress.get('total')}" if progress.get('total') else ''
            return 'pending', REDO_LABELS[stage] + ' ' + state + detail
    return 'done', '3D 복원 다시 완료'

# 평일(월~금) 오전 9시가 되면 그때 진행 중인 에피소드까지만 끝내고, 다음 에피소드는 시작하지 않습니다.
# 기준 시각은 일괄 실행을 시작한 뒤 처음 오는 평일 오전 9시입니다(예: 금 18시 시작 → 월 09:00).
MANO_TZ = 'Asia/Seoul'
MANO_CUTOFF_HOUR = 9

def mano_cutoff(started):
    import datetime as dt
    from zoneinfo import ZoneInfo
    tz = ZoneInfo(MANO_TZ)
    if isinstance(started, str):
        started = dt.datetime.fromisoformat(started.replace('Z', '+00:00'))
    local = (started or dt.datetime.now(dt.timezone.utc)).astimezone(tz)
    day = local.date()
    while True:
        cut = dt.datetime.combine(day, dt.time(MANO_CUTOFF_HOUR), tz)
        if cut > local and cut.weekday() < 5:
            return cut
        day += dt.timedelta(days=1)

def started_in_batch(request, since):
    """이번 일괄 실행이 이 에피소드의 단계를 하나라도 시작했는지(일괄 실행 전에 끝난 3D 복원은 세지 않음)."""
    for stage in BATCH_STAGES:
        st = request(stage, 'status')
        if st['state'] != 'not_started' and (st.get('started') or st.get('finished') or st.get('created') or 0) >= since - CLOCK_SLACK:
            return True
    return False


def poll_mano_batch(**ctx):
    """선택한 순서대로 에피소드마다 3D 복원 준비→3D 복원→MANO 피팅→결과 검사→최종 저장을 진행합니다(이미 끝난 단계는 건너뜀).
    3D 복원 전 에피소드는 실행 고정 이후 코드가 바뀌었는지 확인해, 바뀌었으면 3D 복원 준비부터 다시 고정한 뒤 진행합니다(redo_3d 참고).
    redo_3d가 켜져 있으면 이미 3D 복원까지 끝난 에피소드 중 업데이트 전 코드로 만든 것도 3D 복원부터 다시 합니다.
    한 에피소드가 실패하거나(단계 실패·3D 복원 미완료·작업 요청 오류) UI에서 강제 종료되면
    일괄 실행을 멈추지 않고 그 에피소드만 건너뛰어 다음 에피소드로 넘어갑니다.
    실패한 작업은 Worker가 다시 시작하지 않으므로(start는 실패 상태를 그대로 돌려줌) 다음 확인에서도 계속 건너뜁니다.
    GPU PC 연결 오류는 에피소드 문제가 아니므로 건너뛰지 않고 다음 확인 때 다시 시도합니다."""
    import datetime as dt
    sources = ctx['dag_run'].conf['sources']
    run_obj = ctx['dag_run']
    cutoff = mano_cutoff(run_obj.start_date or run_obj.queued_at or run_obj.logical_date)
    # 업데이트 전 코드로 고정된 3D 결과는 항상 3D 복원부터 다시 합니다. 그대로 MANO만 하면 반드시
    # 'Code/config changed during run'으로 실패하므로, 실행 옵션(redo_3d)을 꺼도 건너뛰지 않습니다(2026-11-18).
    redo = True
    since = started_ts(run_obj)
    skipped = []
    for n, source in enumerate(sources, 1):
        conf = source['conf']
        run = source['run_id']
        tag = f"[{n}/{len(sources)}] {conf['episode_id']}"
        def request(stage, action):
            return call('POST', '/worker/jobs/' + action, worker_id=DEFAULT_WORKER,
                        json=request_for(stage, run, conf))
        def skip(reason):
            print(tag, reason, '· 이 에피소드는 건너뛰고 다음으로 진행합니다')
            skipped.append(f"{conf['episode_id']}({reason})")
        def worker_call(action, body):
            return call('POST', '/worker/jobs/' + action, worker_id=DEFAULT_WORKER, json=body)

        try:
            if request('publish', 'status')['state'] == 'success':
                print(tag, '완료')
                continue
            need3d = request('reconstruct', 'status')['state'] != 'success'
            check3d = redo or need3d
            # 이번 일괄 실행이 이미 3D 복원 다시를 시작한 에피소드는 오전 9시 마감과 관계없이 끝까지 진행합니다.
            reset = worker_call('reset-status', {'episode': conf['episode_id'], 'run_id': run, 'start': REDO_START}) if check3d else None
            redoing = bool(reset) and redo_started(reset, since)
            # 이번 일괄 실행에서 아직 아무 단계도 시작하지 않은 에피소드: 평일 오전 9시가 지났으면 여기서 멈추고 나머지는 대기로 남깁니다.
            # 앞에서 실패해 건너뛴 에피소드는 대기 목록에 넣지 않습니다(이미 실패로 표시됨).
            if not redoing and dt.datetime.now(cutoff.tzinfo) >= cutoff and not started_in_batch(request, since):
                left = [s['conf']['episode_id'] for s in sources[n - 1:]]
                print(f"평일 오전 {MANO_CUTOFF_HOUR}시({cutoff:%m-%d %H:%M}) 이후라 새 에피소드를 시작하지 않습니다. "
                      f"대기로 남김 {len(left)}개: {', '.join(left)}")
                break
            if check3d:
                phase, note = redo_3d(request, worker_call, run, conf, reset, since)
                if phase == 'skip':
                    skip(note)
                    continue
                if note:
                    print(tag, note)
                if phase == 'pending':
                    return False
            outcome = 'success'
            for stage in BATCH_STAGES:
                current = request(stage, 'status')
                if current['state'] == 'success':
                    continue
                if current['state'] == 'interrupted' and current.get('stopped_by_user'):
                    skip(BATCH_LABELS[stage] + ' 강제 종료됨')
                    outcome = 'skipped'
                    break
                result = request(stage, 'start')
                state = result['state']
                progress = result.get('progress') or {}
                detail = f" · {progress.get('label')} {progress.get('done')}/{progress.get('total')}" if progress.get('total') else ''
                print(tag, BATCH_LABELS[stage], state + detail)
                if state in ('failed', 'interrupted'):
                    skip(BATCH_LABELS[stage] + (' 강제 종료됨' if result.get('stopped_by_user') else ' ' + state))
                    outcome = 'skipped'
                    break
                if state != 'success':
                    outcome = 'pending'
                    break
            if outcome == 'pending':
                return False
        except httpx.TransportError as e:
            # GPU PC 연결 문제는 에피소드 탓이 아니므로 건너뛰지 않고, 일괄 실행도 끝내지 않은 채 다음 확인 때 다시 시도합니다.
            print(tag, 'GPU PC 연결 오류 · 다음 확인 때 다시 시도합니다:', str(e)[:200])
            return False
        except (httpx.HTTPStatusError, ValueError, KeyError) as e:
            skip('작업 요청 오류: ' + str(e)[:200])
    if skipped:
        print(f"건너뛴 에피소드 {len(skipped)}개: {', '.join(skipped)}")
    return True
