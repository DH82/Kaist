#!/usr/bin/env python3
"""컨테이너 안에서 파이프라인 단계별 환경을 점검합니다. 모델 추론은 하지 않습니다.

각 단계 스크립트가 실제로 쓰는 검사(tracking_worker / reconstruction_worker / hand_stage_worker check)를
그대로 호출하고, SAM3 환경·EGL 렌더링·xvfb·ffmpeg·NAS 쓰기를 추가로 확인합니다.
"""
import json, os, subprocess, sys, tempfile, time
from pathlib import Path

RUNTIME_ONLY = '--runtime-only' in sys.argv
W = Path(os.environ['KAIST_WORKER_ROOT'])
cfg = json.loads((W / 'config/gpu_4090_a.json').read_text())
scripts = Path(cfg['scripts_dir'])
sys.path.insert(0, str(scripts))
import pipeline  # noqa: E402  (00_scripts/pipeline.py: config.env → 환경변수)

root, phystwin, env = pipeline.runtime()
env.update(PYOPENGL_PLATFORM='egl', HF_HUB_OFFLINE='1', CUDA_VISIBLE_DEVICES=cfg['gpu_uuid'])
env.pop('DISPLAY', None)
py = {k: str(root / f'{k}-env/bin/python') for k in ('sam', 'phystwin', 'hamba')}
results = []


def check(name, cmd, cwd=None, extra_env=None):
    t = time.monotonic()
    e = {**env, **(extra_env or {})}
    r = subprocess.run(cmd, cwd=cwd or scripts, env=e, capture_output=True, text=True)
    ok = r.returncode == 0
    out = (r.stdout.strip().splitlines() or [''])[-1] if ok else (r.stderr or r.stdout).strip()[-800:]
    results.append(ok)
    print(f"{'✔' if ok else '✘'} {name} ({time.monotonic()-t:.0f}s) {out}", flush=True)


def code(env_name, src):
    return [py[env_name], '-c', src]


check('GPU (nvidia-smi)', ['nvidia-smi', '-i', cfg['gpu_uuid'], '--query-gpu=name,driver_version,memory.total', '--format=csv,noheader'])
check('ffmpeg', ['bash', '-c', 'ffmpeg -version | head -1'])
check('xvfb-run', ['xvfb-run', '-a', 'true'])
check('SAM3 환경 (sam-env)', code('sam', "import torch,sam3;x=torch.ones(1024,1024,device='cuda');print('torch',torch.__version__,torch.cuda.get_device_name(0),float((x@x).sum()))"))
check('3D CUDA 확장', code('phystwin', "import torch;from pytorch3d import _C;import diffoctreerast._C,diff_gaussian_rasterization._C;import nvdiffrast.torch as dr;c=dr.RasterizeCudaContext();p=torch.tensor([[[-0.5,-0.5,0,1],[0.5,-0.5,0,1],[0,0.5,0,1]]],dtype=torch.float32,device='cuda');t=torch.tensor([[0,1,2]],dtype=torch.int32,device='cuda');r,_=dr.rasterize(c,p,t,(16,16));assert (r[...,3]>0).any();print('CUDA rasterization OK')"))
check('HaMBA CUDA 확장', code('hamba', "import torch;from pytorch3d import _C;import selective_scan_cuda_oflex;print('PyTorch3D / selective_scan imports OK')"))
if not RUNTIME_ONLY:
    check('SAM3.1 모델 캐시', code('sam', "from huggingface_hub import snapshot_download as s;print(s('facebook/sam3.1',local_files_only=True))"))
    check('추적 (tracking_worker check)', [py['phystwin'], str(scripts / 'tracking_worker.py'), 'check', '--repo', env.get('KP_COTRACKER_REPO', '')], cwd=phystwin)
    check('3D 복원 (reconstruction_worker check)', [py['phystwin'], str(scripts / 'reconstruction_worker.py'), 'check'], cwd=phystwin)
    mono = phystwin / 'Mono'
    hamba_env = {'PYTHONPATH': f"{mono/'data_process'}:{mono/'data_process/Hamba'}:{env.get('PYTHONPATH','')}"}
    check('HaMBA·MANO (hand_stage_worker check)', [py['hamba'], str(scripts / 'hand_stage_worker.py'), 'check', '--mono', str(mono)], cwd=mono, extra_env=hamba_env)
else:
    for name in ('phystwin', 'hamba'):
        check(name+' Python·CUDA', code(name, "import torch;print(torch.__version__,torch.ones(1,device='cuda').item())"))
    print('모델 검사 생략 (--runtime-only): 추론 가능한 상태라는 의미는 아닙니다.')
check('EGL 오프스크린 렌더 (pyrender)', code('hamba', "import pyrender,numpy as n;r=pyrender.OffscreenRenderer(64,64);s=pyrender.Scene();s.add(pyrender.PerspectiveCamera(yfov=1.0));c,d=r.render(s);print('EGL ok',c.shape)"))
try:
    nas = Path(cfg['nas_mount']) / cfg['nas_results_subdir']
    with tempfile.NamedTemporaryFile(dir=nas, prefix='.selfcheck-') as f:
        f.write(b'ok'); f.flush()
    proj = Path(cfg['project_dir'])
    with tempfile.NamedTemporaryFile(dir=proj, prefix='.selfcheck-'):
        pass
    print(f'✔ 저장소 쓰기: NAS {nas} · 작업 폴더 {proj} · RAW {len(list(Path(cfg["raw_root"]).glob("ep_*")))}개'); results.append(True)
except OSError as e:
    print('✘ 저장소 쓰기:', e); results.append(False)

print(f"\n{sum(results)}/{len(results)} 통과")
sys.exit(0 if all(results) else 1)
