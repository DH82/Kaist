"""Operator-confirmed episode settings; a run's settings remain immutable."""
import json,hashlib,html,sys,subprocess,datetime,getpass
from pathlib import Path

def save(path,data):
 tmp=path.with_suffix('.tmp');tmp.write_text(json.dumps(data,ensure_ascii=False,indent=2));tmp.replace(path)
def review_page(prepared,settings):
 meta=json.loads((prepared/'metadata.json').read_text());n=int(meta['frame_num']);frames=sorted(set([0,n//2,n-1]));rows=[];h=hashlib.sha256()
 for name in ['metadata.json','preparation.json']:h.update((prepared/name).read_bytes())
 for c in range(int(meta.get('num_cameras',4))):
  cards=[]
  for f in frames:
   path=prepared/f'color/{c}/{f}.png'
   if not path.is_file():raise ValueError('Representative RGB missing: '+str(path))
   h.update(path.read_bytes());cards.append(f'<figure><a href="color/{c}/{f}.png"><img src="color/{c}/{f}.png"></a><figcaption>Camera {c+1} · frame {f}</figcaption></figure>')
  rows.append('<div class="grid">'+''.join(cards)+'</div>')
 text='''<!doctype html><meta charset="utf-8"><title>SAM3 실행 전 확인</title><style>body{font:16px/1.6 system-ui;max-width:1400px;margin:auto;padding:24px;background:#eef2f6;color:#17283c}.grid{display:grid;grid-template-columns:repeat(3,1fr);gap:12px}figure{margin:0}img{width:100%}section{background:white;padding:20px;border-radius:12px;margin:16px 0}</style><h1>SAM3 실행 전 · 대표 RGB 확인</h1>'''
 text+='<section><b>'+html.escape(prepared.name)+'</b><p>물체 프롬프트: '+html.escape(str(settings.get('prompt','미설정')))+' / 장면 유형: '+html.escape(str(settings.get('scene','미설정')))+'</p><p>물체와 장면을 확인한 다음, QC 설정 창에서 프롬프트를 저장하세요. SAM3는 터미널의 05_sam3.py로 실행합니다. 이미지는 클릭하면 원본 크기로 열립니다.</p><p>1d: 끈 · 2d: 천/평면 조작 · 3d: 입체 물체 조작. 현재 ROI 후보 실행은 2d만 지원합니다. 다른 유형을 2d로 바꿔 우회하지 마세요.</p><p>물체 프롬프트에는 영어 물체명과 필요한 색상만 입력하세요. 예: blue cloth. 손/전완 프롬프트는 별도 고정 설정입니다.</p></section>'
 form=(Path(__file__).parent/'sam_setup_form.html').read_text()
 (prepared/'sam3_setup.html').write_text(text+''.join(rows)+form)
 return prepared/'sam3_setup.html',h.hexdigest()

def choose(project,ep,new=False):
 prepared=project/'03_prepared'/ep;dest=__import__('mask_paths').episode_root(project,ep)
 config=prepared/'sam3_settings.json';contract=dest/'_resume/contract.json'
 defaults=json.loads((project/'episodes.json').read_text()).get(ep,{}) if (project/'episodes.json').exists() else {}
 record=json.loads(config.read_text()) if config.exists() else {}
 locked=json.loads(contract.read_text())['settings'] if contract.exists() and not new else None
 settings=dict(locked if locked is not None else record.get('settings',defaults))
 page,fp=review_page(prepared,settings)
 if record.get('approved') and record.get('review_fingerprint')==fp and record.get('settings')==settings :
  print(f"[설정] 저장된 승인 사용: {settings['prompt']} / {settings['scene']}")
  return settings
 raise ValueError('QC 단계에서 물체 프롬프트를 먼저 저장하세요. 02_capture_qc.py 또는 sam_local_setup.py를 실행하세요. SAM3는 시작하지 않습니다.')
