"""Pilot name sessions and episode ownership. Names are not verified identities."""
import secrets,time,json,threading,re,os
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
 c.execute('CREATE TABLE IF NOT EXISTS raw_exclusions (worker_id TEXT NOT NULL,episode TEXT NOT NULL,operator TEXT NOT NULL,at REAL,PRIMARY KEY(worker_id,episode,operator))')
 c.execute('CREATE TABLE IF NOT EXISTS ui_hidden (scope TEXT NOT NULL,key TEXT NOT NULL,episode TEXT,name TEXT,at REAL,PRIMARY KEY(scope,key))')
 return c

# 로그인 이름은 PC1~PC6만 허용합니다. 이 이름(소문자)이 NAS 결과 폴더 results/pc1~pc6이 됩니다.
PC_NAMES=tuple(f'PC{i}' for i in range(1,7))

def pc_name(name):
 """'pc1', 'PC 1', 'pc-1' → 'PC1'. 허용 목록에 없으면 None."""
 n=re.sub(r'[\s_-]+','',str(name or '')).upper()
 return n if n in PC_NAMES else None

def login(name,role):
 token=secrets.token_urlsafe(32)
 with db() as c:c.execute('INSERT INTO ui_sessions VALUES(?,?,?,?)',(token,name,role,time.time()+12*3600))
 return token

def user(req):
 with db() as c:r=c.execute('SELECT name,role FROM ui_sessions WHERE token=? AND expires>?',(req.cookies.get('kaist_session',''),time.time())).fetchone()
 # PC1~PC6가 아닌 이름으로 만들어진 예전 세션은 다시 로그인해야 합니다.
 if not r or not pc_name(r['name']):raise HTTPException(401,'PC1~PC6 중 하나로 로그인하세요.')
 # 이 화면은 PC_ID의 PC만 조작합니다. 다른 PC 이름 세션(예전 로그인)은 다시 로그인 → 그 PC 화면으로 넘어갑니다.
 here=os.environ.get('PC_ID','').strip().lower()
 if re.fullmatch(r'pc[1-6]',here) and r['name'].lower()!=here:raise HTTPException(401,f'이 화면은 {here.upper()}입니다. 다시 로그인하세요.')
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

def selectable(req,episode,worker_id=None):
 u=user(req);owner=owners().get(episode)
 if u['role']=='admin':return True
 if episode in excluded_raw(u['name'],worker_id):return False
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
 rows=[r for r in rows if pc_name(r['name'])]  # 예전 이름 세션은 무효 → 배정 대상에서 제외
 ops=sorted(r['name'] for r in rows if r['op'])
 return {'total':len(rows),'operators':ops,'admins':len(rows)-len(ops)}

def assignments():
 with db() as c:return {r['episode']:r['operator'] for r in c.execute('SELECT episode,operator FROM raw_assignments')}

def excluded_raw(name,worker_id=None):
 with db() as c:
  if worker_id is None:rows=c.execute('SELECT episode FROM raw_exclusions WHERE operator=?',(name,))
  else:rows=c.execute('SELECT episode FROM raw_exclusions WHERE operator=? AND worker_id=?',(name,worker_id))
  return {r['episode'] for r in rows}

def exclude_raw(name,worker_id,episode,excluded=True):
 """Remove only this operator's assignment; retain RAW, ownership and run history."""
 with _assign_lock:
  owner=owners().get(episode)
  with db() as c:
   c.execute('BEGIN IMMEDIATE')
   previous=c.execute('SELECT 1 FROM raw_exclusions WHERE worker_id=? AND episode=? AND operator=?',(worker_id,episode,name)).fetchone()
   assigned=c.execute('SELECT operator,worker_id FROM raw_assignments WHERE episode=?',(episode,)).fetchone()
   if excluded:
    mine=owner==name or (not owner and assigned and assigned['operator']==name and assigned['worker_id']==worker_id)
    if not mine and not previous:raise HTTPException(403,'본인의 내 작업 항목만 제외할 수 있습니다.')
    c.execute('INSERT OR REPLACE INTO raw_exclusions VALUES(?,?,?,?)',(worker_id,episode,name,time.time()))
    c.execute('DELETE FROM raw_assignments WHERE episode=? AND operator=? AND worker_id=?',(episode,name,worker_id))
   else:
    if not previous:raise HTTPException(404,'본인이 제외한 항목이 아닙니다.')
    c.execute('DELETE FROM raw_exclusions WHERE worker_id=? AND episode=? AND operator=?',(worker_id,episode,name))
   c.execute('INSERT INTO ui_audit VALUES(?,?,?)',(time.time(),name,('exclude_raw ' if excluded else 'restore_raw ')+worker_id+' '+episode))

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
   excluded={(r['worker_id'],r['episode'],r['operator']) for r in c.execute('SELECT worker_id,episode,operator FROM raw_exclusions')}
   rows={r['episode']:(r['operator'],r['worker_id']) for r in c.execute('SELECT episode,operator,worker_id FROM raw_assignments')}
   for ep,(op,wid) in list(rows.items()):
    if own.get(ep) or ep in started or op not in ops or (wid==worker_id and ep not in eps) or (wid,ep,op) in excluded:
     c.execute('DELETE FROM raw_assignments WHERE episode=?',(ep,));del rows[ep]
   load={u:0 for u in ops}
   for ep,(op,_) in rows.items():load[op]+=1
   for ep in eps:
    o=own.get(ep)
    if o in load and ep not in done and (worker_id,ep,o) not in excluded:load[o]+=1
   pool=[ep for ep in sorted(eps,key=capture_key) if not own.get(ep) and ep not in started and ep not in rows]
   for ep in pool:
    needy=[u for u in ops if load[u]<target and (worker_id,ep,u) not in excluded]
    if not needy:continue
    u=min(needy,key=lambda n:(load[n],n))
    c.execute('INSERT INTO raw_assignments VALUES(?,?,?,?)',(ep,u,worker_id,time.time()));rows[ep]=(u,worker_id);load[u]+=1
  return {ep:op for ep,(op,_) in rows.items()},dict(users,load=load,target=target)
