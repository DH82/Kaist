"""Laptop -> mounted NAS. Publishes manifest first, completion marker last; never overwrites an episode."""
import argparse,hashlib,json,shutil
from pathlib import Path
from datetime import datetime,timezone
p=argparse.ArgumentParser();p.add_argument('source',type=Path);p.add_argument('--nas-raw',type=Path,required=True);p.add_argument('--calibration-file',required=True);p.add_argument('--calibration-id',required=True);p.add_argument('--capture-date',required=True);p.add_argument('--uploader',required=True);a=p.parse_args()
src=a.source.resolve();cal=Path(a.calibration_file)
import re
if not re.fullmatch(r'ep_[A-Za-z0-9_]+',src.name):p.error('source folder must be ep_*')
if not a.nas_raw.is_dir():p.error('NAS RAW folder must already exist and be mounted')
if cal.is_absolute() or '..' in cal.parts or not (src/cal).is_file():p.error('calibration file must be inside episode')
files={}
for f in sorted(src.rglob('*')):
 if f.is_symlink():p.error('symlinks are not supported')
 if f.is_file():
  name=f.relative_to(src).as_posix()
  if name in ('upload_manifest.json','upload_complete.json'):p.error('reserved filename in source')
  h=hashlib.sha256()
  with f.open('rb') as stream:
   for chunk in iter(lambda:stream.read(1024*1024),b''):h.update(chunk)
  files[name]={'size':f.stat().st_size,'sha256':h.hexdigest()}
manifest={'episode':src.name,'capture_date':a.capture_date,'uploader':a.uploader,'calibration_id':a.calibration_id,'calibration_file':cal.as_posix(),'files':files}
dest=a.nas_raw/src.name;dest.mkdir() # fail on collision, preserve existing data
(dest/'upload_manifest.json').write_text(json.dumps(manifest,ensure_ascii=False))
for n,name in enumerate(files,1):
 target=dest/name;target.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(src/name,target);print(f'{n}/{len(files)} {name}',flush=True)
(dest/'upload_complete.json').write_text(json.dumps({'finished_at':datetime.now(timezone.utc).isoformat()}))
print('전송 완료. 관리자 UI에서 입고 검증 후 작업 실행 화면에서 선택하세요.')
