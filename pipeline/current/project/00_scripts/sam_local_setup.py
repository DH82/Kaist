"""Native GTK operator setup. No HTTP server or browser required."""
import argparse,json,sys,os,datetime,getpass,threading,subprocess
from pathlib import Path
from sam_settings import review_page,save
from sam_web import status,episode

def approve_only(project,ep,prompt,scene,new=False):
 prepared=episode(project,ep);current=status(project,ep);settings=dict(current['settings'])
 if not prompt.strip() or len(prompt)>200:raise ValueError('물체 프롬프트를 1~200자로 입력하세요')
 if scene!='2d':raise ValueError('현재 손 ROI 후보는 2d만 지원합니다. 1d/3d 장면은 실행할 수 없습니다.')
 if current['locked'] and not new and (prompt.strip()!=settings.get('prompt') or scene!=settings.get('scene')):raise ValueError('재개 설정은 고정입니다. 변경은 새 실행에서 가능합니다.')
 settings.update(prompt=prompt.strip(),scene=scene);settings.setdefault('label','object')
 _,fp=review_page(prepared,settings);path=prepared/'sam3_settings.json';old=json.loads(path.read_text()) if path.exists() else {};history=old.get('history',[])
 if old:history.append({k:v for k,v in old.items() if k!='history'})
 save(path,{'approved':True,'settings':settings,'review_fingerprint':fp,'operator':getpass.getuser(),'confirmed_at':datetime.datetime.now().astimezone().isoformat(),'history':history})
 return settings

def main():
 parser=argparse.ArgumentParser();parser.add_argument('input',type=Path);parser.add_argument('--confirm-only',action='store_true',default=True);parser.add_argument('--new-attempt',action='store_true');args=parser.parse_args()
 prepared=args.input.expanduser().resolve();project=prepared.parent.parent;ep=prepared.name;episode(project,ep)
 import gi
 gi.require_version('Gtk','3.0')
 from gi.repository import Gtk,GdkPixbuf,GLib
 if not Gtk.init_check()[0]:raise ValueError('데스크톱 화면에 연결할 수 없습니다. 로그인한 PC의 터미널에서 실행하세요.')
 initial=status(project,ep);meta=json.loads((prepared/'metadata.json').read_text());frames=[0,int(meta['frame_num'])//2,int(meta['frame_num'])-1]
 window=Gtk.Window(title='WFM · QC 물체 프롬프트 — '+ep);window.set_default_size(1080,860);window.set_border_width(16)
 outer=Gtk.Box(orientation=Gtk.Orientation.VERTICAL,spacing=10);window.add(outer)
 title=Gtk.Label(label='QC · 대표 RGB 확인 → 물체 프롬프트 저장',xalign=0);outer.pack_start(title,False,False,0)
 timeline=Gtk.ComboBoxText()
 for label,f in zip(['처음','중간','마지막'],frames):timeline.append_text(f'{label} · frame {f}')
 timeline.set_active(1);outer.pack_start(timeline,False,False,0)
 scroll=Gtk.ScrolledWindow();scroll.set_policy(Gtk.PolicyType.AUTOMATIC,Gtk.PolicyType.AUTOMATIC);outer.pack_start(scroll,True,True,0)
 grid=Gtk.Grid(column_spacing=10,row_spacing=10);scroll.add(grid);images=[]
 for cam in range(int(meta.get('num_cameras',4))):
  box=Gtk.Box(orientation=Gtk.Orientation.VERTICAL,spacing=4);box.pack_start(Gtk.Label(label=f'Camera {cam+1}'),False,False,0);image=Gtk.Image();box.pack_start(image,True,True,0);grid.attach(box,cam%2,cam//2,1,1);images.append(image)
 def show_frames(*unused):
  f=frames[timeline.get_active()]
  for cam,image in enumerate(images):image.set_from_pixbuf(GdkPixbuf.Pixbuf.new_from_file_at_scale(str(prepared/f'color/{cam}/{f}.png'),480,270,True))
 timeline.connect('changed',show_frames);show_frames()
 row=Gtk.Box(spacing=12);outer.pack_start(row,False,False,0);row.pack_start(Gtk.Label(label='물체 프롬프트'),False,False,0)
 prompt=Gtk.Entry();prompt.set_text(initial['settings'].get('prompt',''));prompt.set_placeholder_text('예: blue cloth');row.pack_start(prompt,True,True,0)
 scene=Gtk.ComboBoxText()
 for key,label in [('1d','1d · 끈 — 현재 미지원'),('2d','2d · 천/평면 조작 (기본)'),('3d','3d · 입체 물체 — 현재 미지원')]:scene.append(key,label)
 scene.set_active_id(initial['settings'].get('scene','2d'));row.pack_start(scene,False,False,0)
 outer.pack_start(Gtk.Label(label='장면 유형은 기본값을 확인하면 됩니다. 실제 장면이 1d/3d라면 2d로 바꿔 우회하지 마세요.',xalign=0),False,False,0)
 fresh=Gtk.CheckButton(label='다음 새 실행용 설정 변경 (실제 재추론은 터미널 --new-attempt)');fresh.set_active(args.new_attempt);fresh.set_sensitive(True);outer.pack_start(fresh,False,False,0)
 notice=Gtk.Label(xalign=0);notice.set_line_wrap(True);outer.pack_start(notice,False,False,0)
 def locked(*unused):
  editable=not initial['locked'] or fresh.get_active();prompt.set_sensitive(editable);scene.set_sensitive(editable)
  if not editable:prompt.set_text(initial['settings'].get('prompt',''));scene.set_active_id(initial['settings'].get('scene','2d'));notice.set_text('저장된 설정으로 재개합니다. 설정 변경은 새 실행을 선택하세요.')
 fresh.connect('toggled',locked);locked()
 button=Gtk.Button(label='물체 프롬프트 저장 · 닫기 (추론하지 않음)');outer.pack_start(button,False,False,0)
 logbox=Gtk.TextView();logbox.set_editable(False);logbox.set_wrap_mode(Gtk.WrapMode.WORD_CHAR);logscroll=Gtk.ScrolledWindow();logscroll.set_size_request(-1,120);logscroll.add(logbox);outer.pack_start(logscroll,False,True,0)
 accepted=False;alive=True;busy=False
 def clicked(*unused):
  nonlocal accepted
  try:
   approve_only(project,ep,prompt.get_text(),scene.get_active_id(),fresh.get_active());accepted=True;window.destroy();return
  except Exception as ex:notice.set_text('중단: '+str(ex))
 button.connect('clicked',clicked)
 def apply(data):
  if not alive:return False
  job=data.get('job',{});active=job.get('status') in ('starting','running');button.set_sensitive(not active)
  if job:
   text='상태: '+job['status']
   if 'elapsed_seconds' in job:text+=f" · 이번 실행 {job['elapsed_seconds']}초"
   if job.get('error'):text+=' · '+job['error']
   complete=data.get('completion')
   if job['status']=='completed' and complete:text+=f" · 산출물 {complete['verified_stages']}/10 검증 · 검수 대기"
   notice.set_text(text)
  logbox.get_buffer().set_text(data.get('log','')[-8000:]);return False
 def poll():
  nonlocal busy
  if not alive:return False
  if busy:return True
  busy=True
  def work():
   nonlocal busy
   try:GLib.idle_add(apply,status(project,ep))
   except Exception:pass
   finally:busy=False
  threading.Thread(target=work,daemon=True).start();return True
 def closed(*unused):
  nonlocal alive
  alive=False;Gtk.main_quit()
 window.connect('destroy',closed);window.show_all()
 if not args.confirm_only:GLib.timeout_add_seconds(3,poll)
 Gtk.main();return 0 if accepted or not args.confirm_only else 2
if __name__=='__main__':
 try:raise SystemExit(main())
 except Exception as ex:print('로컬 설정 창 오류:',ex,file=sys.stderr);raise SystemExit(1)
