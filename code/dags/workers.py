"""GPU PC registry shared by DAGs and UI. Edit workers.json, not six copies of a DAG."""
import json,os
from pathlib import Path
DEFAULT_WORKER='gpu_4090_a'
# PC 한 대에서 동시에 진행할 수 있는 에피소드 실행 수 (Airflow 실행 수·풀 슬롯·UI 검사 공통)
MAX_PARALLEL_RUNS=2
REGISTRY=Path(os.environ.get('KAIST_WORKERS_FILE',str(Path(__file__).with_name('workers.json'))))
def workers():
    rows=json.loads(REGISTRY.read_text())['workers']
    if len({w['id'] for w in rows})!=len(rows):raise ValueError('Duplicate worker id')
    return rows

def worker(worker_id=DEFAULT_WORKER,require_enabled=True):
    row=next((dict(w) for w in workers() if w['id']==worker_id),None)
    if row is None:raise ValueError('등록되지 않은 GPU PC입니다.')
    if require_enabled and not row.get('enabled'):raise ValueError('아직 연결 설정이 완료되지 않은 GPU PC입니다.')
    # NAS: use per-PC environment variables. The old single-PC variables still serve PC1.
    prefix=row['env_prefix']
    row['url']=os.environ.get(prefix+'_URL',os.environ.get('KAIST_WORKER_URL',row['url']) if worker_id==DEFAULT_WORKER else row['url']).rstrip('/')
    row['token_file']=os.environ.get(prefix+'_TOKEN_FILE',os.environ.get('KAIST_WORKER_TOKEN_FILE','') if worker_id==DEFAULT_WORKER else '')
    return row

DAY_DAG='kaist_pc1_reconstruction'
BATCH_DAG='kaist_pc1_mano_batch'

def dag_entries():
    # Six full pipelines. Keep PC1's old RAW-only DAG so its existing history/retries remain usable.
    return [(w['id'],'pipeline',w['dag_id']) for w in workers()]+[(DEFAULT_WORKER,'raw','kaist_direct_raw_4090'), (DEFAULT_WORKER,'day',DAY_DAG)]

def dag_worker(dag_id):
    return next((wid for wid,mode,dag in dag_entries() if dag==dag_id),DEFAULT_WORKER if dag_id=='kaist_mac_to_4090_test' else None)
