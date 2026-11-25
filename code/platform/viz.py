"""완료된 에피소드를 3D 뷰어(MANO 손 메시 · 물체 3D 추적점 · RGB)로 새 창에 띄웁니다.

  /viz/open?episode=ep_...   변환을 시작하고 준비되면 뷰어로 넘어가는 대기 화면
  /viz/status?episode=ep_... 변환 상태
  /viz/v/<episode>/...       뷰어 화면(code/viewer/app) + 변환된 data.json·rgb/

원본 결과는 읽기만 하고, 변환본은 UI DB 옆 viz_cache/<episode>/<key>에 둡니다(최근에 연 20개만).
변환은 UI와 같은 Python(numpy)과 시스템 ffmpeg로 합니다. MANO 면 정보(mano_faces.pkl)는 모델 weight에서 읽습니다.
"""
from pathlib import Path
import hashlib, html, json, os, re, shutil, subprocess, sys, threading, time
from fastapi import HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse

VIEWER = Path(__file__).resolve().parents[1]/'viewer'
APP = VIEWER/'app'
PYTHON = os.environ.get('KAIST_VIZ_PYTHON') or sys.executable
CACHE = Path(os.environ.get('KAIST_VIZ_CACHE') or (Path(os.environ['KAIST_INTAKE_DB']).parent if os.environ.get('KAIST_INTAKE_DB') else Path(__file__).resolve().parents[2]/'intake_state')/'viz_cache')
# MANO 면 정보: Docker는 compose가 weight 파일을 이 경로에 마운트하고, Docker 없이 실행할 때는 weight 폴더에서 바로 읽습니다.
_WEIGHTS = os.environ.get('KAIST_WEIGHTS_ROOT') or str(Path.home()/'Documents/Codex/2026-10-20/https-drive-google-com-drive-folders/outputs/WFM_KAIST/workspace')
FACES = [Path(p) for p in (os.environ.get('KAIST_MANO_FACES') or '', '/opt/kaist-weights/mano_faces.pkl',
                           _WEIGHTS+'/kaist-full/PhysTwin/data/mano_faces.pkl', _WEIGHTS+'/kaist-full/PhysTwin/Mono/data/mano_faces.pkl') if p]
NAS = Path(os.environ.get('NAS_MOUNT', '/mnt/kaist-data'))
WORK = os.environ.get('KAIST_WORK_DIR') or os.environ.get('GPU_PROJECT_DIR') or ''
EPISODE = re.compile(r'ep_[0-9A-Za-z_]{1,60}')
KEEP_EPISODES = 20          # 변환본은 최근에 연 에피소드 20개만 둡니다(에피소드당 약 25MB).
_jobs = {}                  # episode → {'state','message','key','started'}
_lock = threading.Lock()


def _complete(d):
    return (d/'final_data.pkl').is_file() and (d/'hands/hand_v1/hand_reg.pkl').is_file()


def locate(episode):
    """(결과 폴더, color 폴더). NAS 최종 결과가 우선이고, 아직 저장 전이면 작업 폴더의 3D·MANO 결과를 씁니다."""
    found = []
    results = NAS/'results'
    try:
        pcs = [p for p in results.iterdir() if p.is_dir() and re.fullmatch(r'pc\d+', p.name)]
    except OSError:
        pcs = []
    for pc in pcs:
        try:
            parents = [pc/episode] + [t/episode for t in pc.iterdir() if t.is_dir() and not t.name.startswith(('ep_', '.', '#'))]
            for parent in parents:
                if parent.is_dir():
                    found += [k for k in parent.iterdir() if k.is_dir() and _complete(k)]
        except OSError:
            continue
    result = max(found, key=lambda k: (k/'hands/hand_v1/hand_reg.pkl').stat().st_mtime) if found else None
    works = ([Path(WORK)] if WORK else []) + [NAS/'work'/pc.name for pc in pcs]
    if result is None:
        result = next((w/'06_reconstruction'/episode for w in works if _complete(w/'06_reconstruction'/episode)), None)
    if result is None:
        raise HTTPException(404, '3D 복원·MANO 결과(final_data.pkl, hands/hand_v1/hand_reg.pkl)를 찾지 못했습니다.')
    color = next((w/'03_prepared'/episode/'color' for w in works if (w/'03_prepared'/episode/'color/0.mp4').is_file()), None)
    if color is None:
        raise HTTPException(404, '동기화된 RGB 영상(03_prepared/<에피소드>/color/N.mp4)을 찾지 못했습니다.')
    return result, color


def faces_file():
    found = next((p for p in FACES if p.is_file()), None)
    if found is None:
        raise HTTPException(404, 'MANO 면 정보(mano_faces.pkl)를 찾지 못했습니다. 모델 weight 위치를 확인하세요.')
    return found


def cache_key(result, color):
    parts = [str(result), str(color)]
    for p in (result/'final_data.pkl', result/'hands/hand_v1/hand_reg.pkl', color/'0.mp4'):
        st = p.stat()
        parts.append(f'{st.st_size}:{int(st.st_mtime)}')
    return hashlib.sha1('|'.join(parts).encode()).hexdigest()[:16]


def ready_dir(episode):
    job = _jobs.get(episode)
    if job and job['state'] == 'ready' and (CACHE/episode/job['key']/'data.json').is_file():
        return CACHE/episode/job['key']
    return None


def _trim():
    try:
        dirs = sorted((d for d in CACHE.iterdir() if d.is_dir() and EPISODE.fullmatch(d.name)), key=lambda d: d.stat().st_mtime, reverse=True)
        busy = {e for e, j in _jobs.items() if j['state'] == 'running'}
        for d in dirs[KEEP_EPISODES:]:
            if d.name not in busy:
                shutil.rmtree(d, ignore_errors=True)
                _jobs.pop(d.name, None)
    except OSError:
        pass


def _convert(episode, result, color, faces, key):
    base = CACHE/episode
    tmp = base/('.'+key+'.tmp')
    job = _jobs[episode]
    try:
        base.mkdir(parents=True, exist_ok=True)
        shutil.rmtree(tmp, ignore_errors=True)
        proc = subprocess.Popen([str(PYTHON), str(VIEWER/'viz_prepare.py'), '--episode', episode, '--result', str(result),
                                 '--color', str(color), '--faces', str(faces), '--out', str(tmp)], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors='replace')
        last = ''
        for line in proc.stdout:
            if line.strip():
                last = line.strip()
                job['message'] = last
        if proc.wait() != 0:
            raise RuntimeError(last.removeprefix('ERROR:').strip() or '변환 실패')
        for old in base.iterdir():          # 같은 에피소드의 이전 변환본 정리
            if old != tmp:
                shutil.rmtree(old, ignore_errors=True)
        os.replace(tmp, base/key)
        os.utime(base)
        job.update(state='ready', message='준비 완료')
        _trim()
    except Exception as e:
        shutil.rmtree(tmp, ignore_errors=True)
        job.update(state='failed', message=str(e)[-500:])


def start(episode):
    result, color = locate(episode)
    faces = faces_file()
    key = cache_key(result, color)
    with _lock:
        job = _jobs.get(episode)
        if job and job['state'] == 'running':
            return job
        if (CACHE/episode/key/'data.json').is_file():
            os.utime(CACHE/episode)
            _jobs[episode] = job = {'state': 'ready', 'message': '준비 완료', 'key': key, 'started': time.time()}
            return job
        _jobs[episode] = job = {'state': 'running', 'message': '결과 파일을 읽는 중', 'key': key, 'started': time.time()}
        threading.Thread(target=_convert, args=(episode, result, color, faces, key), name='kaist-viz', daemon=True).start()
        return job


WAIT_PAGE = """<!doctype html><html lang="ko"><head><meta charset="utf-8"><title>Visualization · __EP__</title>
<style>body{margin:0;background:#0d1118;color:#e7edf4;font-family:'Segoe UI','Malgun Gothic',sans-serif;display:grid;place-items:center;height:100vh}
main{text-align:center;max-width:720px;padding:24px}h1{font-size:18px;font-weight:600}p{color:#8fa1b8;line-height:1.7}code{color:#c5d9ef}
.err{color:#f0a3a3}button{border:1px solid #394559;border-radius:6px;background:#1b2533;color:#d9e3f1;padding:8px 14px;font:inherit;cursor:pointer}</style></head>
<body><main><h1>3D 시각화 준비 중</h1><p><code>__EP__</code></p><p id="msg">변환을 시작합니다…</p><p id="sec"></p></main>
<script>
const ep=__EPJS__,began=Date.now();
async function poll(){
 try{
  const r=await fetch('/viz/status?episode='+encodeURIComponent(ep),{cache:'no-store'});const d=await r.json();
  if(d.state==='ready'){location.replace('/viz/v/'+encodeURIComponent(ep)+'/');return}
  if(!r.ok||d.state==='failed'){document.getElementById('msg').innerHTML='<span class="err"></span><br><br><button onclick="location.reload()">다시 시도</button>';
   document.querySelector('.err').textContent='시각화를 준비하지 못했습니다. '+(d.message||d.detail||'');document.getElementById('sec').textContent='';return}
  document.getElementById('msg').textContent=d.message||'변환 중';
 }catch(e){document.getElementById('msg').textContent='서버 응답을 기다리는 중…'}
 document.getElementById('sec').textContent=Math.round((Date.now()-began)/1000)+'초 경과';
 setTimeout(poll,1000);
}
poll();
</script></body></html>"""


def install(app, guard, access):
    def check(req, episode):
        guard(req)
        if not EPISODE.fullmatch(episode or ''):
            raise HTTPException(400, '에피소드 이름을 확인하세요.')
        access.require(req, episode)

    @app.get('/viz/open')
    def viz_open(req: Request, episode: str):
        check(req, episode)
        try:
            start(episode)
        except HTTPException as e:      # 대기 화면에서 사유를 보여 줍니다.
            _jobs[episode] = {'state': 'failed', 'message': str(e.detail), 'key': '', 'started': time.time()}
        page = WAIT_PAGE.replace('__EPJS__', json.dumps(episode)).replace('__EP__', html.escape(episode))
        return HTMLResponse(page, headers={'Cache-Control': 'no-store'})

    @app.get('/viz/status')
    def viz_status(req: Request, episode: str):
        check(req, episode)
        job = _jobs.get(episode)
        if not job:
            return {'state': 'failed', 'message': '변환 기록이 없습니다. 다시 시도를 누르세요.'}
        return {'state': job['state'], 'message': job['message']}

    @app.get('/viz/v/{episode}/{path:path}')
    def viz_file(req: Request, episode: str, path: str = ''):
        check(req, episode)
        data = path in ('data.json', 'data_audit.json') or path.startswith('rgb/')
        if data:
            base = ready_dir(episode)
            if base is None:            # 서버를 다시 시작했어도 변환본이 남아 있으면 그대로 씁니다.
                if start(episode)['state'] != 'ready':
                    raise HTTPException(409, '변환 중입니다. Visualization 버튼으로 다시 여세요.')
                base = ready_dir(episode)
        elif path in ('', 'index.html', 'app.js', 'style.css') or path.startswith('vendor/'):
            base = APP
        else:
            raise HTTPException(404)
        target = (base/(path or 'index.html')).resolve()
        if not target.is_relative_to(base.resolve()) or not target.is_file():
            raise HTTPException(404)
        media = {'.js': 'text/javascript', '.json': 'application/json', '.css': 'text/css', '.html': 'text/html; charset=utf-8', '.jpg': 'image/jpeg'}.get(target.suffix)
        return FileResponse(target, media_type=media, headers={'Cache-Control': 'private, max-age=3600' if path.startswith('rgb/') else 'no-store'})
