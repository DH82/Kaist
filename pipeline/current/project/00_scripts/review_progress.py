"""Persist operator-owned checks independently from mask edit/approval validity."""
import json
from resume_steps import snapshot
from sam_settings import save

def fingerprint(draft):
 return snapshot([draft/'mask', draft/'basis.json'],draft)

def load_progress(draft):
 path=draft.parent/'progress.json'
 if not path.exists():return None
 try:
  data=json.loads(path.read_text())
  if not data.get('modified') and data.get('files')!=fingerprint(draft):data['modified']=True
  if len(data.get('checked',[]))!=4 or any(type(v) is not bool for v in data['checked']):return None
  return data
 except (OSError,ValueError,KeyError):return None

def save_progress(draft,checked,operator,note,modified=False):
 path=draft.parent/'progress.json'
 if modified:
  try:files=json.loads(path.read_text()).get('files',{})
  except (OSError,ValueError):files={}
 else:files=fingerprint(draft)
 save(path,{'modified':bool(modified),'checked':list(checked),'operator':operator,'note':note,'files':files})
