#!/usr/bin/env python3
"""One-command deployment of UI, Airflow, PostgreSQL and GPU Worker."""
import argparse
import json
import os
from pathlib import Path
import secrets
import re
import socket
import subprocess
import sys

from prepare_pc import DEFAULT_WEIGHTS, cuda_arch
from weights import resolve_weights
from verify_source import verify

ROOT = Path(__file__).resolve().parents[1]


def run(args, **kwargs):
    return subprocess.run(args, check=True, **kwargs)


def read_env(path):
    result = {}
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            key, value = line.split('=', 1)
            value = value.strip()
            if value[:1] in ('"', "'") and value[-1:] == value[:1]:
                value = value[1:-1]
            result[key.strip()] = value
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--env-file', type=Path, default=ROOT / '.env')
    parser.add_argument('--state-dir', type=Path)
    parser.add_argument('--stack-name', help='Separate Compose project, useful for side-by-side validation')
    parser.add_argument('--pc-id')
    parser.add_argument('--nas', type=Path)
    parser.add_argument('--nas-gid', type=int)
    parser.add_argument('--project', type=Path)
    parser.add_argument('--weights', type=Path, help='Root folder holding all existing model assets')
    parser.add_argument('--weight', action='append', default=[], metavar='NAME=PATH', help='Independent model/cache location; repeat for multiple assets')
    for name in ('ui', 'airflow', 'worker', 'postgres'):
        parser.add_argument(f'--{name}-port', type=int)
    parser.add_argument('--airflow-log-port', type=int)
    parser.add_argument('--airflow-trigger-log-port', type=int)
    parser.add_argument('--configure-only', action='store_true', help='Write configuration without building or starting services')
    parser.add_argument('--check', action='store_true', help='Preflight only; no build or service restart')
    args = parser.parse_args()
    os.chdir(ROOT)
    env_file = args.env_file.resolve()
    values = read_env(env_file)
    def setting(key, default):
        return values.get(key) or str(default)
    stack_name = args.stack_name or setting('COMPOSE_PROJECT_NAME', 'kaist-pc')
    if not re.fullmatch(r'[a-z0-9][a-z0-9_-]*', stack_name):
        raise ValueError('Invalid Compose project name')
    default_state = ROOT if stack_name == 'kaist-pc' else ROOT / '.runtime' / stack_name
    state = (args.state_dir or Path(setting('STACK_STATE_DIR', default_state))).resolve()
    default_nas = Path('/mnt/kaist-data')
    if not default_nas.is_mount():
        default_nas = state / 'data'
    nas = (args.nas or Path(setting('NAS_MOUNT', default_nas))).expanduser().resolve()
    pc = args.pc_id or setting('PC_ID', 'pc1')
    if not re.fullmatch(r'pc[1-9][0-9]*', pc):
        raise ValueError('PC ID must be pc1, pc2, ...')
    default_project = Path.home() / 'kaist_work' / pc if stack_name == 'kaist-pc' else state / 'project'
    project = (args.project or Path(setting('GPU_PROJECT_DIR', default_project))).resolve()
    weight_root = args.weights or values.get('KAIST_WEIGHTS_ROOT')
    if not weight_root and DEFAULT_WEIGHTS.is_dir():
        weight_root = DEFAULT_WEIGHTS
    if not weight_root and not args.weight and not any(k.startswith('WEIGHT_') and v for k,v in values.items()) and sys.stdin.isatty():
        weight_root = input('모든 weight가 들어 있는 폴더 (각 경로를 따로 입력하려면 Enter): ').strip()
    weights = Path(weight_root).expanduser().resolve() if weight_root else None
    ports = {name: supplied or int(setting(key, default)) for name, supplied, key, default in (
        ('ui', args.ui_port, 'UI_PORT', 18083), ('airflow', args.airflow_port, 'AIRFLOW_PORT', 18082),
        ('worker', args.worker_port, 'GPU_WORKER_PORT', 8090), ('postgres', args.postgres_port, 'POSTGRES_PORT', 15432))}
    ports['airflow-log'] = args.airflow_log_port or int(setting('AIRFLOW_LOG_PORT', ports['airflow'] + 711))
    ports['airflow-trigger-log'] = args.airflow_trigger_log_port or int(setting('AIRFLOW_TRIGGER_LOG_PORT', ports['airflow'] + 712))
    if len(set(ports.values())) != 6 or any(not 1024 <= p <= 65535 for p in ports.values()):
        raise ValueError('Six distinct ports in 1024..65535 are required')
    run(['docker', 'info'], stdout=subprocess.DEVNULL)
    run(['docker', 'compose', 'version'], stdout=subprocess.DEVNULL)
    run(['nvidia-smi', '--query-gpu=name', '--format=csv,noheader'])
    verify()
    saved_weights = {} if args.weights else values
    values.update(resolve_weights(weights, saved_weights, args.weight, interactive=sys.stdin.isatty() and not args.check))
    if args.nas or values.get('NAS_MOUNT'):
        if not (nas / 'rgbd_raw').is_dir() or not (nas / 'results').is_dir():
            raise ValueError(f'Storage must contain rgbd_raw and results: {nas}')
    ip = setting('HOST_IP', subprocess.check_output(['hostname', '-I'], text=True).split()[0])
    uid = setting('HOST_UID', os.getuid())
    if int(uid) == 0:
        raise ValueError('Run as your ordinary Docker-enabled user (HOST_UID must not be root)')
    arch = setting('TORCH_CUDA_ARCH_LIST', cuda_arch())
    if not arch.replace(';', ' ').split() or not all(re.fullmatch(r'[0-9]+\.[0-9]+(?:\+PTX)?', a) for a in arch.replace(';', ' ').split()):
        raise ValueError('Use numeric CUDA architectures, e.g. 8.9 or 8.9;12.0+PTX')
    worker_state = Path(setting('GPU_STATE_DIR', state / 'gpu_state')).resolve()
    worker_url = f'http://127.0.0.1:{ports["worker"]}'
    worker_secret = worker_state / 'worker/secret'
    # All clients must use the worker started by this stack, including existing configurations.
    values.update(COMPOSE_PROJECT_NAME=stack_name, HOST_UID=uid, HOST_IP=ip, PC_ID=pc, PC_LABEL=setting('PC_LABEL', pc),
                  STACK_STATE_DIR=str(state), NAS_MOUNT=str(nas), NAS_RAW_HOST_PATH=str(nas / 'rgbd_raw'),
                  NAS_GID=str(args.nas_gid if args.nas_gid is not None else setting('NAS_GID', ((nas / 'results').stat().st_gid if (nas / 'results').exists() else os.getgid()))),
                  GPU_PROJECT_DIR=str(project), GPU_STATE_DIR=str(worker_state),
                  GPU_LOCK_FILE=setting('GPU_LOCK_FILE', worker_state / 'worker/gpu_4090_a.lock'),
                  KAIST_WEIGHTS_ROOT=str(weights) if weights else '', TORCH_CUDA_ARCH_LIST=arch,
                  WORKER_URL=worker_url, WORKER_SECRET_FILE=str(worker_secret),
                  UI_PORT=str(ports['ui']), AIRFLOW_PORT=str(ports['airflow']),
                  GPU_WORKER_PORT=str(ports['worker']), POSTGRES_PORT=str(ports['postgres']),
                  AIRFLOW_LOG_PORT=str(ports['airflow-log']), AIRFLOW_TRIGGER_LOG_PORT=str(ports['airflow-trigger-log']),
                  UI_BIND=setting('UI_BIND', '0.0.0.0'), AIRFLOW_BIND=setting('AIRFLOW_BIND', '0.0.0.0'),
                  KAIST_UI_ALLOWED_HOSTS=f'{ip}:{ports["ui"]},127.0.0.1:{ports["ui"]},localhost:{ports["ui"]}',
                  KAIST_AIRFLOW_PUBLIC_URL=f'http://{ip}:{ports["airflow"]}',
                  POSTGRES_PASSWORD=setting('POSTGRES_PASSWORD', secrets.token_hex(32)),
                  AIRFLOW_JWT_SECRET=setting('AIRFLOW_JWT_SECRET', secrets.token_hex(32)))
    env = {**os.environ, **values}
    # Use explicit environment values even during first install, before the file exists.
    compose = ['docker', 'compose', '--env-file', str(env_file if env_file.exists() else Path('/dev/null'))]
    run(compose + ['config', '--quiet'], env=env)
    # A native service or another Compose project must never be stopped implicitly.
    running = subprocess.check_output(compose + ['ps', '--services', '--status', 'running'], env=env, text=True).split()
    for name, port in ports.items():
        service = 'gpu-worker' if name == 'worker' else ('airflow' if name.startswith('airflow') else name)
        with socket.socket() as sock:
            sock.settimeout(1)
            if sock.connect_ex(('127.0.0.1', port)) == 0 and service not in running:
                raise ValueError(f'Port {port} is already in use outside this stack. Select --{name}-port or explicitly stop the existing service.')
    if any('\n' in v or '\r' in v or "'" in v for v in values.values()):
        raise ValueError('Configuration values cannot contain newlines or single quotes')
    if args.check:
        print('Preflight passed. No files or services changed.')
        return
    for directory in (state / 'state', state / 'intake_state', state / 'config', state / 'secrets', worker_state / 'worker/jobs', project, nas / 'rgbd_raw', nas / 'results'):
        directory.mkdir(parents=True, exist_ok=True)
    lock = Path(values['GPU_LOCK_FILE'])
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.touch(exist_ok=True)
    for path in (state / 'secrets/ui_password', worker_secret):
        if path.exists() and not path.read_text().strip():
            raise ValueError(f'Empty authentication file: {path}; restore it before starting')
        if not path.exists():
            with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'w') as f:
                f.write(secrets.token_hex(24) + '\n')
    registry = {'workers': [dict(id='gpu_4090_a', label=values['PC_LABEL'], enabled=True,
                                dag_id='kaist_direct_pipeline_4090', pool='gpu_4090_a',
                                env_prefix='KAIST_PC1', url=worker_url, review_dag='kaist_revising_gpu_4090_a')]}
    (state / 'config/workers.json').write_text(json.dumps(registry, ensure_ascii=False, indent=2) + '\n')
    # Quote values so Compose does not interpolate secrets or paths a second time.
    env_file.parent.mkdir(parents=True, exist_ok=True)
    temp = env_file.with_name(env_file.name + '.tmp')
    with os.fdopen(os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), 'w') as f:
        f.write(''.join(f"{k}='{v}'\n" for k, v in values.items()))
    temp.chmod(0o600)
    temp.replace(env_file)
    compose = ['docker', 'compose', '--env-file', str(env_file)]
    if args.configure_only:
        print(f'Configuration ready: {env_file}')
        return
    run(compose + ['build'], env=env)
    # Exercise GPU packages, model assets, rendering and NAS before bringing the UI online.
    run(compose + ['run', '--rm', '--no-deps', 'gpu-worker', 'check'], env=env)
    run(compose + ['up', '-d', '--wait', '--wait-timeout', '600'], env=env)
    run([sys.executable, str(ROOT / 'repro/check_ui.py'), '--env-file', str(env_file)], env=env)
    run(compose + ['ps'], env=env)
    print(f'UI ready: http://{ip}:{ports["ui"]}/')
    print(f'Login: operator; password file: {state / "secrets/ui_password"}')
    print(f'Airflow admin password file: {state / "state/simple_auth_manager_passwords.json.generated"}')


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, subprocess.CalledProcessError) as exc:
        print(f'Start failed: {exc}', file=sys.stderr)
        sys.exit(1)
