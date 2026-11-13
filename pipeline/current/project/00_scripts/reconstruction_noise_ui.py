"""GTK controls for auditable 3D candidate analysis; no NumPy in desktop Python."""
import json,subprocess,threading
from pathlib import Path
from gi.repository import Gtk,GLib

# Semantic group and detector are encoded together; overlap has its own colour.
CANDIDATE_COLORS={
 'object':{'sor':(240,85,255),'cluster':(120,245,70),'both':(255,235,70)},
 'hand':{'sor':(0,230,230),'cluster':(255,80,110),'both':(245,245,245)}}
def candidate_color(group,reason):
 return CANDIDATE_COLORS[group]['both' if reason&1 and reason&6 else 'sor' if reason&1 else 'cluster']

class NoiseControls:
 def noise_controls(self,box,case):
  self.case=Path(case) if case else None;self.noise=None;self.noise_busy=False;self.noise_widgets=[];self.group_previews={};self.group_decisions={}
  row=Gtk.Box(spacing=8);box.pack_start(row,False,False,0)
  self.noise_mode=Gtk.ComboBoxText()
  for name in ['원본','노이즈 후보 강조','제거 후 미리보기','제거 전 / 후 비교']:self.noise_mode.append_text(name)
  self.noise_mode.set_active(0);self.noise_mode.connect('changed',lambda *_:self.render());row.pack_start(self.noise_mode,False,False,0)
  self.noise_widgets.append(self.noise_mode)
  semantic=Gtk.Box(spacing=8);row.pack_start(semantic,False,False,0)
  semantic.pack_start(Gtk.Label(label='점 표시 대상'),False,False,0)
  self.group_visible={}
  for group,title in [('object','물체 표시 ●'),('hand','손 표시 ■')]:
   toggle=Gtk.CheckButton(label=title);toggle.set_active(True);toggle.connect('toggled',lambda *_:self.render());semantic.pack_start(toggle,False,False,0);self.group_visible[group]=toggle
  semantic.pack_start(Gtk.Label(label='표시는 각각 켜기/끄기 · 계산/적용도 물체·손 개별 처리'),False,False,0)
  self.settings_expander=Gtk.Expander(label='노이즈 설정 · 색상 범례 · 개별 적용 (Tab)');self.settings_expander.set_expanded(False);box.pack_start(self.settings_expander,False,False,0)
  settings_scroll=Gtk.ScrolledWindow();settings_scroll.set_policy(Gtk.PolicyType.AUTOMATIC,Gtk.PolicyType.AUTOMATIC);settings_scroll.set_min_content_height(260);settings_scroll.set_max_content_height(320);settings_scroll.set_propagate_natural_height(True)
  self.settings_expander.add(settings_scroll)
  panel=Gtk.Box(orientation=Gtk.Orientation.VERTICAL,spacing=5);settings_scroll.add(panel)
  box=panel
  for group,title in [('object','물체 ●'),('hand','손 ■')]:
   legend=Gtk.Label(xalign=0)
   parts=[]
   for reason,label in [('sor','SOR 후보'),('cluster','군집·고립 후보'),('both','SOR + 군집 중복')]:
    color='#%02x%02x%02x'%CANDIDATE_COLORS[group][reason]
    parts.append(f'<span background="{color}" foreground="#101820">  {title} · {label}  </span>')
   legend.set_markup('   '.join(parts));box.pack_start(legend,False,False,0)
  self.noise_fields={};self.apply_buttons={};self.reset_buttons={};self.group_status={}
  for group,title in [('object','물체'),('hand','손')]:
   controls=Gtk.Box(spacing=6);box.pack_start(controls,False,False,0);controls.pack_start(Gtk.Label(label=title),False,False,0)
   self.noise_fields[group]={}
   for label,key,value,low,high,step,digits in [('SOR 이웃','neighbors',31,3,200,1,0),('배수','std-ratio',8.0,.1,20,.1,1),('군집 반경(m)','cluster-radius',.02,.001,.2,.001,3),('최소 점','cluster-min-points',10,3,200,1,0)]:
    controls.pack_start(Gtk.Label(label=label),False,False,0);spin=Gtk.SpinButton.new_with_range(low,high,step);spin.set_digits(digits);spin.set_value(value);controls.pack_start(spin,False,False,0);self.noise_fields[group][key]=spin;self.noise_widgets.append(spin)
   for text,callback in [('후보 계산',self.noise_analyze),('제외 적용',self.noise_apply),('적용 해제',self.noise_reset)]:
    button=Gtk.Button(label=title+' '+text);button.connect('clicked',lambda _,g=group,fn=callback:fn(g));controls.pack_start(button,False,False,0);self.noise_widgets.append(button)
    if text=='제외 적용':self.apply_buttons[group]=button
    if text=='적용 해제':self.reset_buttons[group]=button
   status=Gtk.Label(label=title+' · 분석 전',xalign=0);box.pack_start(status,False,False,0);self.group_status[group]=status
  self.reviewer=Gtk.Entry();self.reviewer.set_placeholder_text('검수자 이름 · 선택한 대상에만 적용');box.pack_start(self.reviewer,False,False,0);self.noise_widgets.append(self.reviewer)
  self.noise_status=Gtk.Label(label='물체·손 설정 및 적용 기록을 따로 저장합니다. 후보는 정상 경계일 수도 있으므로 비교 후 적용하세요.',xalign=0);self.noise_status.set_line_wrap(True);box.pack_start(self.noise_status,False,False,0)
  self.noise_stats=Gtk.Label(xalign=0);self.noise_stats.set_line_wrap(True);box.pack_start(self.noise_stats,False,False,0)
  self.connect('delete-event',self.noise_close_guard)
  self.noise_enable()
  if self.case:GLib.idle_add(lambda:self.noise_run('verify'))
 def noise_close_guard(self,*_):
  if self.noise_busy:self.noise_status.set_text('분석/저장이 진행 중입니다. 완료 후 창을 닫아주세요.');return True
  return False
 def noise_enable(self):
  for widget in self.noise_widgets:widget.set_sensitive(bool(self.case) and not self.noise_busy)
  self.noise_mode.set_sensitive(bool(self.group_previews) and not self.noise_busy)
  for group in ('object','hand'):
   self.apply_buttons[group].set_sensitive(group in self.group_previews and not self.noise_busy)
   self.reset_buttons[group].set_sensitive(group in self.group_decisions and not self.noise_busy)
 def noise_analyze(self,group):
  args=[]
  for key,spin in self.noise_fields[group].items():args.extend(['--'+key,str(spin.get_value_as_int() if key in ('neighbors','cluster-min-points') else spin.get_value())])
  self.noise_run('analyze',args,group)
 def noise_apply(self,group):
  name=self.reviewer.get_text().strip()
  if not name:self.noise_status.set_text('검수자 이름을 입력한 뒤 적용하세요.');return
  if group not in self.group_previews:return
  saved=self.group_previews[group]['settings']
  if any(abs(spin.get_value()-saved[key.replace('-','_')])>1e-8 for key,spin in self.noise_fields[group].items()):
   self.noise_status.set_text('설정이 변경되었습니다. 해당 대상의 후보 계산을 먼저 실행하세요.');return
  self.noise_run('apply',['--reviewer',name,'--analysis-id',self.group_previews[group]['analysis_id']],group)
 def noise_reset(self,group):self.noise_run('reset',group=group)
 def noise_run(self,action,args=None,group='both'):
  if self.noise_busy or not self.case:return False
  self.playing=False;self.noise_busy=True;self.noise_enable();self.noise_status.set_text(('물체' if group=='object' else '손' if group=='hand' else '물체·손')+' · '+{'analyze':'후보 계산 중…','apply':'정제본 저장 중…','verify':'저장 기록 확인 중…','reset':'적용 해제 중…'}[action])
  def work():
   try:
    import pipeline
    root,cwd,env=pipeline.runtime();env['OMP_NUM_THREADS']='2';env['OPENBLAS_NUM_THREADS']='1'
    cmd=[str(root/'phystwin-env/bin/python'),str(Path(__file__).with_name('reconstruction_noise.py')),action,'--case',str(self.case),'--group',group,*(args or [])]
    result=subprocess.run(cmd,cwd=cwd,env=env,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True)
    if result.returncode:raise RuntimeError(result.stdout[-3000:] or '작업 실패')
    loaded={};decisions={}
    for g in (('object','hand') if group=='both' else (group,)):
     directory=self.case/'noise'/g
     if (directory/'preview.json').exists():loaded[g]=json.loads((directory/'preview.json').read_text())
     if (directory/'applied/decision.json').exists():decisions[g]=json.loads((directory/'applied/decision.json').read_text())
    GLib.idle_add(self.noise_done,action,group,loaded,decisions,None)
   except Exception as ex:GLib.idle_add(self.noise_done,action,group,{}, {},str(ex))
  threading.Thread(target=work,daemon=True).start();return False
 def noise_done(self,action,group,loaded,decisions,error):
  if self.closed:return False
  self.noise_busy=False
  targets=('object','hand') if group=='both' else (group,)
  for g in targets:
   self.group_previews.pop(g,None);self.group_decisions.pop(g,None)
  if error:self.noise_status.set_text('중단: '+error)
  else:
   self.group_previews.update(loaded);self.group_decisions.update(decisions)
   self.noise_status.set_text('선택한 대상만 처리했습니다. 다른 대상의 설정·적용 상태는 유지됩니다. 메시/final_data는 변경하지 않습니다.')
   for g,preview in loaded.items():
    for key,spin in self.noise_fields[g].items():spin.set_value(preview['settings'][key.replace('-','_')])
   if action=='analyze':self.noise_mode.set_active(1)
  # Unanalysed groups remain visible as original points, never fabricated candidates.
  if self.group_previews:
   self.noise={'points':{},'counts':{}}
   for g in ('object','hand'):
    if g in self.group_previews:
     self.noise['points'][g]=self.group_previews[g]['points'][g];self.noise['counts'][g]=self.group_previews[g]['counts'][g]
    else:self.noise['points'][g]=[[{'p':p,'id':-1,'reason':0} for p in frame] for frame in self.data['points'][g]]
   self.bounds()
  else:self.noise=None;self.noise_mode.set_active(0)
  for g,title in [('object','물체'),('hand','손')]:
   d=self.group_decisions.get(g);p=self.group_previews.get(g)
   text=title+' · '+('적용 완료 · '+d['reviewer']+' · '+d['applied_at'] if d else '후보 검수 대기' if p else '분석 전 · 기본값 31 / 8.0')
   if p:
    c=p['counts'][g];text+=f' · 후보 {c["candidates"]:,} / 유효 관측 {c["visible"]:,}'
    if c['candidates']>.2*max(1,c['visible']):text+=' · 주의: 후보 20% 초과'
   self.group_status[g].set_text(text)
  self.noise_stats.set_text('적용은 대상별 전체 프레임에 수행합니다. 원본 좌표·추적 ID는 보존됩니다.')
  self.noise_enable();self.render();return False
 def noise_points(self,variant):
  return [(p,color,radius) for p,color,radius,group,reason in self.noise_glyphs(variant)]
 def noise_glyphs(self,variant):
  rows=[]
  for group,color in [('object',(85,170,255)),('hand',(255,170,85))]:
   if not self.group_visible[group].get_active():continue
   full=self.display_records(group) if hasattr(self,'full_points') else None
   if self.noise or full is not None:
    source=full if full is not None else self.noise['points'][group][self.frame]
    for item in source:
     reason=item['reason']
     if variant==2 and reason:continue
     paint=color
     if variant==1 and reason:paint=candidate_color(group,reason)
     rows.append((item['p'],paint,4 if variant==1 and reason else 2,group,reason))
   else:rows.extend((p,color,2,group,0) for p in self.data['points'][group][self.frame])
  return rows
