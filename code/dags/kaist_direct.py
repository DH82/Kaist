"""중앙 Airflow 하나에서 PC별 전체 파이프라인 6개를 생성합니다."""
import sys
from pathlib import Path
from datetime import datetime,timezone
sys.path.insert(0,str(Path(__file__).parent))
from direct_worker import poll, poll_mano_batch
from workers import dag_entries,worker,DEFAULT_WORKER,BATCH_DAG,MAX_PARALLEL_RUNS
from airflow.sdk import DAG,Param
from airflow.providers.standard.sensors.python import PythonSensor

STAGES=['prepare_raw','sam_settings_ready','sam3','mask_approved','preflight','track','tracking_approved',
        'prepare_reconstruction','reconstruct','hands','validate','publish']

# PC를 추가/활성화할 때 workers.json의 주소·이름·enabled와 연결 토큰을 설정하세요.
# 반복문을 PC 수만큼 복사하지 않습니다. 모델 코드도 공통 버전을 각 PC에 배포합니다.
for worker_id,mode,dag_id in dag_entries():
    pc=worker(worker_id,require_enabled=False)
    raw=mode=='raw'
    # 특정 촬영 에피소드를 기본값으로 고정하지 않고 실행할 때 반드시 받습니다.
    params={'episode_id':Param(type='string',pattern='^ep_[A-Za-z0-9_]+$')}
    if not raw:
        params.update(sam_prompt=Param(type='string',minLength=1,maxLength=200),
                      sam_scene=Param('2d',enum=['2d']),sam_settings_confirmed=Param(False,type='boolean'),
                      # text: 기존 텍스트 분할 / bbox_track: 첫 프레임 bbox+텍스트 단일 물체 추적(테스트 옵션)
                      sam_object_mode=Param('text',enum=['text','bbox_track']),
                      sam_object_bboxes=Param(None,type=['null','object']))
    with DAG(dag_id,schedule=None,catchup=False,start_date=datetime(2026,1,1,tzinfo=timezone.utc),
             max_active_runs=MAX_PARALLEL_RUNS,max_active_tasks=MAX_PARALLEL_RUNS,is_paused_upon_creation=True,
             params=params,tags=['kaist',worker_id,'연결됨' if pc['enabled'] else '미연결'],
             default_args={'pool':pc['pool'],'retries':0}) as dag:
        previous=None
        stages = STAGES[:STAGES.index('hands')] if mode=='day' else STAGES
        for stage in ['prepare_raw'] if raw else stages:
            # worker_id는 DAG에 고정합니다. 실행 파라미터를 바꿔 다른 PC로 우회하지 않습니다.
            task=PythonSensor(task_id=stage,python_callable=poll,
                              op_kwargs={'stage':stage,'worker_id':worker_id},mode='reschedule',
                              poke_interval=15,timeout=7*24*3600)
            if previous:previous >> task
            previous=task
    globals()[dag_id]=dag
# PC마다 에피소드 최대 MAX_PARALLEL_RUNS(2)개가 동시에 진행되고, 그 이상은 대기합니다.
# Sensor는 원격 GPU를 직접 점유하지 않으므로 실제 GPU 동시 연산 수 제한은 각 PC Worker의 GPU 슬롯(gpu_slots.py)이 담당합니다.

# 낮에 완료한 원본 실행을 선택한 순서로 이어갑니다.
with DAG(BATCH_DAG, schedule=None, catchup=False, max_active_runs=1,
         start_date=datetime(2026,1,1,tzinfo=timezone.utc), tags=['kaist', DEFAULT_WORKER],
         params={'sources': Param(type='array', minItems=1, maxItems=100)},
         default_args={'pool':worker(DEFAULT_WORKER,False)['pool'],'retries':0}) as mano_batch:
    PythonSensor(task_id='process_in_order', python_callable=poll_mano_batch,
                 mode='reschedule', poke_interval=20, timeout=7*24*3600)
