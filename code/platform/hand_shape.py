"""MANO beta: 사람별 손 형상(MANO 베타) 측정 도구와 프로필 지정 (작업 현황 'MANO beta' 칸, RAW 목록 '손 형상').

흐름 (손만 찍은 오른손 영상, rgbd_episode.json 라벨로 고름)
  1) SAM3 손 분할: NAS → 로컬 복사 → QC·전처리 → SAM3 손 분할(팔뚝·그림자 제외) → 빠진 마스크를 빈 마스크로 채움
  2) 마스크 편집: 브러시·지우개·마법봉(워커와 같은 sam_wand_server.py), 처음 저장할 때 원래 마스크는 mask_original/에 보관
  3) β 측정: HaMBA → 운영 MANO 피팅과 같은 공유 형상 보정(--shape_only) → hand_v1/shared_shape.json
  4) 프로필 만들기: 측정한 에피소드들의 β 평균 → NAS control/hand_profiles/<ID>.json (형식 kaist-mano-shape-profile/v1, 덮어쓰기 없음)
RAW 목록에서 에피소드마다 프로필을 지정하면 NAS control/hand_profiles/assign/<RAW>.json 에 남고,
Worker(kaist_runtime)의 MANO 단계가 그 프로필로 β를 고정합니다(18_fit_hands.py --hand-profile).

작업 폴더(KAIST_HAND_SHAPE_DIR, 기본 ~/Desktop/test_shape)는 운영 프로젝트·입고 기록·Airflow와 분리돼 있습니다.
GPU 작업은 이 UI 프로세스가 직접 실행합니다(Native PC 전용: PC1). Worker와 같은 GPU 잠금을 기다렸다가 씁니다
(SAM3 = SAM3 자리 + GPU 슬롯 1개, HaMBA·β 측정 = GPU 슬롯 1개). UI를 재시작하면 진행 중이던 단계는 끊기고, 다시 실행하면 이어서 합니다.
"""
import base64, datetime, hashlib, json, os, re, shlex, shutil, struct, subprocess, sys, threading, time, traceback, zlib
from pathlib import Path

from fastapi import HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, Response
from pydantic import BaseModel, Field

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
SCRIPTS = REPO / 'pipeline/current/project/00_scripts'          # 01_copy_raw.py · pipeline.py · hand_profile.py
WORKER_HOME = Path(os.environ.get('KAIST_HAND_SHAPE_WORKER', str(Path.home() / 'Desktop/worker1/airflow')))
WORKER = WORKER_HOME / 'worker'                                  # gpu_slots.py · sam_wand_server.py
LOCK = WORKER / 'gpu_4090_a.lock'
BASE = Path(os.environ.get('KAIST_HAND_SHAPE_DIR', str(Path.home() / 'Desktop/test_shape')))
RAWCOPY = Path(os.environ.get('KAIST_HAND_SHAPE_RAWCOPY', str(Path.home() / 'Desktop/test_raw')))
NAS = Path(os.environ.get('NAS_MOUNT', '/mnt/kaist-data'))
NAS_ROOTS = [NAS / 'TEST', NAS / 'rgbd_raw']                     # 같은 에피소드가 둘 다 있으면 앞쪽 우선
PROFILES = Path(os.environ.get('KAIST_HAND_PROFILES', str(NAS / 'control/hand_profiles')))
ASSIGN = PROFILES / 'assign'
# 두 방식 MANO 비교(mano_compare.py): 결과는 NAS, 진행 상태는 로컬 작업 폴더
COMPARE_OUT = Path(os.environ.get('KAIST_MANO_COMPARE_OUT', str(NAS / 'TEST/Set2')))
COMPARE_WORK = Path(os.environ.get('KAIST_MANO_COMPARE_WORK', str(Path.home() / 'kaist_work/mano_compare')))
VARIANT = re.compile(r'(default|[A-Za-z0-9][A-Za-z0-9_.+-]{0,80})$')
SWEEP = re.compile(r'pt[0-9.e+]+_hw[0-9.e+-]+_pr[0-9.e+]+_(prev|hamba)(_crop)?(_s[0-9]+)?(_def)?$')


def variant_dir(ep, v):
    """비교 화면의 조건 폴더: Set2/<ep>/<default|프로필> 또는 손가락 시험 ~/kaist_work/mano_compare/<ep>/sweep/<조건>."""
    if not EP.fullmatch(ep) or not VARIANT.fullmatch(v) or v in ('.', '..'): raise HTTPException(400, '잘못된 요청입니다.')
    d = COMPARE_WORK / ep / 'sweep' / v if SWEEP.fullmatch(v) else COMPARE_OUT / ep / v
    if not (d / 'hand_v1/hand_reg.pkl').is_file(): raise HTTPException(404, f'{v} 결과가 없습니다.')
    return d


def variant_label(v, settings=None):
    if v == 'default': return '현재 운영 (기본 β 보정 · 손가락 500,000)'
    m = SWEEP.fullmatch(v)
    if not m: return f'{v} β · 현재 설정 (기준)'
    pt, hw, pr = re.match(r'pt([^_]+)_hw([^_]+)_pr([^_]+)_', v).groups()
    return f"손가락 시간축 {float(pt):g} · HaMBA {float(hw):g} · 자세 사전 {float(pr):g} · {'프레임별 HaMBA 시작' if m.group(1) == 'hamba' else '이전 프레임 시작'}" + (' · MediaPipe 잘라서 재검출' if m.group(2) else '') + (f' · 관절 평활 ±{m.group(3)[2:]}' if m.group(3) else '') + (' · 기본 β 보정' if m.group(4) else ' · User2 β')
SUBTRACT = ['human forearm', 'shadow']
WAND_IDLE = 180
WAND_MIN_FREE_MB = 8192
EP = re.compile(r'ep_[A-Za-z0-9_]+$')
PID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$')
# 운영 18_fit_hands.py(mano-geometric-shared-shape-v7)의 피팅 설정 + --shape_only (공유 형상 보정까지만)
SHAPE_ARGS = ['--temp_weight', '500000', '--hamba_weight', '0.01', '--log', 'hand_v1', '--seed', '1027', '--num_iters', '1500',
              '--num_hands', '1', '--num_cams', '4', '--resume', '--w_mask', '1.0', '--w_mask_cover', '1.0', '--cover_max_px', '40.0',
              '--wrist_weight', '0.1', '--w_silhouette', '0.0', '--silhouette_size', '64', '--silhouette_every', '10',
              '--pose_temp_weight', '500', '--w_pose_prior', '25.0', '--mp_crop', '--mp_smooth', '4', '--camera_init_iters', '300', '--fit_shape_lr', '0.002',
              '--fit_shape_max_delta', '0.0', '--fit_shape_temporal_weight', '1.0', '--robust_joint_delta', '5.0', '--staged_fit',
              '--shared_shape', '--shape_frames', '', '--shape_frame_count', '12', '--shape_iters', '3000', '--shape_lr', '0.001',
              '--shape_prior_weight', '1.0', '--shape_max_delta', '1.0', '--shape_only']


class UserError(Exception):
    pass


def workspace():
    """Worker 설정의 파이프라인 사본(workspace). 없으면 None (이 PC에서는 측정 도구를 쓸 수 없음)."""
    try:
        cfg = json.loads((WORKER_HOME / 'config/gpu_4090_a.json').read_text())
        w = Path(cfg['pipeline_root'])
        return w if (w / 'kaist-full/PhysTwin/Mono').is_dir() else None
    except (OSError, ValueError, KeyError):
        return None


def ui_enabled():
    """사람별 손 형상 화면(MANO beta 칸·RAW '손 형상' 칸·비교 화면) 표시 여부. 기본 꺼짐, KAIST_HAND_PROFILE_UI=1 일 때만 켬."""
    return os.environ.get('KAIST_HAND_PROFILE_UI', '').strip().lower() in ('1', 'true', 'yes', 'on')


def strip_ui(page):
    """index.html에서 MANO beta 칸과 RAW '손 형상' 열 머리를 뺍니다(outputs.js는 #manoBeta가 없으면 열을 그리지 않음)."""
    page = re.sub(r'<details class="card" id="manoBeta">.*?</details>', '', page, count=1, flags=re.S)
    return re.sub(r'<th title="MANO 피팅에 쓸 사람별 손 형상[^"]*">손 형상</th>', '', page, count=1)


def tool_available():
    return workspace() is not None and (WORKER / 'gpu_slots.py').exists() and SCRIPTS.is_dir()


def _import(name, folder):
    if str(folder) not in sys.path: sys.path.insert(0, str(folder))
    return __import__(name)


def hand_profile():
    return _import('hand_profile', SCRIPTS)


def gpu_slots():
    return _import('gpu_slots', WORKER)


def now(): return time.strftime('%H:%M:%S')


def mono():
    return workspace() / 'kaist-full/PhysTwin/Mono'


def mano_dir():
    return mono() / 'data_process/Hamba/_DATA/data/mano'


def sha256(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda: f.read(1 << 20), b''): h.update(block)
    return h.hexdigest()


# ---------- 상태 ----------
LOGS = BASE / 'ui_logs'
state_lock = threading.Lock()
queue, running = [], {}          # queue: [(ep, step)], running: ep -> 현재 단계 문구
catalog = {'at': 0, 'label': None, 'rows': [], 'error': None}
_worker_started = False


def log(ep, msg):
    LOGS.mkdir(parents=True, exist_ok=True)
    with (LOGS / f'{ep}.log').open('a') as f: f.write(f'[{now()}] {msg}\n')


def case_dir(ep): return BASE / 'hand' / ep
def state_path(ep): return case_dir(ep) / 'sam_ui_state.json'


def read_state(ep):
    try: return json.loads(state_path(ep).read_text())
    except (OSError, ValueError): return {}


def write_state(ep, **kv):
    p = state_path(ep); p.parent.mkdir(parents=True, exist_ok=True)
    d = read_state(ep); d.update(kv); tmp = p.with_suffix('.tmp'); tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1)); tmp.replace(p)


def pipeline_env():
    env = os.environ.copy()
    for line in (workspace() / 'kaist-full/config.env').read_text().splitlines():
        if line and not line.startswith('#') and '=' in line:
            k, v = line.split('=', 1); env[k] = ' '.join(shlex.split(v))
    env.update(PYTHONUNBUFFERED='1', PYTORCH_ALLOC_CONF='expandable_segments:True', HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1')
    return env


def hands_env():
    env = pipeline_env(); m = mono()
    env.update(PYTHONPATH=f'{m}/data_process:{m}/data_process/Hamba', PYOPENGL_PLATFORM='egl')
    env.pop('DISPLAY', None); env.pop('XAUTHORITY', None)
    return env


def scan(label):
    rows, seen = [], set()
    dirs = [d for root in NAS_ROOTS if root.is_dir() for d in root.iterdir()]
    for d in sorted(dirs, key=lambda d: (d.name, NAS_ROOTS.index(d.parent))):
        if not EP.fullmatch(d.name) or not d.is_dir() or d.name in seen: continue
        try: m = json.loads((d / 'rgbd_episode.json').read_text())
        except (OSError, ValueError): continue
        names = {str(m.get('task', {}).get('taskName') or '').lower(), str(m.get('object', {}).get('label') or '').lower()}
        if label.lower() not in names: continue
        cap = m.get('capture', {})
        seen.add(d.name)
        rows.append({'episode': d.name, 'source': str(d), 'folder': d.parent.name, 'started': (cap.get('startedAt') or '')[:19].replace('T', ' '),
                     'frames': cap.get('frameCounts', {})})
    return rows


def refresh(label):
    try:
        rows = scan(label)
        with state_lock: catalog.update(at=time.time(), label=label, rows=rows, error=None)
    except OSError as e:
        with state_lock: catalog.update(at=time.time(), label=label, error=f'NAS 목록을 읽지 못했습니다: {e}')


def case_meta(ep):
    meta = json.loads((case_dir(ep) / 'metadata.json').read_text())
    return int(meta['frame_num']), [int(v) for v in meta['WH']]


def masks_ready(ep): return (case_dir(ep) / 'mask' / 'mask_info_3.json').exists()


def mask_summary(ep):
    case = case_dir(ep)
    try: n, wh = case_meta(ep)
    except (OSError, ValueError, KeyError): return None
    have = [len(list((case / 'mask' / str(c) / '1').glob('*.png'))) for c in range(4)]
    edited = len(list((case / 'mask_original').glob('*/1/*.png'))) if (case / 'mask_original').exists() else 0
    return {'frames': n, 'wh': wh, 'per_camera': have, 'edited': edited}


def newest_mask(ep):
    return max((p.stat().st_mtime for p in (case_dir(ep) / 'mask').glob('*/1/*.png')), default=0)


def measurement(ep):
    """β 측정 결과 요약 또는 None. 측정 뒤 마스크를 고쳤으면 stale=True."""
    p = case_dir(ep) / 'hand_v1' / 'shared_shape.json'
    try: d = json.loads(p.read_text())['diagnostics']['right']
    except (OSError, ValueError, KeyError): return None
    b = d['betas'][0] if isinstance(d['betas'][0], list) else d['betas']
    st = read_state(ep)
    return {'betas': [round(float(v), 4) for v in b], 'frames': len(d.get('frames') or []), 'path': str(p),
            'stale': newest_mask(ep) > p.stat().st_mtime, 'size': st.get('hand_size'),
            'at': time.strftime('%m-%d %H:%M', time.localtime(p.stat().st_mtime))}


def status(ep):
    st = read_state(ep)
    with state_lock:
        if ep in running: return 'running', running[ep]
        for i, (e, s) in enumerate(queue):
            if e == ep: return 'queued', f'대기 {i + 1}번째 · ' + ('SAM3 손 분할' if s == 'sam' else 'β 측정')
    if st.get('error'): return 'failed', st['error']
    m = measurement(ep)
    if m: return ('stale', '마스크를 고친 뒤 β를 다시 측정해야 합니다') if m['stale'] else ('measured', f'β 측정 완료 · 보정 프레임 {m["frames"]}개')
    if st.get('sam_done') and masks_ready(ep): return 'masked', 'SAM3 완료 · 마스크 확인 후 β 측정'
    if masks_ready(ep): return 'partial', 'SAM3 마스크 있음 · 빈 프레임 채우기 전 (SAM3 다시 실행하면 마무리)'
    if (BASE / '03_prepared' / ep / 'preparation.json').exists(): return 'prepared', '전처리 완료 · SAM3 전'
    if (BASE / '01_raw' / ep).exists(): return 'copied', 'RAW 복사됨'
    return 'new', '미처리'


# ---------- 실행 ----------
def run(ep, cmd, cwd=None, env=None):
    log(ep, '$ ' + ' '.join(map(str, cmd)))
    with (LOGS / f'{ep}.log').open('a') as f:
        r = subprocess.run([str(c) for c in cmd], cwd=cwd, env=env, stdout=f, stderr=subprocess.STDOUT)
    if r.returncode:
        name = next((Path(str(c)).name for c in cmd if str(c).endswith('.py')), Path(str(cmd[0])).name)
        raise RuntimeError(f'종료코드 {r.returncode}: {name} (로그 확인)')


def step(ep, text):
    with state_lock: running[ep] = text
    log(ep, '== ' + text)


def gpu_slot(ep, text):
    step(ep, text + ' · GPU 순서 대기 (운영 작업과 같은 GPU 슬롯)')
    slot, _ = gpu_slots().acquire(str(LOCK))
    step(ep, text)
    return slot


def process_sam(ep):
    W = workspace()
    write_state(ep, error=None, started=now())
    step(ep, '1/5 NAS → 로컬 복사')
    RAWCOPY.mkdir(parents=True, exist_ok=True)
    src = next((r['source'] for r in catalog['rows'] if r['episode'] == ep), None)
    if not src: raise RuntimeError('NAS에서 에피소드 위치를 찾지 못했습니다. 목록을 새로고침하세요.')
    log(ep, f'NAS 위치: {src}')
    run(ep, ['rsync', '-a', src, str(RAWCOPY) + '/'])
    if not (BASE / '01_raw' / ep).exists():
        run(ep, ['/usr/bin/python3', SCRIPTS / '01_copy_raw.py', '--project', BASE, RAWCOPY / ep], cwd=SCRIPTS)
    step(ep, '2/5 QC · 전처리')
    if not (BASE / '03_prepared' / ep / 'preparation.json').exists():
        run(ep, ['/usr/bin/python3', '-c', f"from pathlib import Path;import pipeline as pl;p=Path({str(BASE)!r});pl.init(p);pl.run_qc(p,{ep!r},open_setup=False)"], cwd=SCRIPTS)
    case = case_dir(ep); case.mkdir(parents=True, exist_ok=True)
    for n in ('color', 'depth', 'metadata.json', 'calibrate.pkl', 'source_frames.json'):
        link = case / n
        if link.is_symlink(): link.unlink()
        elif link.exists(): continue
        link.symlink_to(BASE / '03_prepared' / ep / n)
    if not masks_ready(ep):
        step(ep, '3/5 GPU 순서 대기 (운영 SAM3·GPU 작업 우선)')
        sam = gpu_slots().acquire_sam(str(LOCK))
        slot, _ = gpu_slots().acquire(str(LOCK))
        try:
            if not masks_ready(ep):
                step(ep, '4/5 SAM3 손 분할 (팔뚝·그림자 제외)')
                if (case / 'mask').exists(): shutil.move(str(case / 'mask'), str(case / f'mask_incomplete_{time.strftime("%H%M%S")}'))
                run(ep, [W / 'sam-env/bin/python', 'data_process/segment_object_sam3.py', '--base_path', BASE / 'hand', '--case_name', ep,
                         '--groups', 'controller', '--scene_type', '2d', '--version', 'sam3.1', '--grounding_batch_size', '4',
                         '--cameras', '0', '1', '2', '3', '--controller_subtract_prompts', *SUBTRACT],
                    cwd=W / 'kaist-full/PhysTwin', env=pipeline_env())
        finally:
            slot.close()
            if sam: sam.close()
    step(ep, '5/5 빠진 마스크 프레임 확인')
    n, (w, h) = case_meta(ep); filled = {}
    for cam in range(4):
        d = case / 'mask' / str(cam) / '1'; d.mkdir(parents=True, exist_ok=True); filled[str(cam)] = []
        for f in range(n):
            if not (d / f'{f}.png').exists():
                write_png(d / f'{f}.png', w, h, bytes(w * h)); filled[str(cam)].append(f)
    total = sum(len(v) for v in filled.values())
    log(ep, f'빈 마스크로 채운 프레임: {filled} (합계 {total}) - SAM3가 면적 급증(팔뚝 섞임)으로 지운 프레임')
    write_state(ep, sam_done=True, filled=filled, finished=now(), subtract=SUBTRACT, error=None)


def runtime_mono():
    """data_process_hands.py를 Worker 사본 그대로 쓰되, 실행 폴더(디버그 출력)는 작업 폴더 안에 둡니다."""
    r = BASE / '_runtime/Mono'; m = mono()
    (r / 'data_process').mkdir(parents=True, exist_ok=True)
    for p in m.iterdir():
        if p.name != 'data_process' and not (r / p.name).exists(): (r / p.name).symlink_to(p)
    for p in (m / 'data_process').iterdir():
        if not (r / 'data_process' / p.name).exists(): (r / 'data_process' / p.name).symlink_to(p)
    return r


SIZE_CODE = r'''
import json, sys
import numpy as np, torch
sys.path.insert(0, 'data_process')
from models.mano_wrapper import MANO
mano = MANO(data_dir='data_process/Hamba/_DATA/data/', model_path='data_process/Hamba/_DATA/data/mano', gender='neutral', num_hand_joints=15,
            mean_params='data_process/Hamba/_DATA/data/mano_mean_params.npz', create_body_pose=False)
b = torch.as_tensor(np.asarray(json.loads(sys.argv[1]), np.float32).reshape(-1, 10)); n = len(b); eye = torch.eye(3)
j = mano(global_orient=eye[None, None].expand(n, 1, 3, 3), hand_pose=eye[None, None].expand(n, 15, 3, 3), betas=b, pose2rot=False).joints.detach().numpy() * 1000
print(json.dumps([{'length': round(float(np.linalg.norm(x[12] - x[0])), 1), 'width': round(float(np.linalg.norm(x[17] - x[5])), 1)} for x in j]))
'''


def hand_size(betas_list):
    """기본 자세 손 길이(손목→중지 끝)·폭(검지 뿌리↔소지 뿌리) mm. 실패하면 None (표시용)."""
    try:
        out = subprocess.run([str(workspace() / 'hamba-env/bin/python'), '-c', SIZE_CODE, json.dumps(betas_list)], cwd=mono(), env=hands_env(),
                             capture_output=True, text=True, timeout=180)
        return json.loads(out.stdout.strip().splitlines()[-1]) if out.returncode == 0 else None
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        return None


def hamba_done(ep): return all((BASE / 'hamba' / ep / str(c) / 'results.pt').is_file() for c in range(4))


def process_measure(ep):
    W = workspace(); case = case_dir(ep)
    if not (read_state(ep).get('sam_done') and masks_ready(ep)): raise RuntimeError('SAM3 손 분할을 먼저 끝내세요.')
    write_state(ep, error=None, started=now())
    if not hamba_done(ep):
        # HaMBA는 --img_folder 아래의 모든 에피소드를 처리하므로, 이 에피소드만 든 폴더를 따로 만듭니다.
        scope = BASE / '_hamba_in' / ep; scope.mkdir(parents=True, exist_ok=True)
        if not (scope / ep).exists(): (scope / ep).symlink_to(case)
        slot = gpu_slot(ep, '1/3 HaMBA 손 추정')
        try:
            run(ep, [W / 'hamba-env/bin/python', '-u', 'script_hand_recon.py', '--img_folder', scope, '--out_folder', BASE / 'hamba'],
                cwd=mono() / 'data_process/Hamba', env=hands_env())
        finally: slot.close()
        if not hamba_done(ep): raise RuntimeError('HaMBA 결과(카메라 4대 results.pt)가 없습니다.')
    # 다시 측정: 이전 결과(형상·관측 캐시)는 보관하고 새로 보정합니다(관측 캐시에 이전 마스크가 들어 있음).
    stamp = time.strftime('%Y%m%d_%H%M%S')
    if (case / 'hand_v1').exists(): shutil.move(str(case / 'hand_v1'), str(case / f'hand_v1_{stamp}'))
    if (case / 'observations.pkl').exists(): shutil.move(str(case / 'observations.pkl'), str(case / f'observations_{stamp}.pkl'))
    r = runtime_mono(); code = mono() / 'data_process/data_process_hands.py'
    slot = gpu_slot(ep, '2/3 β 측정 (공유 형상 보정 · 운영 MANO 설정 + --shape_only)')
    try:
        run(ep, [W / 'hamba-env/bin/python', '-u', r / 'data_process/data_process_hands.py', '--hamba_base_path', BASE / 'hamba',
                 '--phystwin_base_path', BASE / 'hand', '--case_name', ep, '--observations-cache', case / 'observations.pkl', *SHAPE_ARGS],
            cwd=r, env=hands_env())
    except RuntimeError:
        tail = (LOGS / f'{ep}.log').read_text(errors='replace')[-6000:]
        if 'ref_cidx' in tail and 'KeyError' in tail:
            raise RuntimeError('첫 프레임에서 어느 카메라도 오른손을 찾지 못했습니다(HaMBA 손 검출 없음). 첫 프레임부터 오른손 전체가 보이게 다시 찍은 영상을 쓰세요.') from None
        raise
    finally: slot.close()
    m = measurement(ep)
    if not m: raise RuntimeError('형상 보정 결과(hand_v1/shared_shape.json)가 없습니다. 로그를 확인하세요.')
    step(ep, '3/3 손 크기 계산')
    size = hand_size([m['betas']])
    write_state(ep, measured=now(), hand_size=size[0] if size else None, error=None,
                measure_code={'data_process_hands.py': sha256(code), 'args': SHAPE_ARGS})
    log(ep, f'β 측정 완료: {m["betas"]} · 보정 프레임 {m["frames"]}개' + (f' · 손 길이 {size[0]["length"]}mm 폭 {size[0]["width"]}mm' if size else ''))


def worker():
    while True:
        with state_lock: job = queue.pop(0) if queue else None
        if not job: time.sleep(1); continue
        ep, kind = job
        try:
            with state_lock: running[ep] = '시작'
            (process_sam if kind == 'sam' else process_measure)(ep); log(ep, '완료')
        except Exception as e:
            log(ep, traceback.format_exc()); write_state(ep, error=f'{e}'.strip()[:300])
        finally:
            with state_lock: running.pop(ep, None)


def start_worker():
    global _worker_started
    with state_lock:
        if _worker_started: return
        _worker_started = True
    threading.Thread(target=worker, name='kaist-hand-shape', daemon=True).start()


# ---------- 마스크 파일 ----------
def write_png(path, w, h, ones):
    """0/1(또는 0/255) 바이트 w*h개를 0/255 회색조 PNG로 원자적으로 씁니다."""
    px = bytes(ones).translate(bytes([0] + [255] * 255))
    raw = b''.join(b'\x00' + px[y * w:(y + 1) * w] for y in range(h))
    def chunk(t, d): return struct.pack('>I', len(d)) + t + d + struct.pack('>I', zlib.crc32(t + d) & 0xffffffff)
    data = b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', w, h, 8, 0, 0, 0, 0)) + chunk(b'IDAT', zlib.compress(raw, 6)) + chunk(b'IEND', b'')
    tmp = Path(str(path) + '.tmp'); tmp.write_bytes(data); tmp.replace(path)


def frame_paths(ep, cam, f):
    case = case_dir(ep)
    return case / 'color' / str(cam) / f'{f}.png', case / 'mask' / str(cam) / '1' / f'{f}.png', case / 'mask_original' / str(cam) / '1' / f'{f}.png'


def check_edit(ep, cam, f):
    if not EP.fullmatch(ep) or cam not in range(4): raise UserError('잘못된 요청입니다.')
    st, _ = status(ep)
    if st in ('running', 'queued'): raise UserError('실행 중이거나 대기 중인 에피소드는 편집할 수 없습니다.')
    n, wh = case_meta(ep)
    if not 0 <= f < n: raise UserError('프레임 번호가 범위를 벗어났습니다.')
    return n, wh


edit_lock = threading.Lock()


def save_mask(ep, cam, f, data, who):
    _, (w, h) = check_edit(ep, cam, f)
    ones = base64.b64decode(data)
    if len(ones) != w * h: raise UserError(f'마스크 크기가 맞지 않습니다 ({len(ones)} != {w}x{h}).')
    _, mask, orig = frame_paths(ep, cam, f)
    with edit_lock:
        if mask.exists() and not orig.exists():
            orig.parent.mkdir(parents=True, exist_ok=True); shutil.copy2(mask, orig)
        mask.parent.mkdir(parents=True, exist_ok=True)
        write_png(mask, w, h, ones)
    area = sum(1 for b in ones if b)
    log(ep, f'마스크 편집 저장({who}): cam{cam + 1} 프레임 {f} (손 픽셀 {area})')
    return {'ok': True, 'area': area}


def revert_mask(ep, cam, f, who):
    check_edit(ep, cam, f)
    _, mask, orig = frame_paths(ep, cam, f)
    if not orig.exists(): raise UserError('이 프레임은 편집한 적이 없습니다.')
    with edit_lock:
        shutil.copy2(orig, mask); orig.unlink()
    log(ep, f'마스크 원래대로({who}): cam{cam + 1} 프레임 {f}')
    return {'ok': True}


# ---------- 마법봉 ----------
def gpu_free_mb():
    try: out = subprocess.check_output(['nvidia-smi', '--query-gpu=memory.free', '--format=csv,noheader,nounits'], text=True, timeout=10)
    except (OSError, subprocess.SubprocessError): return None
    vals = [int(v) for v in out.split() if v.strip().isdigit()]
    return vals[0] if vals else None


class Wand:
    def __init__(self):
        self.lock = threading.Lock(); self.proc = None; self.gpu = None; self.last = 0.0; self.reaper = False

    def _stop(self):
        if self.proc:
            try: self.proc.stdin.close(); self.proc.wait(timeout=15)
            except Exception: self.proc.kill()
        self.proc = None
        if self.gpu: self.gpu.close(); self.gpu = None

    def _start(self):
        if not self.reaper:
            self.reaper = True; threading.Thread(target=self._reap, name='kaist-hand-shape-wand', daemon=True).start()
        shared, unit = gpu_slots().acquire_wand(str(LOCK))  # RTX 4090은 자리 제한 없음(shared=False)
        if shared and not unit: raise UserError('SAM3 작업 2개가 실행 중이라 지금은 마법봉을 쓸 수 없습니다. 잠시 뒤 다시 시도하세요.')
        self.gpu = unit
        free = gpu_free_mb()
        if free is not None and free < WAND_MIN_FREE_MB:
            self._stop()
            raise UserError(f'GPU 메모리가 부족해 마법봉을 쓸 수 없습니다 (남은 {free / 1024:.1f}GB, 필요 {WAND_MIN_FREE_MB / 1024:.0f}GB). 브러시를 쓰거나 잠시 뒤 다시 시도하세요.')
        env = pipeline_env(); LOGS.mkdir(parents=True, exist_ok=True)
        logf = (LOGS / 'sam_wand.log').open('ab')
        self.proc = subprocess.Popen([env['KP_PY_SAM3'], str(WORKER / 'sam_wand_server.py')], cwd=str(workspace() / 'kaist-full/PhysTwin'), env=env,
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=logf, text=True, bufsize=1)
        ready = self.proc.stdout.readline()
        if not ready.strip().startswith('{"ready"'):
            self._stop(); raise UserError('마법봉(SAM3.1)을 시작하지 못했습니다. 작업 폴더 ui_logs/sam_wand.log를 확인하세요.')

    def call(self, request):
        with self.lock:
            if self.proc and self.proc.poll() is not None: self._stop()
            if not self.proc: self._start()
            self.last = time.monotonic()
            try:
                self.proc.stdin.write(json.dumps(request) + '\n'); self.proc.stdin.flush()
                line = self.proc.stdout.readline()
            except (BrokenPipeError, OSError): line = ''
            self.last = time.monotonic()
            if not line: self._stop(); raise UserError('마법봉이 종료됐습니다. 다시 시도하세요.')
            result = json.loads(line)
            if not result.get('ok'): raise UserError(result.get('error', '분할하지 못했습니다.'))
            return result

    def status(self): return {'loaded': bool(self.proc and self.proc.poll() is None), 'idle_seconds': WAND_IDLE}

    def _reap(self):
        while True:
            time.sleep(10)
            with self.lock:
                if self.proc and time.monotonic() - self.last > WAND_IDLE: self._stop()


wand = Wand()
_mesh_lock = threading.Lock()
MESH_CODE = r'''
import json, sys, pickle, io
import numpy as np, torch
sys.path.insert(0, 'data_process')
from models.mano_wrapper import MANO
class U(pickle.Unpickler):
    def find_class(self, m, n):
        if m == 'torch.storage' and n == '_load_from_bytes': return lambda b: torch.load(io.BytesIO(b), map_location='cpu', weights_only=False)
        return super().find_class(m, n)
a = json.loads(sys.argv[1])
regs = []
for p in a['regs']:
    with open(p, 'rb') as f: regs.append(U(f).load())
frames = sorted(set.intersection(*[set(r) for r in regs]))
V = np.stack([np.stack([np.asarray(r[f]['right'], np.float32) for f in frames]) for r in regs])   # [방식, 프레임, 778, 3] (m)
faces = np.asarray(MANO(data_dir='data_process/Hamba/_DATA/data/', model_path='data_process/Hamba/_DATA/data/mano', gender='neutral', num_hand_joints=15,
                        mean_params='data_process/Hamba/_DATA/data/mano_mean_params.npz', create_body_pose=False).faces, np.int32)
dist = np.linalg.norm(V[0] - V[1], axis=2) * 1000                                                # [프레임, 778] mm
center = V.reshape(-1, 3).mean(0)
out = a['out']
(V - center).astype('<f4').tofile(out + '/mesh.bin.tmp')
import os; os.replace(out + '/mesh.bin.tmp', out + '/mesh.bin')
meta = {'names': a['names'], 'frames': frames, 'vertices': int(V.shape[2]), 'faces': faces.reshape(-1).tolist(), 'center': center.tolist(),
        'dist_mean_mm': [round(float(x), 2) for x in dist.mean(1)], 'dist_max_mm': [round(float(x), 2) for x in dist.max(1)],
        'dist_vertex_mean_mm': [round(float(x), 2) for x in dist.mean(0)]}
with open(out + '/mesh.json.tmp', 'w') as f: json.dump(meta, f)
os.replace(out + '/mesh.json.tmp', out + '/mesh.json')
'''


def preview(ep, f):
    case = case_dir(ep); out = case / 'preview' / f'{f}.jpg'
    masks = [case / 'mask' / str(c) / '1' / f'{f}.png' for c in range(4)]
    newest = max(p.stat().st_mtime for p in masks)
    if out.exists() and out.stat().st_mtime >= newest: return out.read_bytes()
    out.parent.mkdir(exist_ok=True); inputs, chains = [], []
    for c in range(4):
        inputs += ['-i', str(case / 'color' / str(c) / f'{f}.png'), '-i', str(masks[c])]
        chains.append(f'[{2*c+1}]format=gray,colorchannelmixer=rr=1:gg=0:bb=0[m{c}];[{2*c}][m{c}]blend=all_mode=screen[o{c}]')
    graph = ';'.join(chains) + ';[o0][o1]hstack[a];[o2][o3]hstack[b];[a][b]vstack,scale=1280:-1'
    subprocess.run(['ffmpeg', '-loglevel', 'error', '-y', *inputs, '-filter_complex', graph, '-q:v', '4', str(out)], check=True, timeout=60)
    return out.read_bytes()


# ---------- 프로필 (NAS) ----------
_profile_cache = {'at': 0, 'value': None}


def list_profiles(fresh=False):
    """{'profiles': [...], 'assignments': {RAW: {...}}, 'error': None|str}. NAS를 10초 동안 기억합니다."""
    if not fresh and _profile_cache['value'] and time.time() - _profile_cache['at'] < 10: return _profile_cache['value']
    out = {'profiles': [], 'assignments': {}, 'error': None, 'root': str(PROFILES)}
    try:
        hp = hand_profile()
        for p in sorted(PROFILES.glob('*.json')):
            try:
                prof, digest = hp.load(p, ())
                out['profiles'].append({'id': prof['profile_id'], 'sha256': digest, 'created': prof.get('created'), 'method': prof.get('method'),
                                        'note': prof.get('note', ''), 'betas': prof.get('betas'), 'size': prof.get('hand_size'),
                                        'episodes': [Path(s).parts[-3] for s in (prof.get('sources') or {})], 'by': prof.get('created_by')})
            except (OSError, ValueError, KeyError) as e:
                out['profiles'].append({'id': p.stem, 'invalid': str(e)})
        for p in sorted(ASSIGN.glob('ep_*.json')):
            try: out['assignments'][p.stem] = json.loads(p.read_text())
            except (OSError, ValueError): pass
    except OSError as e:
        out['error'] = f'NAS 프로필 폴더를 읽지 못했습니다: {e}'
    except ImportError:
        out['error'] = '이 PC에는 손 형상 프로필 코드(hand_profile.py)가 없습니다.'
    _profile_cache.update(at=time.time(), value=out)
    return out


def make_profile(pid, episodes, note, who):
    if not PID.fullmatch(pid): raise UserError('프로필 이름은 영문·숫자로 시작하고 영문·숫자·_ . - 만 쓸 수 있습니다 (최대 64자).')
    if not episodes: raise UserError('β 측정이 끝난 에피소드를 하나 이상 고르세요.')
    sources, codes = [], set()
    for ep in episodes:
        if not EP.fullmatch(ep): raise UserError('잘못된 에피소드입니다.')
        st, _ = status(ep); m = measurement(ep)
        if st in ('running', 'queued'): raise UserError(f'{ep}: 실행 중입니다.')
        if not m: raise UserError(f'{ep}: β 측정 결과가 없습니다.')
        if m['stale']: raise UserError(f'{ep}: 측정 뒤 마스크를 고쳤습니다. β를 다시 측정하세요.')
        sources.append(m['path']); codes.add(json.dumps(read_state(ep).get('measure_code'), sort_keys=True))
    PROFILES.mkdir(parents=True, exist_ok=True)
    measure = {'tool': 'kaist-ui MANO beta', 'episodes': episodes, 'code': [json.loads(c) for c in sorted(codes)]}
    out = PROFILES / f'{pid}.json'
    profile, digest = hand_profile().build(pid, sources, mano_dir(), out, 'right', note, measure)
    # 표시용 손 크기와 만든 사람: 파일을 만든 직후 같은 내용으로 추가(형식 검사 대상 아님). 해시는 최종 파일 기준입니다.
    size = hand_size([profile['betas']['right']])
    profile.update(created_by=who, hand_size=size[0] if size else None)
    tmp = out.with_name(f'.{out.name}.{os.getpid()}.size.tmp'); tmp.write_text(json.dumps(profile, ensure_ascii=False, indent=1) + '\n'); tmp.replace(out)
    _profile_cache['at'] = 0
    for ep in episodes: log(ep, f'프로필 {pid}에 포함 ({who})')
    return {'id': pid, 'sha256': sha256(out), 'betas': profile['betas']['right'], 'size': profile.get('hand_size'), 'path': str(out)}


def assign(raw, pid, who):
    if not EP.fullmatch(raw): raise UserError('잘못된 RAW 에피소드입니다.')
    ASSIGN.mkdir(parents=True, exist_ok=True)
    path = ASSIGN / f'{raw}.json'
    if not pid:
        if path.exists():
            old = json.loads(path.read_text())
            hist = ASSIGN / 'history'; hist.mkdir(exist_ok=True)
            (hist / f'{raw}.{time.strftime("%Y%m%d_%H%M%S")}.json').write_text(json.dumps(dict(old, removed_by=who, removed_at=time.time()), ensure_ascii=False))
            path.unlink()
        _profile_cache['at'] = 0
        return {'episode': raw, 'profile': None}
    if not PID.fullmatch(pid) or not (PROFILES / f'{pid}.json').is_file(): raise UserError(f'프로필 {pid}이(가) 없습니다.')
    hand_profile().load(PROFILES / f'{pid}.json')   # 오른손 β 10개·형식 검사
    record = {'profile': pid, 'sha256': sha256(PROFILES / f'{pid}.json'), 'by': who, 'at': time.time(),
              'at_text': datetime.datetime.now().astimezone().isoformat(timespec='seconds')}
    tmp = path.with_name(f'.{path.name}.{os.getpid()}.tmp'); tmp.write_text(json.dumps(record, ensure_ascii=False, indent=1)); tmp.replace(path)
    _profile_cache['at'] = 0
    return {'episode': raw, **record}


# ---------- 화면·API ----------
class RunBody(BaseModel):
    episodes: list[str] = Field(default_factory=list, max_length=50)
    step: str = 'sam'


class MaskBody(BaseModel):
    ep: str; cam: int; f: int; data: str = Field(default='', max_length=4_000_000)


class FrameBody(BaseModel):
    ep: str; cam: int; f: int


class WandBody(BaseModel):
    ep: str; cam: int; f: int; points: list[list[float]] = Field(default_factory=list, max_length=60); text: str = Field(default='', max_length=40)


class ProfileBody(BaseModel):
    id: str = Field(max_length=64); episodes: list[str] = Field(max_length=50); note: str = Field(default='', max_length=300)


class AssignBody(BaseModel):
    episode: str = Field(max_length=80); profile: str | None = Field(default=None, max_length=64)


def setup(app, guard, access, csrf):
    """platform_app에 경로를 붙입니다. guard: 주소·CSRF·로그인 검사, access: 사용자·권한·감사 기록."""
    if not ui_enabled(): return  # 화면을 끈 PC는 경로도 만들지 않습니다(404).
    def need_tool():
        if not tool_available(): raise HTTPException(409, '이 PC에서는 MANO beta 측정 도구를 쓸 수 없습니다(Worker 파이프라인 사본이 있는 Native PC 전용).')
        start_worker()

    def fail(e):
        if isinstance(e, HTTPException): raise e
        if isinstance(e, (UserError, ValueError, KeyError, OSError, ImportError)): raise HTTPException(409, str(e))
        raise HTTPException(500, str(e))

    @app.get('/mano-beta')
    def page(req: Request):
        guard(req)
        html = (HERE / 'hand-shape.html').read_text().replace('__CSRF__', csrf)
        return HTMLResponse(html, headers={'Cache-Control': 'no-store', 'Content-Security-Policy':
                            "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; frame-ancestors 'self'"})

    @app.get('/api/hand-shape/state')
    def state(req: Request, label: str = 'Test', refresh_list: int = 0):
        guard(req)
        if not tool_available(): return {'available': False, 'rows': [], 'profiles': list_profiles()}
        start_worker(); label = (label or 'Test')[:40]
        if refresh_list or catalog['label'] != label or time.time() - catalog['at'] > 300: refresh(label)
        rows = []
        for r in catalog['rows']:
            ep = r['episode']; st, detail = status(ep); s = read_state(ep)
            rows.append(dict({k: v for k, v in r.items() if k != 'source'}, state=st, detail=detail,
                             masks=mask_summary(ep) if masks_ready(ep) else None,
                             filled=sum(len(v) for v in (s.get('filled') or {}).values()), measure=measurement(ep), hamba=hamba_done(ep)))
        return {'available': True, 'rows': rows, 'error': catalog['error'], 'wand': wand.status(), 'base': str(BASE),
                'scanned': time.strftime('%H:%M:%S', time.localtime(catalog['at'])), 'profiles': list_profiles()}

    @app.get('/api/hand-shape/log')
    def get_log(req: Request, ep: str):
        guard(req)
        if not EP.fullmatch(ep): raise HTTPException(400, '잘못된 에피소드입니다.')
        p = LOGS / f'{ep}.log'
        lines = p.read_text(errors='replace').splitlines()[-120:] if p.exists() else ['(아직 로그 없음)']
        lines = [l for l in lines if not re.search(r'\d+it/s\]|s/it\]', l)]
        return Response('\n'.join(lines), media_type='text/plain; charset=utf-8', headers={'Cache-Control': 'no-store'})

    @app.get('/api/hand-shape/img/{kind}')
    def image(req: Request, kind: str, ep: str, f: int, cam: int = 0):
        guard(req)
        if kind not in ('preview', 'color', 'mask') or not EP.fullmatch(ep) or cam not in range(4) or f < 0: raise HTTPException(400, '잘못된 요청입니다.')
        try:
            if kind == 'preview': return Response(preview(ep, f), media_type='image/jpeg', headers={'Cache-Control': 'no-store'})
            color, mask, _ = frame_paths(ep, cam, f); p = color if kind == 'color' else mask
            if not p.exists(): raise HTTPException(404, '파일이 없습니다.')
            return Response(p.read_bytes(), media_type='image/png', headers={'Cache-Control': 'no-store'})
        except Exception as e: fail(e)

    @app.post('/api/hand-shape/run')
    def run_steps(data: RunBody, req: Request):
        guard(req); need_tool()
        if data.step not in ('sam', 'measure'): raise HTTPException(400, '잘못된 단계입니다.')
        known = {r['episode'] for r in catalog['rows']}; added = []
        for ep in data.episodes:
            if not (EP.fullmatch(ep) and ep in known): continue
            if data.step == 'measure' and not (read_state(ep).get('sam_done') and masks_ready(ep)): continue
            with state_lock:
                if ep in running or any(e == ep for e, _ in queue): continue
                queue.append((ep, data.step)); added.append(ep)
            write_state(ep, error=None)
            log(ep, f'실행 대기열에 추가: {"SAM3 손 분할" if data.step == "sam" else "β 측정"} ({access.user(req)["name"]})')
        if added: access.audit(req, f'mano-beta {data.step} ' + ','.join(added))
        return {'added': added}

    @app.post('/api/hand-shape/mask')
    def mask(data: MaskBody, req: Request):
        guard(req); need_tool()
        try: return save_mask(data.ep, data.cam, data.f, data.data, access.user(req)['name'])
        except Exception as e: fail(e)

    @app.post('/api/hand-shape/mask/revert')
    def mask_revert(data: FrameBody, req: Request):
        guard(req); need_tool()
        try: return revert_mask(data.ep, data.cam, data.f, access.user(req)['name'])
        except Exception as e: fail(e)

    @app.post('/api/hand-shape/wand')
    def wand_call(data: WandBody, req: Request):
        guard(req); need_tool()
        try:
            check_edit(data.ep, data.cam, data.f)
            pts = []
            for p in data.points:
                if len(p) != 3: raise UserError('점 형식이 잘못됐습니다.')
                x, y, l = round(float(p[0]), 5), round(float(p[1]), 5), int(p[2])
                if not (0 <= x <= 1 and 0 <= y <= 1) or l not in (0, 1): raise UserError('점 좌표가 화면 밖입니다.')
                pts.append([x, y, l])
            if not any(p[2] == 1 for p in pts): raise UserError('포함할 곳(왼쪽 클릭)을 하나 이상 찍어 주세요.')
            color, _, _ = frame_paths(data.ep, data.cam, data.f)
            r = wand.call({'image': str(color.resolve()), 'points': pts, 'text': data.text, 'threshold': 0.5})
            return {k: r.get(k) for k in ('mask_png', 'width', 'height', 'method', 'score', 'message')}
        except Exception as e: fail(e)

    @app.post('/api/hand-shape/profile')
    def create_profile(data: ProfileBody, req: Request):
        guard(req); need_tool()
        who = access.user(req)['name']
        try: out = make_profile(data.id.strip(), list(dict.fromkeys(data.episodes)), data.note.strip(), who)
        except Exception as e: fail(e)
        access.audit(req, f'mano-beta profile {out["id"]} {out["sha256"][:12]}')
        return out

    @app.get('/api/hand-shape/compare')
    def compare_list(req: Request):
        guard(req)
        try: status = json.loads((COMPARE_WORK / 'status.json').read_text())
        except (OSError, ValueError): status = None
        results = []
        try:
            for p in sorted(COMPARE_OUT.glob('ep_*/summary.json')):
                try:
                    d = json.loads(p.read_text()); b = d.get('between_variants') or {}
                    results.append({'episode': d['episode'], 'frames': d.get('frames'), 'created': d.get('created'), 'variants': d.get('variants'),
                                    'profile': (d.get('profile') or {}).get('id'), 'between': {k: v for k, v in b.items() if k != 'per_frame_mean_mm'},
                                    'per_frame': b.get('per_frame_mean_mm')})
                except (OSError, ValueError, KeyError): pass
        except OSError as e: return {'status': status, 'results': [], 'error': str(e), 'out': str(COMPARE_OUT)}
        try: analysis = json.loads((COMPARE_OUT / 'shape_analysis.json').read_text())
        except (OSError, ValueError): analysis = None
        return {'status': status, 'results': results, 'out': str(COMPARE_OUT), 'analysis': analysis}

    @app.get('/api/hand-shape/compare/video')
    def compare_video(req: Request, ep: str, v: str = 'compare'):
        guard(req)
        if not EP.fullmatch(ep) or not (v == 'compare' or VARIANT.fullmatch(v)): raise HTTPException(400, '잘못된 요청입니다.')
        p = COMPARE_OUT / ep / 'compare.mp4' if v == 'compare' else variant_dir(ep, v) / 'review/mano_review.mp4'
        if not p.is_file(): raise HTTPException(404, '영상이 없습니다.')
        return FileResponse(p, media_type='video/mp4', headers={'Cache-Control': 'no-store'})

    @app.get('/mano-beta/compare')
    def compare_page(req: Request, ep: str = ''):
        guard(req)
        html = (HERE / 'hand-compare.html').read_text().replace('__CSRF__', csrf)
        return HTMLResponse(html, headers={'Cache-Control': 'no-store', 'Content-Security-Policy':
                            "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; media-src 'self'; frame-ancestors 'self'"})

    @app.get('/mano-beta/vendor/{name}')
    def compare_vendor(req: Request, name: str):
        guard(req)
        if name not in ('three.module.js', 'OrbitControls.js'): raise HTTPException(404, '없음')
        return FileResponse(REPO / 'code/viewer/app/vendor' / name, media_type='application/javascript', headers={'Cache-Control': 'max-age=3600'})

    @app.get('/api/hand-shape/compare/variants')
    def compare_variants(req: Request, ep: str):
        guard(req)
        if not EP.fullmatch(ep): raise HTTPException(400, '잘못된 요청입니다.')
        out = []
        cands = [p.name for p in sorted((COMPARE_OUT / ep).glob('*')) if (p / 'hand_v1/hand_reg.pkl').is_file()]
        cands += [p.name for p in sorted((COMPARE_WORK / ep / 'sweep').glob('*')) if SWEEP.fullmatch(p.name) and (p / 'review/mano_review.mp4').is_file()]
        for v in cands:
            m = None
            for d in (COMPARE_WORK / ep / v, COMPARE_WORK / ep / 'sweep' / v):
                try: m = json.loads((d / 'finger_metrics.json').read_text()); break
                except (OSError, ValueError): pass
            out.append({'id': v, 'label': variant_label(v), 'metrics': m, 'sweep': bool(SWEEP.fullmatch(v))})
        return {'episode': ep, 'variants': out}

    @app.get('/api/hand-shape/compare/mesh')
    def compare_mesh(req: Request, ep: str, part: str = 'json', a: str = '', b: str = ''):
        """두 방식 손 메시(프레임별 778점)를 3D 비교용으로 변환: Set2/<ep>/viewer/mesh.json + mesh.bin(float32 [방식,프레임,778,3])."""
        guard(req)
        if not EP.fullmatch(ep) or part not in ('json', 'bin'): raise HTTPException(400, '잘못된 요청입니다.')
        d = COMPARE_OUT / ep
        try: summ = json.loads((d / 'summary.json').read_text())
        except (OSError, ValueError): raise HTTPException(404, '비교 결과가 없습니다.')
        names = [a or list(summ['variants'])[0], b or list(summ['variants'])[1]]
        regs = [variant_dir(ep, n) / 'hand_v1/hand_reg.pkl' for n in names]
        v = COMPARE_WORK / ep / 'viewer' / hashlib.sha256('|'.join(names).encode()).hexdigest()[:16]; v.parent.mkdir(parents=True, exist_ok=True)
        fresh = (v / 'mesh.json').exists() and (v / 'mesh.bin').exists() and all((v / 'mesh.json').stat().st_mtime >= r.stat().st_mtime for r in regs)
        if not fresh:
            with _mesh_lock:
                if not ((v / 'mesh.json').exists() and all((v / 'mesh.json').stat().st_mtime >= r.stat().st_mtime for r in regs)):
                    v.mkdir(exist_ok=True)
                    r = subprocess.run([str(workspace() / 'hamba-env/bin/python'), '-c', MESH_CODE, json.dumps({'regs': [str(x) for x in regs], 'names': names, 'out': str(v)})],
                                       cwd=mono(), env=hands_env(), capture_output=True, text=True, timeout=300)
                    if r.returncode: raise HTTPException(500, '메시 변환 실패: ' + r.stderr[-400:])
        if part == 'bin': return FileResponse(v / 'mesh.bin', media_type='application/octet-stream', headers={'Cache-Control': 'no-store'})
        meta = json.loads((v / 'mesh.json').read_text())
        meta['names'] = names; meta['labels'] = [variant_label(n) for n in names]
        meta['summary'] = summ if set(names) == set(summ['variants']) else None
        try:
            a = json.loads((COMPARE_OUT / 'shape_analysis.json').read_text())
            meta['analysis'] = next((e for e in a['episodes'] if e['episode'] == ep), None); meta['profile_size'] = a['profile']['size']
        except (OSError, ValueError, KeyError): pass
        return meta

    @app.get('/api/hand-profiles')
    def profiles(req: Request, fresh: int = 0):
        guard(req)
        return list_profiles(bool(fresh))

    @app.post('/api/hand-profiles/assign')
    def assign_profile(data: AssignBody, req: Request):
        guard(req)
        raw = data.episode
        if not (access.allowed(req, raw) or access.selectable(req, raw)): raise HTTPException(403, '본인에게 배정된 RAW 에피소드만 지정할 수 있습니다.')
        try: out = assign(raw, (data.profile or '').strip() or None, access.user(req)['name'])
        except Exception as e: fail(e)
        access.audit(req, f'hand-profile {raw} -> {out.get("profile") or "없음"}')
        return out
