import sys,time,json,os,traceback,datetime
from pathlib import Path
from sam_settings import save
from pipeline import sam3
project=Path(sys.argv[1]);ep=sys.argv[2];fd=int(sys.argv[3]);new=bool(int(sys.argv[4]));jobid=sys.argv[5];path=project/'03_prepared'/ep/'sam3_job.json'
# Wait for the launcher to persist the PID before publishing final status.
for _ in range(100):
 job=json.loads(path.read_text())
 if job.get('status')=='running':break
 time.sleep(.05)
start=time.monotonic();code=0
try:sam3(project,ep,new);job.update(status='completed')
except BaseException as ex:code=1;traceback.print_exc();job.update(status='failed',error=str(ex))
finally:
 job.update(elapsed_seconds=round(time.monotonic()-start,1),finished_at=datetime.datetime.now().astimezone().isoformat());save(path,job);os.close(fd)
raise SystemExit(code)
