"""Native GTK tracking review. Temporary decoded previews; no browser/server."""
import json,subprocess,tempfile,threading,shutil
from collections import OrderedDict
from pathlib import Path
import gi
gi.require_version('Gtk','3.0')
from gi.repository import Gtk,Gdk,GdkPixbuf,GLib
import tracking_review as review
from mask_review import lock
from tracking_paths import resolve_input

class Window(Gtk.Window):
 def __init__(self,case):
  super().__init__(title='WFM · 추적 검수 · '+case.name)
  self.project,case=resolve_input(case)
  self.case=case;self.closed=False;self.ready=False;self.playing=False;self.frame=0;self.count=0;self.cache=OrderedDict();self.process=None;self.temp=tempfile.TemporaryDirectory(prefix='wfm-tracking-review-');self.folder=Path(self.temp.name)
  with lock(self.project):self.record=review.status(case)
  if not self.record['ready']:self.temp.cleanup();raise ValueError(self.record.get('reason','추적 완료 후 검수하세요.'))
  self.qc=json.loads((case/'qc/report.json').read_text()) if (case/'qc/report.json').exists() else None
  self.review_end=None
  self.token=self.record['token'];self.set_default_size(1440,900);self.maximize();self.set_border_width(8)
  box=Gtk.Box(orientation=Gtk.Orientation.VERTICAL,spacing=8);self.add(box)
  self.status=Gtk.Label(xalign=0);self.status.set_line_wrap(True);box.pack_start(self.status,False,False,0);self.show_status(self.record)
  hint=Gtk.Label(label='초록: 보이는 점 · 빨강: 가림 예측 (불량을 뜻하지 않음) | 표면 이탈·미끄러짐·가림 후 잘못된 재연결을 확인하세요.\n문제가 있으면 대상 / 카메라 / 프레임 구간을 기록하고 보류합니다. 이 화면은 좌표 편집기가 아닙니다.',xalign=0);hint.set_line_wrap(True);box.pack_start(hint,False,False,0)
  panes=Gtk.Box(spacing=8);box.pack_start(panes,True,True,0);self.areas=[];self.holders=[];self.checks=[]
  for group,title in [('cotracker','물체 · 4캠'),('cotracker_hand','손 · 4캠')]:
   panel=Gtk.Box(orientation=Gtk.Orientation.VERTICAL,spacing=4);panes.pack_start(panel,True,True,0);panel.pack_start(Gtk.Label(label=title),False,False,0)
   holder=Gtk.ScrolledWindow();holder.set_policy(Gtk.PolicyType.NEVER,Gtk.PolicyType.NEVER);holder.set_size_request(200,150);area=Gtk.Image();holder.add(area);holder.connect('size-allocate',lambda *_:self.refresh());panel.pack_start(holder,True,True,0);self.areas.append(area);self.holders.append(holder)
   row=Gtk.Box(spacing=8);panel.pack_start(row,False,False,0)
   for c in range(4):
    b=Gtk.CheckButton(label=f'C{c+1} 검수 완료');b.set_active(bool((self.record.get('checked') or [False]*8)[len(self.checks)]) if not self.qc and self.record['status']!='needs_review' else False);b.connect('toggled',self.buttons);self.checks.append(b);row.pack_start(b,False,False,0)
  if self.qc is not None:
   for check in self.checks:check.set_no_show_all(True);check.hide()
   self.checks=[];events=self.qc['events'];self.events=events
   candidates=Gtk.Box(orientation=Gtk.Orientation.VERTICAL,spacing=3)
   sc=Gtk.ScrolledWindow();sc.set_size_request(-1,120);sc.add(candidates);box.pack_start(sc,False,False,0)
   reasons={'outside_mask':'승인 마스크 이탈','outside_image':'화면 밖 이탈','jump':'급격한 이동','low_visibility':'가시성 부족','low_coverage':'추적점 부족'}
   if not events:candidates.pack_start(Gtk.Label(label='자동 QC 이상 후보 없음 · 전체 영상은 선택적으로 확인할 수 있습니다.',xalign=0),False,False,0)
   for i,event in enumerate(events):
    row=Gtk.Box(spacing=8);candidates.pack_start(row,False,False,0)
    text=f"{'물체' if event['group']=='cotracker' else '손'} C{event['camera']} · 프레임 {event['start_frame']}–{event['end_frame']} · {reasons[event['reason']]}"
    jump=Gtk.Button(label=text);jump.connect('clicked',lambda _,e=event:self.goto_event(e));row.pack_start(jump,False,False,0)
    check=Gtk.CheckButton(label='이 구간 확인');saved=self.record.get('checked',[])
    check.set_active(bool(saved[i]) if i<len(saved) and self.record['status']!='needs_review' else False);check.connect('toggled',self.buttons);self.checks.append(check);row.pack_start(check,False,False,0)
   hint.set_text('자동 QC 이상 후보만 확인하세요. 후보 버튼을 누르면 해당 구간으로 이동합니다. 확인 후 승인하려면 허용 사유를 기록하세요.\n빨간 가림 예측점 자체는 불량이 아닙니다. 구조 오류는 사람 승인으로 통과시킬 수 없습니다.')
  row=Gtk.Box(spacing=8);box.pack_start(row,False,False,0)
  self.play=Gtk.Button(label='▶ 재생 / 일시정지 (Space)');self.play.connect('clicked',self.toggle);row.pack_start(self.play,False,False,0)
  for title,delta in [('◀ 이전',-1),('다음 ▶',1)]:
   b=Gtk.Button(label=title);b.connect('clicked',lambda _,d=delta:self.move(d));row.pack_start(b,False,False,0)
  b=Gtk.Button(label='현재 프레임 문제 기록');b.connect('clicked',self.issue);row.pack_start(b,False,False,0)
  self.position=Gtk.Label(label='영상 준비 중…');row.pack_start(self.position,False,False,0)
  self.slider=Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL,0,1,1);self.slider.set_draw_value(False);self.slider.connect('value-changed',self.seek);box.pack_start(self.slider,False,False,0)
  row=Gtk.Box(spacing=8);box.pack_start(row,False,False,0);row.pack_start(Gtk.Label(label='검수자'),False,False,0);self.name=Gtk.Entry();self.name.set_text(self.record.get('reviewer',''));self.name.connect('changed',self.buttons);row.pack_start(self.name,False,False,0)
  self.approve=Gtk.Button(label='✓ 승인');self.hold=Gtk.Button(label='보류');self.approve.connect('clicked',lambda _:self.decide('approved'));self.hold.connect('clicked',lambda _:self.decide('held'));row.pack_start(self.approve,False,False,0);row.pack_start(self.hold,False,False,0)
  row.pack_start(Gtk.Label(label='검수자 필수 · 보류 시 사유 필수'),False,False,0)
  scroll=Gtk.ScrolledWindow();scroll.set_size_request(-1,90);self.note=Gtk.TextView();self.note.set_wrap_mode(Gtk.WrapMode.WORD_CHAR);self.note.get_buffer().set_text(self.record.get('note',''));self.note.get_buffer().connect('changed',self.buttons);scroll.add(self.note);box.pack_start(scroll,False,False,0)
  self.message=Gtk.Label(label='로컬 영상 준비 중… 원본 결과는 변경하지 않습니다.',xalign=0);self.message.set_line_wrap(True);box.pack_start(self.message,False,False,0)
  self.connect('key-press-event',self.key);self.connect('destroy',self.close);self.buttons();self.show_all()
  self.worker=threading.Thread(target=self.decode,daemon=True);self.worker.start();self.timer=GLib.timeout_add(67,self.tick)
 def show_status(self,r):
  label={'approved':'✓ 승인 완료','held':'보류','pending':'검수 대기','needs_review':'재검수 필요'}[r['status']]
  self.status.set_text(f"{self.case.name} · {label}"+(f" · {'자동 QC' if r.get('approval_type')=='automatic' else '검수자'}: {r['reviewer']} · 시각: {r['decided_at']}" if r.get('reviewer') else ''))
 def text(self):
  b=self.note.get_buffer();return b.get_text(b.get_start_iter(),b.get_end_iter(),True)
 def buttons(self,*_):
  if not hasattr(self,'hold'):return
  valid=self.ready and bool(self.name.get_text().strip())
  self.approve.set_sensitive(valid and all(c.get_active() for c in self.checks) and (not self.qc or not self.qc['events'] or bool(self.text().strip())));self.hold.set_sensitive(valid and bool(self.text().strip()))
 def decode(self):
  try:
   for group in ('cotracker','cotracker_hand'):
    if self.closed:return
    folder=self.folder/group;folder.mkdir()
    with (self.folder/(group+'.log')).open('w') as log:
     proc=subprocess.Popen(['ffmpeg','-nostdin','-v','error','-i',str(self.case/'review'/f'{group}_review.mp4'),'-vf','scale=960:-2','-vsync','0','-q:v','3',str(folder/'%06d.jpg')],stdout=log,stderr=log);self.process=proc
     if self.closed:proc.terminate()
     if proc.wait():raise ValueError('검수 영상 디코딩 실패: '+group)
   counts=[len(list((self.folder/g).glob('*.jpg'))) for g in ('cotracker','cotracker_hand')]
   if not counts[0] or counts[0]!=counts[1]:raise ValueError('손·물체 검수 영상 프레임 수가 다릅니다.')
   if not self.closed:GLib.idle_add(self.loaded,counts[0])
  except Exception as ex:
   if not self.closed:GLib.idle_add(self.failed,str(ex))
 def failed(self,message):
  if not self.closed:self.message.set_text(message)
  return False
 def loaded(self,count):
  if self.closed:return False
  self.count=count;self.ready=True;self.slider.set_range(0,max(1,count-1));self.buttons();self.refresh();self.message.set_text('← →: 프레임 이동 · Space: 재생/정지 · 두 영상은 같은 프레임으로 표시됩니다.');return False
 def refresh(self):
  if not self.ready:return
  self.position.set_text(f'프레임 {self.frame} / {self.count-1} · 검수용 15fps')
  for index,group in enumerate(('cotracker','cotracker_hand')):self.draw(self.areas[index],self.holders[index],group)
 def draw(self,area,holder,group):
  if not self.ready:return
  key=(group,self.frame)
  if key not in self.cache:self.cache[key]=GdkPixbuf.Pixbuf.new_from_file(str(self.folder/group/f'{self.frame+1:06d}.jpg'))
  image=self.cache[key];self.cache.move_to_end(key)
  while len(self.cache)>24:self.cache.popitem(last=False)
  w=max(1,holder.get_allocated_width()-4);h=max(1,holder.get_allocated_height()-4);scale=min(w/image.get_width(),h/image.get_height())
  size=(max(1,int(image.get_width()*scale)),max(1,int(image.get_height()*scale)))
  signature=(key,size)
  if getattr(area,'last_signature',None)==signature:return
  area.last_signature=signature;area.set_from_pixbuf(image.scale_simple(*size,GdkPixbuf.InterpType.BILINEAR))
 def seek(self,*_):
  if self.ready:self.frame=min(self.count-1,int(self.slider.get_value()));self.refresh()
 def move(self,delta):
  if self.ready:self.playing=False;self.slider.set_value(max(0,min(self.count-1,self.frame+delta)))
 def toggle(self,*_):
  if self.ready:
   if self.frame==self.count-1:self.slider.set_value(0)
   self.playing=not self.playing
 def tick(self):
  if self.closed:return False
  if self.playing:
   if self.frame>=(self.review_end if self.review_end is not None else self.count-1):self.playing=False
   else:self.slider.set_value(self.frame+1)
  return True
 def goto_event(self,event):
  if self.ready:
   self.playing=False;self.review_end=min(self.count-1,event['end_frame']+3);self.slider.set_value(max(0,event['start_frame']-3));self.message.set_text(f"{event['group']} C{event['camera']} · {event['reason']} · 최대 지표 {event['peak']:.3f} / 기준 {event['threshold']:.3f}")
 def issue(self,*_):
  if not self.ready:return
  self.playing=False;b=self.note.get_buffer();b.insert(b.get_end_iter(),f'\n프레임 {self.frame} / 대상·카메라: / 증상: ');self.note.grab_focus()
 def key(self,_,event):
  if isinstance(self.get_focus(),(Gtk.Entry,Gtk.TextView)):return False
  key=Gdk.keyval_name(event.keyval)
  if key in ('Left','Right'):self.move(-1 if key=='Left' else 1);return True
  if key=='space':self.toggle();return True
  return False
 def decide(self,decision):
  self.playing=False
  try:
   with lock(self.project):r=review.decide(self.case,decision,self.name.get_text(),[c.get_active() for c in self.checks],self.text(),self.token)
   self.case=review.resolve(self.case)
   self.show_status(r)
   if decision=='approved':
    with lock(self.project):out=review.prepare_reconstruction(self.case)
    self.message.set_text('승인 저장 · 다음 단계 입력 준비: '+str(out))
   else:self.message.set_text('보류 기록 저장 완료 · 다음 단계 차단')
  except Exception as ex:self.message.set_text('저장 실패: '+str(ex))
 def close(self,*_):
  self.closed=True;self.playing=False
  if hasattr(self,'timer'):GLib.source_remove(self.timer)
  if self.process and self.process.poll() is None:
   self.process.terminate()
   try:self.process.wait(timeout=3)
   except subprocess.TimeoutExpired:self.process.kill();self.process.wait()
  if hasattr(self,'worker'):self.worker.join(timeout=5)
  self.temp.cleanup()
  if Gtk.main_level():Gtk.main_quit()

def run(case):
 if not Gtk.init_check()[0]:raise ValueError('데스크톱 터미널에서 실행하세요 (DISPLAY 필요).')
 if not shutil.which('ffmpeg'):raise ValueError('ffmpeg가 필요합니다.')
 Window(case);Gtk.main()
