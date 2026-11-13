"""Pure hashing contract shared by tracking producers and downstream consumers.
No torch import, CUDA query, inference, or output mutation.
"""
import os
from pathlib import Path
from resume_steps import snapshot,digest

def make_contract(prepared,approval,script,repo,checkpoint,scripts):
 return {'approval_version':approval.name,'approval_sha256':digest(approval/'approval.json'),
         'input':snapshot([prepared/'metadata.json',prepared/'color',prepared/'source_frames.json'],prepared),
         'code':{'tracking_contract.py':digest(scripts/'tracking_contract.py'),'tracking_review.py':digest(scripts/'tracking_review.py'),'07_track.py':digest(scripts/'07_track.py'),'stage_refs.py':digest(scripts/'stage_refs.py'),'resume_steps.py':digest(scripts/'resume_steps.py'),'tracking_inputs.py':digest(scripts/'tracking_inputs.py'),'track_object.py':digest(script),'cotracker_source':snapshot(list(repo.rglob('*.py')),repo),'tracking_worker.py':digest(scripts/'tracking_worker.py'),'tracking_preview.py':digest(scripts/'tracking_preview.py')},
         'checkpoint_sha256':digest(checkpoint),'settings':{'algorithm':'coverage_gap','objects':[0,1],'cameras':[0,1,2,3],'seed_spacing':3.,'max_pts_per_seed':4000,'cov_dilate':7,'gap_min_area':600,'cooldown':4,'min_track_frames':8}}

def checkpoint_from_env(env):
 # Same default resolution as torch.hub.get_dir() in the fresh tracking_worker process.
 cache=env.get('TORCH_HOME') or str(Path(env.get('XDG_CACHE_HOME') or str(Path.home()/'.cache'))/'torch')
 return Path(os.path.expandvars(os.path.expanduser(cache)))/'hub/checkpoints/scaled_online.pth'

def current_contract(prepared,approval,scripts):
 import pipeline
 _,cwd,env=pipeline.runtime()
 repo=Path(env.get('KP_COTRACKER_REPO',''))
 if not (repo/'hubconf.py').is_file():raise ValueError('Current CoTracker repository missing')
 return make_contract(prepared,approval,cwd/'data_process/track_object.py',repo,checkpoint_from_env(env),scripts)
