"""Point-count controls and bounded binary frame cache; no NumPy dependency."""
import json,struct,threading,subprocess
from pathlib import Path
from collections import OrderedDict
from gi.repository import Gtk,GLib
class PointDisplay:
 def point_controls(self,box):
  self.full_points=None;self.point_frames=OrderedDict();self.point_limits={};self.point_all={}
  row=Gtk.Box(spacing=6);box.pack_start(row,False,False,0)
  for group,title in [('object','물체'),('hand','손')]:
   row.pack_start(Gtk.Label(label=title+' 정상점 최대'),False,False,0)
   spin=Gtk.SpinButton.new_with_range(100,1000000,100);spin.set_value(1500);spin.set_sensitive(False);spin.connect('value-changed',lambda *_:self.render());row.pack_start(spin,False,False,0);self.point_limits[group]=spin
   toggle=Gtk.CheckButton(label='전체 유효점');toggle.set_sensitive(False);toggle.connect('toggled',self.point_toggle);row.pack_start(toggle,False,False,0);self.point_all[group]=toggle
  row.pack_start(Gtk.Label(label='후보는 제한과 관계없이 모두 표시'),False,False,0)
  self.point_count=Gtk.Label(label='전체 포인트 캐시 준비 중…',xalign=0);self.point_count.set_line_wrap(True);box.pack_start(self.point_count,False,False,0)
  if self.case:GLib.idle_add(self.start_point_cache)
  else:self.point_count.set_text('미리보기 샘플 · 전체 데이터 경로 없음')
 def point_toggle(self,*_):
  for g in self.point_limits:self.point_limits[g].set_sensitive(self.full_points is not None and not self.point_all[g].get_active())
  self.render()
 def start_point_cache(self):
  def work():
   try:
    import pipeline
    root,cwd,env=pipeline.runtime();env['OMP_NUM_THREADS']='2'
    r=subprocess.run([str(root/'phystwin-env/bin/python'),str(Path(__file__).with_name('point_display_cache.py')),str(self.case)],cwd=cwd,env=env,capture_output=True,text=True)
    if r.returncode:raise ValueError(r.stderr[-1500:] or r.stdout[-1500:])
    data=json.loads((self.case/'review/point_cache/manifest.json').read_text());GLib.idle_add(self.point_ready,data,None)
   except Exception as e:GLib.idle_add(self.point_ready,None,str(e))
  threading.Thread(target=work,daemon=True).start();return False
 def point_ready(self,data,error):
  if self.closed:return False
  if error:self.point_count.set_text('전체 점 로딩 실패: '+error);return False
  self.full_points=data
  for toggle in self.point_all.values():toggle.set_sensitive(True)
  self.point_toggle();self.bounds();self.render();return False
 def point_records(self,group):
  key=(group,self.frame)
  if key not in self.point_frames:
   path=self.case/'review/point_cache'/f'{group}_{self.frame}.bin'
   self.point_frames[key]=[(i,[x,y,z]) for i,x,y,z in struct.iter_unpack('<Ifff',path.read_bytes())]
   while len(self.point_frames)>8:self.point_frames.popitem(last=False)
  self.point_frames.move_to_end(key);return self.point_frames[key]
 def display_records(self,group):
  if not self.full_points:return None
  reasons={item['id']:item['reason'] for item in self.noise['points'][group][self.frame] if item['reason']} if self.noise else {}
  records=self.point_records(group);good=[(i,p) for i,p in records if i not in reasons];bad=[(i,p) for i,p in records if i in reasons]
  if not self.point_all[group].get_active():
   limit=self.point_limits[group].get_value_as_int()
   if len(good)>limit:good=[good[i*len(good)//limit] for i in range(limit)]
  return [{'id':i,'p':p,'reason':reasons.get(i,0)} for i,p in good+bad]
 def update_point_counts(self,variant):
  if not self.full_points:return
  parts=[]
  for group,title in [('object','물체'),('hand','손')]:
   original=self.full_points['counts'][group][self.frame];rows=self.display_records(group);removed=sum(bool(p['reason']) for p in rows);before=len(rows) if self.group_visible[group].get_active() else 0;after=before-removed if before else 0
   display=f'{before:,} → {after:,}' if variant==3 else f'{after if variant==2 else before:,}'
   parts.append(f'{title}: 원본 유효 {original:,} / 후보 제외 후 {original-removed:,} / 표시 {display}')
  self.point_count.set_text(' | '.join(parts)+' (현재 프레임 · 가림/화면 밖 점 포함)')
