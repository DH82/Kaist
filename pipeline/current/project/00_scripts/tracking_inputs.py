#!/usr/bin/env python3
"""Internal approved-mask input preparation used by 07_track.py."""
import json,os
from pathlib import Path
from mask_review import approved,root
from stage_refs import inputs,source,link
from sam_settings import save
from tracking_paths import episode_root

def prepare(project,ep):
 version=approved(project,ep);out=episode_root(project,ep)
 out.mkdir(parents=True,exist_ok=True)
 existing=out/'approval_source.json'
 if existing.exists() and json.loads(existing.read_text())['version']!=version.name:raise ValueError('이미 다른 승인본을 사용하는 추적 작업이 있습니다. 기존 작업을 보관한 후 진행하세요.')
 inputs(out,source(root(project,ep)));link(out/'mask',version/'mask')
 save(existing,{'version':version.name,'approval':os.path.relpath(version/'approval.json',out),'mask':os.path.relpath(version/'mask',out),'status':'approved_inputs_ready'})
 return out
