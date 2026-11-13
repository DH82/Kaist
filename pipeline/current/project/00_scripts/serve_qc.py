#!/usr/bin/env python3
"""Loopback-only QC review server. Run beside project.json."""
import subprocess,sys
import argparse,fcntl,functools,hashlib,importlib.util,json,secrets,threading
from http.server import SimpleHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit,parse_qs,unquote
from qc_review_ui import page,CHECKS,prepare_media
import pipeline as qc

def report_path(project,value):
 if not isinstance(value,str):raise ValueError('보고서 경로가 필요합니다.')
 out=(project/value.lstrip('/')).resolve()
 base=(project/'02_qc').resolve()
 if out.parent!=base:raise ValueError('허용되지 않은 보고서 경로입니다.')
 if not (out/'provenance.json').is_file():raise ValueError('검사 출처 파일이 없습니다.')
 meta=qc.load(out/'provenance.json');root=(out/meta['root_relative']).resolve();case=(root/meta['case_relative']).resolve()
 if not root.is_relative_to(project) or not case.is_relative_to(project):raise ValueError('프로젝트 밖 RAW는 이 서버에서 검수할 수 없습니다.')
 if (out/meta.get('project_relative','.')).resolve()!=project:raise ValueError('보고서 프로젝트가 일치하지 않습니다.')
 return out,case

def checked_timeline(case,manifest):
 from qc_sync import timeline
 current=timeline(case)
 cached=qc.load(manifest)
 if cached!=current:
  raise ValueError('기존 QC 동시 화면의 시간축이 현재 전처리 정책/RAW와 다릅니다. 기존 결과는 보존됩니다. 02_capture_qc.py를 다시 실행한 후 새 QC 보고서를 여세요.')
 return cached


def state(out):
 p=out/'operator_review.json'
 return {'review':qc.load(p) if p.exists() else None,'revision':qc.sha(p) if p.exists() else ''}

def persist(project,data):
 out,_=report_path(project,data.get('report'))
 if data.get('decision') not in ('accept','reject'):raise ValueError('승인 또는 보류를 선택하세요.')
 for key in ('operator','note'):
  if not isinstance(data.get(key),str) or not data[key].strip() or len(data[key])>5000:raise ValueError('작업자 이름과 메모를 입력하세요.')
 checklist=data.get('checklist')
 if not isinstance(checklist,dict) or set(checklist)!=set(CHECKS) or any(type(v) is not bool for v in checklist.values()):raise ValueError('체크리스트 형식 오류입니다.')
 with (project/'.qc-review.lock').open('a') as lock:
  fcntl.flock(lock,fcntl.LOCK_EX)
  if data.get('revision')!=state(out)['revision']:raise ValueError('다른 검수 기록이 저장되었습니다. 새로고침 후 확인하세요.')
  if data['decision']=='accept':
   if not all(checklist.values()):raise ValueError('승인하려면 모든 항목을 확인하세요.')
  qc.review_report(out,data['decision'],data['note'].strip(),data['operator'].strip(),checklist)
  return state(out)

class Handler(SimpleHTTPRequestHandler):
 def __init__(self,*args,project,**kwargs):
  self.project=project
  super().__init__(*args,directory=str(project),**kwargs)
 def json_response(self,data,status=200):
  body=json.dumps(data,ensure_ascii=False).encode();self.send_response(status);self.send_header('Content-Type','application/json; charset=utf-8');self.send_header('Content-Length',str(len(body)));self.send_header('Cache-Control','no-store');self.end_headers();self.wfile.write(body)
 def trusted_host(self):
  return self.headers.get('Host') in {f'127.0.0.1:{self.server.server_port}',f'localhost:{self.server.server_port}'}
 def do_GET(self):
  if not self.trusted_host():return self.json_response({'error':'Invalid host'},403)
  path=urlsplit(self.path)
  try:
   if path.path=='/api/sam3':
    from sam_web import status
    return self.json_response({**status(self.project,parse_qs(path.query).get('episode',[''])[0]),'token':self.server.token})
   if path.path=='/api/sync':
    out,case=report_path(self.project,parse_qs(path.query).get('report',[''])[0])
    with (out/'.sync.lock').open('a') as lock:
     fcntl.flock(lock,fcntl.LOCK_EX)
     manifest=out/'sync_media/timeline.json'
     if not manifest.exists():
      here=Path(__file__).resolve().parent
      config=qc.load(here/'runtime.local.json') if (here/'runtime.local.json').exists() else {}
      python=config.get('qc_python',sys.executable)
      run=subprocess.run([python,str(here/'qc_sync.py'),str(case),str(out)],capture_output=True,text=True,timeout=300)
      if run.returncode:raise ValueError('동기 프레임 생성 실패: '+run.stderr[-1000:])
     return self.json_response(checked_timeline(case,manifest))
   if path.path=='/api/review':
    out,_=report_path(self.project,parse_qs(path.query).get('report',[''])[0]);return self.json_response({**state(out),'token':self.server.token})
   if path.path.endswith('/report.html'):
    out,case=report_path(self.project,unquote(path.path).removesuffix('/report.html'))
    prepare_media(case,out)
    body=page(qc.load(out/'provenance.json')['episode'],case,out,qc.load(out/'qc.json')).encode()
    self.send_response(200);self.send_header('Content-Type','text/html; charset=utf-8');self.send_header('Content-Length',str(len(body)));self.send_header('Cache-Control','no-store');self.end_headers();self.wfile.write(body);return
   resolved=Path(self.translate_path(path.path)).resolve()
   if not resolved.is_relative_to(self.project):return self.json_response({'error':'Forbidden'},403)
   super().do_GET()
  except (ValueError,OSError,KeyError,subprocess.SubprocessError) as e:self.json_response({'error':str(e)},400)
 def do_POST(self):
  origin=self.headers.get('Origin')
  if not self.trusted_host() or origin!=f'http://{self.headers.get("Host")}' or self.headers.get('X-QC-Token')!=self.server.token:return self.json_response({'error':'Invalid origin/token'},403)
  if self.path not in ('/api/review','/api/sam3'):return self.json_response({'error':'Not found'},404)
  try:
   size=int(self.headers.get('Content-Length','0'))
   if not 0<size<=30000:raise ValueError('요청 크기 오류')
   data=json.loads(self.rfile.read(size))
   if not isinstance(data,dict):raise ValueError('요청 형식 오류')
   if self.path=='/api/sam3':
    from sam_web import launch
    return self.json_response(launch(self.project,data))
   self.json_response(persist(self.project,data))
  except (ValueError,OSError,KeyError,subprocess.SubprocessError) as e:self.json_response({'error':str(e)},400)

def main():
 p=argparse.ArgumentParser(description='QC 화면과 승인/보류 저장 서버');p.add_argument('--project',type=Path,default=Path(__file__).resolve().parent.parent);p.add_argument('--port',type=int,default=8768);a=p.parse_args();project=a.project.expanduser().resolve()
 if not (project/'project.json').is_file():p.error('project.json이 있는 프로젝트를 지정하세요.')
 server=ThreadingHTTPServer(('127.0.0.1',a.port),functools.partial(Handler,project=project));server.token=secrets.token_urlsafe(32)
 print(f'QC 화면: http://127.0.0.1:{a.port}/overview.html',flush=True);server.serve_forever()
if __name__=='__main__':main()
