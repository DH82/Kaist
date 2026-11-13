"""Measure an explicit command. Does not report file existence as task success."""
import argparse, datetime, json, os, subprocess, time
from pathlib import Path
p=argparse.ArgumentParser(); p.add_argument('--root',required=True); p.add_argument('--label',required=True); p.add_argument('command',nargs=argparse.REMAINDER); a=p.parse_args()
cmd=a.command[1:] if a.command[:1]==['--'] else a.command
if not cmd: p.error('Provide command after --')
if not a.label.replace('-','').replace('_','').isalnum(): p.error('Simple label required')
r=Path(a.root).expanduser().resolve(); out=r/'benchmarks'/(datetime.datetime.now().strftime('%Y%m%d-%H%M%S')+'-'+a.label); out.mkdir(parents=True,exist_ok=False)
meta={'command':cmd,'started':datetime.datetime.now().isoformat(),'status':'running','human_review':'not_performed'}
status=out/'status.json'; status.write_text(json.dumps(meta,indent=2))
mon=None; start=time.monotonic(); rc=130
with (out/'gpu.csv').open('w') as gpu, (out/'output.log').open('w') as log:
    try:
        try: mon=subprocess.Popen(['nvidia-smi','--query-gpu=timestamp,index,name,utilization.gpu,memory.used,memory.total,power.draw','--format=csv','-l','1'],stdout=gpu,stderr=subprocess.STDOUT)
        except FileNotFoundError: meta['gpu_monitor']='unavailable'
        rc=subprocess.call(cmd,stdout=log,stderr=subprocess.STDOUT)
    finally:
        if mon: mon.terminate(); mon.wait()
        meta.update(returncode=rc,wall_seconds=time.monotonic()-start,status='command_succeeded_needs_output_validation' if rc==0 else 'failed_or_interrupted')
        status.write_text(json.dumps(meta,indent=2))
print(out); print(json.dumps(meta,indent=2)); raise SystemExit(rc)
