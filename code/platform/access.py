"""Pilot name sessions and episode ownership. Names are not verified identities."""
import secrets,time,json,threading
import intake
from fastapi import HTTPException

def db():
 c=intake.db()
 c.execute('CREATE TABLE IF NOT EXISTS ui_sessions (token TEXT PRIMARY KEY,name TEXT,role TEXT,expires REAL)')
 c.execute('CREATE TABLE IF NOT EXISTS episode_owners (episode TEXT PRIMARY KEY,operator TEXT)')
 c.execute('CREATE TABLE IF NOT EXISTS execution_versions (run_id TEXT PRIMARY KEY, raw_episode TEXT NOT NULL, episode TEXT UNIQUE NOT NULL, number INTEGER NOT NULL, operator TEXT NOT NULL, UNIQUE(raw_episode,number))')
 c.execute('CREATE TABLE IF NOT EXISTS execution_workers (run_id TEXT PRIMARY KEY, episode TEXT UNIQUE NOT NULL, worker_id TEXT NOT NULL)')
 c.execute('CREATE TABLE IF NOT EXISTS ui_audit (at REAL,name TEXT,action TEXT)')
 # 목록에서 삭제(숨김): 실제 데이터·실행 기록은 지우지 않습니다. scope=jobs|outputs|review
 # 자동 배정(아직 실행 전): 실행을 시작하면 episode_owners/execution_versions의 담당으로 넘어가고 여기서는 빠집니다.
 c.execute('CREATE TABLE IF NOT EXISTS raw_assignments (episode TEXT PRIMARY KEY,operator TEXT NOT NULL,worker_id TEXT NOT NULL,at REAL)')
 c.execute('CREATE TABLE IF NOT EXISTS ui_hidden (scope TEXT NOT NULL,key TEXT NOT NULL,episode TEXT,name TEXT,at REAL,PRIMARY KEY(scope,key))')
 return c

def login(name,role):
 token=secrets.token_urlsafe(32)
 with db() as c:c.execute('INSERT INTO ui_sessions VALUES(?,?,?,?)',(token,name,role,time.time()+12*3600))
 return token

def user(req):
 with db() as c:r=c.execute('SELECT name,role FROM ui_sessions WHERE token=? AND expires>?',(req.cookies.get('kaist_session',''),time.time())).fetchone()
 if not r:raise HTTPException(401,'이름을 입력하고 로그인하세요.')
 return dict(r)

def admin(req):
 u=user(req)
 if u['role']!='admin':raise HTTPException(403,'관리자만 사용할 수 있습니다.')
 return u

def owners():
 with db() as c:out={r['episode']:r['operator'] for r in c.execute('SELECT * FROM episode_owners')}
 with db() as c:out.update({r['episode']:r['operator'] for r in c.execute('SELECT episode,operator FROM execution_versions')})
 out.update({r['episode']:r['operator'] for r in intake.rows() if r['operator']})
 return out

def allowed(req,episode):
 u=user(req)
 return u['role']=='admin' or owners().get(episode)==u['name']

def require(req,episode):
 if not allowed(req,episode):raise HTTPException(403,'본인에게 배정된 에피소드만 사용할 수 있습니다.')

def audit(req,action):
 with db() as c:c.execute('INSERT INTO ui_audit VALUES(?,?,?)',(time.time(),user(req)['name'],action))

def selectable(req,episode):
 u=user(req);owner=owners().get(episode)
 if u['role']=='admin':return True
 if owner:return owner==u['name']
 # 담당자가 없는 RAW는 자동 배정받은 작업자만 시작할 수 있습니다.
 return assignments().get(episode)==u['name']

def execution_worker(episode):
 with db() as c:r=c.execute('SELECT worker_id FROM execution_workers WHERE episode=?',(episode,)).fetchone()
 return r['worker_id'] if r else 'gpu_4090_a'


# ---- RAW 자동 배정: 로그인한 작업자(관리자 제외)마다 진행 중인 RAW를 ASSIGN_TARGET개로 유지 ----
ASSIGN_TARGET=3
_assign_lock=threading.Lock()

def logged_in():
 """지금 세션이 유효한 사용자. 같은 이름이 작업자로도 로그인했으면 작업자로 셉니다."""
 with db() as c:rows=c.execute("SELECT name,MAX(role='operator') op FROM ui_sessions WHERE expires>? GROUP BY name",(time.time(),)).fetchall()
 ops=sorted(r['name'] for r in rows if r['op'])
 return {'total':len(rows),'operators':ops,'admins':len(rows)-len(ops)}

def assignments():
 with db() as c:return {r['episode']:r['operator'] for r in c.execute('SELECT episode,operator FROM raw_assignments')}

def capture_key(ep):
 # ep_YYYYMMDD_HHMMSS → 촬영 시각 순서(오래된 것부터)
 return ep

def rebalance(worker_id,episodes,done,started,target=ASSIGN_TARGET):
 """episodes: 이 PC의 RAW 목록, done: 3D 복원까지 끝난 RAW, started: 실행 기록이 있는 RAW.
 - 회수: 실행을 시작했거나(담당자 생김) 목록에서 사라졌거나 배정받은 작업자의 세션이 없으면 배정을 풉니다.
 - 부하: 작업자별 (시작 전 배정) + (본인 담당이면서 3D 복원이 끝나지 않은 RAW)
 - 채우기: 담당자·실행 기록·배정이 없는 RAW를 오래된 촬영부터, 부하가 가장 적은 작업자에게 하나씩 줍니다."""
 with _assign_lock:
  users=logged_in();ops=users['operators'];own=owners();eps=set(episodes)
  with db() as c:
   rows={r['episode']:(r['operator'],r['worker_id']) for r in c.execute('SELECT episode,operator,worker_id FROM raw_assignments')}
   for ep,(op,wid) in list(rows.items()):
    if own.get(ep) or ep in started or op not in ops or (wid==worker_id and ep not in eps):
     c.execute('DELETE FROM raw_assignments WHERE episode=?',(ep,));del rows[ep]
   load={u:0 for u in ops}
   for ep,(op,_) in rows.items():load[op]+=1
   for ep in eps:
    o=own.get(ep)
    if o in load and ep not in done:load[o]+=1
   pool=[ep for ep in sorted(eps,key=capture_key) if not own.get(ep) and ep not in started and ep not in rows]
   while pool:
    needy=[u for u in ops if load[u]<target]
    if not needy:break
    u=min(needy,key=lambda n:(load[n],n));ep=pool.pop(0)
    c.execute('INSERT INTO raw_assignments VALUES(?,?,?,?)',(ep,u,worker_id,time.time()));rows[ep]=(u,worker_id);load[u]+=1
  return {ep:op for ep,(op,_) in rows.items()},dict(users,load=load,target=target)
