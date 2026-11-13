"""Pilot name sessions and episode ownership. Names are not verified identities."""
import secrets,time,json
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
 return u['role']=='admin' or not owner or owner==u['name']

def execution_worker(episode):
 with db() as c:r=c.execute('SELECT worker_id FROM execution_workers WHERE episode=?',(episode,)).fetchone()
 return r['worker_id'] if r else 'gpu_4090_a'
