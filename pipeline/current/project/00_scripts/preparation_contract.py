"""Strict input/code/settings and immutable prepared output contract, schema 2."""
import hashlib,json
from pathlib import Path
SETTINGS={'fps':30.,'sync_tol_ms':25.,'convention':'auto','allow_bad_extrinsics':False,'no_reorient':False,'trim_policy':'keep_all_synced_frames','trim_lo':.71,'trim_hi':1.39}

def sha(p):
 h=hashlib.sha256()
 with Path(p).open('rb') as f:
  for block in iter(lambda:f.read(1024*1024),b''):h.update(block)
 return h.hexdigest()

def contract(raw_fingerprint,scripts,source):
 files=[(scripts/n) for n in ['pipeline.py','raw_io.py','process_input_worker.py','preparation_contract.py','sync_mapping.py']]+[source/'data_process'/n for n in ['preprocess_rgbd.py','trim_exposure.py','sync_mapping.py']]
 if (scripts/'sync_mapping.py').read_bytes()!=(source/'data_process/sync_mapping.py').read_bytes():raise ValueError('QC/preprocess mapping code differs')
 return {'schema_version':2,'raw_fingerprint':raw_fingerprint,'settings':SETTINGS,'code':{('operator/' if f.parent==scripts else 'data_process/')+f.name:sha(f) for f in files}}

def outputs(case):
 records=[]
 for name in ['color','depth','metadata.json','calibrate.pkl','world_reorient_M.npy','source_frames.json','trim.json']:
  p=case/name
  if not p.exists():raise ValueError('Missing prepared product: '+str(p))
  files=sorted(p.rglob('*')) if p.is_dir() else [p]
  for f in files:
   if f.is_symlink():raise ValueError('Prepared product symlink: '+str(f))
   if f.is_file():records.append({'path':f.relative_to(case).as_posix(),'bytes':f.stat().st_size,'sha256':sha(f)})
 return records

def verify(case,expected):
 rec=json.loads((case/'preparation.json').read_text())
 if rec.get('contract')!=expected:raise ValueError('Prepared contract changed or legacy receipt. Preserve existing output and rerun with --new-attempt.')
 if rec.get('outputs')!=outputs(case):raise ValueError('Prepared output missing/changed. Preserve existing output and rerun with --new-attempt.')
 return rec
