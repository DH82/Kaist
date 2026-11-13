#!/usr/bin/env python3
"""Generate fresh PC configuration without changing an existing deployment."""
import argparse
import os
from pathlib import Path
import re
import secrets
import subprocess

ROOT=Path(__file__).resolve().parents[1]
DEFAULT_WEIGHTS=Path('/home/cw-wfm/Documents/Codex/2026-10-20/https-drive-google-com-drive-folders/outputs/WFM_KAIST/workspace')

def weight_paths(root=ROOT):
    lines=(root/'gpu/env/weights.txt').read_text().splitlines()
    return [l.split()[0] for l in lines if l.strip() and not l.lstrip().startswith('#')]

def check_weights(weights,root=ROOT):
    missing=[rel for rel in weight_paths(root) if not (weights/rel).exists()]
    if missing:raise ValueError(f'Weights not found under {weights}: '+', '.join(missing))

def cuda_arch():
    caps=subprocess.check_output(['nvidia-smi','--query-gpu=compute_cap','--format=csv,noheader'],text=True).split()
    return ';'.join(sorted(set(caps)))

def prepare(root, pc_id, nas, nas_gid, project, weights=DEFAULT_WEIGHTS, arch=None):
    if not re.fullmatch(r'pc[1-9][0-9]*',pc_id):raise ValueError('PC ID must be pc1, pc2, ...')
    if (root/'.env').exists():raise ValueError('.env already exists; existing deployments are not overwritten')
    nas,project,weights=nas.resolve(),project.resolve(),weights.resolve()
    for path in (nas,project,weights,root):
        if any(c in str(path) for c in '\n\r$'):raise ValueError('Unsupported path character')
    check_weights(weights,root)
    arch=arch or cuda_arch()
    if not re.fullmatch(r'[0-9.;+A-Za-z]+',arch):raise ValueError(f'Unexpected GPU compute capability: {arch}')
    if not nas.is_mount() or not (nas/'rgbd_raw').is_dir():raise ValueError('Mount NAS with rgbd_raw before setup')
    state=root/'gpu_state/worker';(state/'jobs').mkdir(parents=True,exist_ok=True)
    (state/'gpu_4090_a.lock').touch();project.mkdir(parents=True,exist_ok=True)
    host_ip=subprocess.check_output(['hostname','-I'],text=True).split()[0]
    values=dict(HOST_IP=host_ip,POSTGRES_PASSWORD=secrets.token_hex(32),AIRFLOW_JWT_SECRET=secrets.token_hex(32),
                KAIST_UI_ALLOWED_HOSTS=f'{host_ip}:18083,127.0.0.1:18083,localhost:18083',
                KAIST_AIRFLOW_PUBLIC_URL=f'http://{host_ip}:18082',PC_ID=pc_id,PC_LABEL=pc_id,HOST_UID=str(os.getuid()),NAS_MOUNT=str(nas),
                NAS_RAW_HOST_PATH=str(nas/'rgbd_raw'),NAS_GID=str(nas_gid),
                KAIST_REV_ROOT=str(root/'pipeline/current'),KAIST_WEIGHTS_ROOT=str(weights),TORCH_CUDA_ARCH_LIST=arch,
                GPU_PROJECT_DIR=str(project),GPU_STATE_DIR=str(root/'gpu_state'),
                GPU_LOCK_FILE=str(state/'gpu_4090_a.lock'),GPU_WORKER_PORT='8090',
                WORKER_URL='http://127.0.0.1:8090',WORKER_SECRET_FILE=str(state/'secret'),
                UI_PORT='18083',AIRFLOW_PORT='18082',POSTGRES_PORT='15432',
                UI_BIND='0.0.0.0',AIRFLOW_BIND='0.0.0.0')
    fd=os.open(root/'.env',os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,'w') as f:f.write(''.join(k+'='+v+'\n' for k,v in values.items()))
    print('Created .env; generated secrets will be local to this PC.')

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--pc-id',required=True);p.add_argument('--nas',type=Path,default=Path('/mnt/kaist-data'))
    p.add_argument('--nas-gid',type=int,required=True)
    p.add_argument('--project',type=Path,required=True)
    p.add_argument('--weights',type=Path,default=DEFAULT_WEIGHTS,help='folder that already holds the model weights (WFM_KAIST/workspace layout)')
    p.add_argument('--cuda-arch',help='TORCH_CUDA_ARCH_LIST override, e.g. 8.9 (default: detected by nvidia-smi)')
    a=p.parse_args();prepare(ROOT,a.pc_id,a.nas,a.nas_gid,a.project,a.weights,a.cuda_arch)
