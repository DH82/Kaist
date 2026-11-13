#!/usr/bin/env python3
"""Native brush review of a separate draft. No inference or automatic approval."""
import argparse,json,sys,uuid
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo
from PIL import Image,ImageDraw
import mask_review as review
from review_cache import ReviewImageCache
from review_progress import load_progress,save_progress
from stage_refs import source
from brush_visual import preview
from mask_noise import candidates,remove_candidates,component_at,source_point,hole_candidates,fill_candidates

def main():
 ap=argparse.ArgumentParser();ap.add_argument('input',type=Path);a=ap.parse_args();project,d=review.resolve_input(a.input);ep=d.name
 import gi
 gi.require_version('Gtk','3.0')
 from gi.repository import Gtk,Gdk,GdkPixbuf,GLib,Pango
 if not Gtk.init_check()[0]:raise ValueError('데스크톱에서 실행하세요')
 with review.lock(project):
  draft=review.prepare_draft(project,ep);prep=source(d);meta=json.loads((prep/'metadata.json').read_text());w,h=meta['WH'];n=meta['frame_num'];scale=min(1000/w,560/h)
  window=Gtk.Window(title='WFM · 마스크 검수 / 수정 · '+ep);window.set_default_size(1440,900);window.set_border_width(6);window.maximize()
  box=Gtk.Box(orientation=Gtk.Orientation.VERTICAL,spacing=8);window.add(box)
  box.pack_start(Gtk.Label(label='추론 원본 보존 | 수정은 초안에 자동 저장 | 4캠 확인 후 승인본 생성',xalign=0),False,False,0)
  row=Gtk.Box(spacing=8);box.pack_start(row,False,False,0)
  camera=Gtk.ComboBoxText()
  for c in range(4):camera.append_text(f'Camera {c+1}')
  camera.set_tooltip_text('숫자 1–4: 카메라 전환 · 프레임/배율/위치 유지');camera.set_active(0);row.pack_start(camera,False,False,0)
  obj=Gtk.ComboBoxText();obj.append_text('물체 (초록)');obj.append_text('손 (분홍)');obj.set_tooltip_text('H: 손 · B: 물체 · 프레임/배율/위치 유지');obj.set_active(1);row.pack_start(obj,False,False,0)
  mode=Gtk.ComboBoxText()
  for text in ['영역 선택 / 보기','추가 브러시','지우기 브러시','사각형 구역 지정']:mode.append_text(text)
  mode.set_active(0);row.pack_start(mode,False,False,0)
  radius=Gtk.SpinButton.new_with_range(1,80,1);radius.set_value(3);row.pack_start(Gtk.Label(label='브러시 반경(px)'),False,False,0);row.pack_start(radius,False,False,0)
  size_label=Gtk.Label(label='지름 6px · 원본 기준');row.pack_start(size_label,False,False,0)
  noise_row=Gtk.Box(spacing=8);box.pack_start(noise_row,False,False,0)
  show_noise=Gtk.CheckButton(label='작은 영역 후보 표시 (노랑)');show_noise.set_active(True);noise_row.pack_start(show_noise,False,False,0)
  noise_row.pack_start(Gtk.Label(label='면적 ≤'),False,False,0)
  threshold=Gtk.SpinButton.new_with_range(0,w*h,1);threshold.set_value(50);noise_row.pack_start(threshold,False,False,0)
  noise_row.pack_start(Gtk.Label(label='원본 픽셀 · 0=끄기'),False,False,0)
  remove_noise=Gtk.Button(label='표시된 후보 제거 · 현재 프레임');noise_row.pack_start(remove_noise,False,False,0)
  remove_noise.set_label('표시된 후보 제거 · 현재 프레임 (N)')
  remove_noise.set_tooltip_text('노란 후보 전체 제거 · 현재 프레임만 · N · Ctrl+Z 복구')
  remove_noise.get_style_context().add_class('noise-remove')
  button_css=Gtk.CssProvider()
  button_css.load_from_data(b'button:not(:disabled) { background-image: none; background-color: #2563eb; color: #ffffff; border-color: #1d4ed8; } button:disabled { background-image: none; background-color: #e5e7eb; color: #6b7280; border-color: #d1d5db; } button.noise-remove:not(:disabled) { background-image: none; background-color: #b45309; color: #ffffff; border-color: #92400e; font-weight: bold; } button.noise-remove:not(:disabled):hover { background-color: #92400e; } button.noise-remove:disabled { background-image: none; background-color: #e5e7eb; color: #6b7280; border-color: #d1d5db; }')
  remove_noise.get_style_context().add_provider(button_css,Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
  select_row=Gtk.Box(spacing=8);box.pack_start(select_row,False,False,0)
  remove_selected=Gtk.Button(label='선택한 영역만 제거');select_row.pack_start(remove_selected,False,False,0)
  clear_selected=Gtk.Button(label='선택 해제');select_row.pack_start(clear_selected,False,False,0)
  zoom_label=Gtk.Label();select_row.pack_start(zoom_label,False,False,0)
  reset_zoom=Gtk.Button(label='화면 맞춤');select_row.pack_start(reset_zoom,False,False,0)
  batch_row=Gtk.Box(spacing=8);box.pack_start(batch_row,False,False,0)
  batch_row.pack_start(Gtk.Label(label='같은 위치 일괄 제거 · 프레임'),False,False,0)
  range_start=Gtk.SpinButton.new_with_range(0,max(1,n-1),1);range_end=Gtk.SpinButton.new_with_range(0,max(1,n-1),1);range_end.set_value(n-1)
  batch_row.pack_start(range_start,False,False,0);batch_row.pack_start(Gtk.Label(label='~'),False,False,0);batch_row.pack_start(range_end,False,False,0)
  batch_remove=Gtk.Button(label='범위 확인 후 제거');batch_row.pack_start(batch_remove,False,False,0)
  hole_row=Gtk.Box(spacing=8);box.pack_start(hole_row,False,False,0)
  show_holes=Gtk.CheckButton(label='내부 구멍 후보 표시 (주황)');show_holes.set_active(True);hole_row.pack_start(show_holes,False,False,0)
  hole_row.pack_start(Gtk.Label(label='면적 ≤'),False,False,0)
  hole_limit=Gtk.SpinButton.new_with_range(0,w*h,1);hole_limit.set_value(50);hole_row.pack_start(hole_limit,False,False,0)
  hole_row.pack_start(Gtk.Label(label='원본 픽셀'),False,False,0)
  fill_selected=Gtk.Button(label='선택 구멍 채우기');hole_row.pack_start(fill_selected,False,False,0)
  fill_all=Gtk.Button(label='후보 구멍 전체 채우기 · 현재 프레임 (M)');hole_row.pack_start(fill_all,False,False,0)
  hole_info=Gtk.Label(xalign=0);box.pack_start(hole_info,False,False,0)
  noise_info=Gtk.Label(xalign=0);box.pack_start(noise_info,False,False,0)
  hint=Gtk.Label(label='영역 선택: 마스크 클릭 · 사각형 구역 지정: 드래그로 제거 범위 지정 → 청록색 선택 (재클릭 해제) → 선택한 영역만 제거. 휠 위/아래: 확대/축소 · 우클릭 드래그: 화면 이동 · ←/→: 프레임 이동 · 1–4: 카메라 · H: 손 / B: 물체 · O: 원본 RGB 켜기/끄기 (자동 저장). 가려진 손가락에 주의하세요.',xalign=0);hint.set_line_wrap(True);box.pack_start(hint,False,False,0)
  frame=Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL,0,max(1,n-1),1);frame.set_digits(0);frame.set_draw_value(True);box.pack_start(frame,False,False,0)
  area=Gtk.EventBox();area.set_can_focus(True);picture=Gtk.Image();picture.set_halign(Gtk.Align.START);picture.set_valign(Gtk.Align.START);area.add(picture);area.set_size_request(int(w*scale),int(h*scale))
  area.set_halign(Gtk.Align.START);area.set_valign(Gtk.Align.START)
  scroll=Gtk.ScrolledWindow();scroll.set_policy(Gtk.PolicyType.AUTOMATIC,Gtk.PolicyType.AUTOMATIC);scroll.set_min_content_height(280);scroll.add(area);box.pack_start(scroll,True,True,0)
  info=Gtk.Label(xalign=0);box.pack_start(info,False,False,0)
  checks=[];visited=set();checkrow=Gtk.Box(spacing=12);box.pack_start(checkrow,False,False,0)
  for c in range(4):
   check=Gtk.CheckButton(label=f'Camera {c+1} 검수 완료');check.set_sensitive(False);checks.append(check);checkrow.pack_start(check,False,False,0)
  operator=Gtk.Entry();operator.set_placeholder_text('검수자 이름 · 승인 시 필수 확인');box.pack_start(operator,False,False,0)
  note=Gtk.Entry();note.set_placeholder_text('승인/보류 사유 · 누락/빈 마스크가 있으면 승인 사유 필수');box.pack_start(note,False,False,0)
  actions=Gtk.Box(spacing=10);box.pack_start(actions,False,False,0)
  undo=Gtk.Button(label='직전 수정 취소 (Ctrl+Z)');approve=Gtk.Button(label='4캠 검수 완료 · 승인본 저장');hold=Gtk.Button(label='보류 (후속 단계 차단)')
  for button in [undo,hold,approve]:actions.pack_start(button,False,False,0)
  # Keep the image viewport separate from the scrollable editing controls.
  for child in box.get_children():box.remove(child)
  def detach(widget):
   parent=widget.get_parent()
   if parent is not None:parent.remove(widget)
  def stack_section(title,widgets):
   section=Gtk.Expander(label=title);section.set_expanded(True)
   content=Gtk.Box(orientation=Gtk.Orientation.VERTICAL,spacing=5);content.set_margin_top(5);content.set_margin_bottom(8)
   for widget in widgets:
    detach(widget)
    if isinstance(widget,Gtk.Box):widget.set_orientation(Gtk.Orientation.VERTICAL)
    content.pack_start(widget,False,False,0)
   section.add(content);return section
  header=Gtk.Box(spacing=8);box.pack_start(header,False,False,0)
  for widget in [camera,obj,mode,zoom_label,reset_zoom]:
   detach(widget);header.pack_start(widget,False,False,0)
  overview_button=Gtk.ToggleButton(label='4캠 전체 (5)');header.pack_start(overview_button,False,False,0)
  panel_toggle=Gtk.ToggleButton(label='설정 패널');panel_toggle.set_active(True);header.pack_end(panel_toggle,False,False,0)
  fullscreen=Gtk.ToggleButton(label='전체 화면 (F11)');header.pack_end(fullscreen,False,False,0)
  review_bar=Gtk.Box(spacing=10);box.pack_start(review_bar,False,False,0)
  detach(checkrow);review_bar.pack_start(checkrow,False,False,0)
  for c,check in enumerate(checks):check.set_label(f'Cam {c+1} 검수 완료')
  approve.set_label('검수 승인 · 저장');hold.set_label('보류')
  for button in [approve,hold]:detach(button);review_bar.pack_start(button,False,False,0)
  rgb_hint=Gtk.ToggleButton(label='원본 RGB 보기 (O)');review_bar.pack_end(rgb_hint,False,False,0)
  approval_label=Gtk.Label(xalign=0);approval_label.set_ellipsize(Pango.EllipsizeMode.END);approval_label.set_max_width_chars(110)
  box.pack_start(approval_label,False,False,0)
  body=Gtk.Box(spacing=8);box.pack_start(body,True,True,0)
  scroll.set_min_content_height(160);scroll.set_hexpand(True);scroll.set_vexpand(True);body.pack_start(scroll,True,True,0)
  panel=Gtk.ScrolledWindow();panel.set_policy(Gtk.PolicyType.NEVER,Gtk.PolicyType.AUTOMATIC);panel.set_size_request(350,-1)
  side=Gtk.Box(orientation=Gtk.Orientation.VERTICAL,spacing=8);side.set_margin_start(5);side.set_margin_end(5);panel.add(side)
  panel_holder=Gtk.Box();panel_holder.pack_start(panel,True,True,0);body.pack_start(panel_holder,False,False,0)
  for label in [size_label,noise_info,hole_info,hint]:
   label.set_line_wrap(True);label.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR);label.set_max_width_chars(34)
  progress_label=Gtk.Label(xalign=0);progress_label.set_line_wrap(True);progress_label.set_max_width_chars(34)
  for title,widgets in [
   ('브러시',[row]),('노이즈 후보',[noise_row,noise_info]),
   ('선택 영역 / 일괄 제거',[select_row,batch_row]),
   ('내부 구멍',[hole_row,hole_info]),
   ('검수자 / 메모',[progress_label,operator,note,actions]),('사용 안내',[hint])]:
   side.pack_start(stack_section(title,widgets),False,False,0)
  for button in [remove_noise,remove_selected,batch_remove,fill_all,approve,hold,undo]:
   label=button.get_child()
   if isinstance(label,Gtk.Label):label.set_line_wrap(True);label.set_max_width_chars(30)
  box.pack_start(frame,False,False,0)
  info.set_ellipsize(Pango.EllipsizeMode.END);info.set_max_width_chars(100);box.pack_start(info,False,False,0)
  panel_toggle.connect('toggled',lambda button:panel_holder.set_visible(button.get_active()))
  fullscreen.connect('toggled',lambda button:window.fullscreen() if button.get_active() else window.unfullscreen())
  image_cache=ReviewImageCache()
  state={'overview':False,'grid_cache':None,'single_view':None,'progress_modified':False,'o_down':False,'rgb_only':False,'fit':True,'fit_pending':False,'pix':None,'mask':None,'rgb':None,'key':None,'drag':False,'undo':None,'cursor':None,'noise':None,'holes':None,'hole_areas':[],'hole_selected':Image.new('L',(w,h)),'areas':[],'pan':None,'rectangle':None,'batch_undo':[],'selected':Image.new('L',(w,h))}
  def show_approval(summary):
   status=summary.get('status')
   if status=='approved':
    timestamp=summary['approved_at']
    try:timestamp=datetime.fromisoformat(timestamp).astimezone(ZoneInfo('Asia/Seoul')).strftime('%Y-%m-%d %H:%M:%S KST')
    except ValueError:pass
    text=f"✓ 승인 완료 · 검수자: {summary['name']} · 승인 시각: {timestamp}"
    color='#15803d'
   else:
    text={'pending':'승인 대기 · 검수 완료 후 승인해 주세요.','needs_approval':'재승인 필요 · 이전 승인본은 현재 초안에 사용할 수 없습니다.','held':'보류 · 다음 단계 사용 전 승인이 필요합니다.'}.get(status,'승인 상태 확인 필요')
    color='#b45309'
   approval_label.set_markup(f'<b><span foreground="{color}">{GLib.markup_escape_text(text)}</span></b>')
   approval_label.set_tooltip_text(text+('\n'+summary['reason'] if summary.get('reason') else ''))
  def sync_paths():
   nonlocal d,draft
   updated=review.root(project,ep)
   if updated!=d:
    old=d;d=updated;draft=d/'review/draft'
    state['batch_undo']=[(c,o,f,d/path.relative_to(old) if path.is_relative_to(old) else path) for c,o,f,path in state['batch_undo']]
  def update_progress_label():
   progress_label.set_text('검수 체크 유지 · 마스크 수정 있음 · 최종 승인 별도' if state['progress_modified'] else '검수 체크는 직접 해제할 때까지 저장됩니다. 최종 승인은 별도입니다.')
  def persist_progress(*args):
   save_progress(draft,[c.get_active() for c in checks],operator.get_text(),note.get_text(),state['progress_modified']);update_progress_label()
  def mark_progress_modified():
   state['progress_modified']=True;persist_progress();show_approval({'status':'needs_approval'})
  def focus_canvas():
   # GTK scrolls focused children into view; restore the viewport synchronously.
   ha=scroll.get_hadjustment();va=scroll.get_vadjustment();x,y=ha.get_value(),va.get_value()
   area.grab_focus();ha.set_value(x);va.set_value(y)
  def refresh_noise():
   if state['noise'] is None:
    state['selected']=Image.new('L',(w,h))
    state['hole_selected']=Image.new('L',(w,h))
    state['holes'],state['hole_areas']=hole_candidates(state['mask'],hole_limit.get_value_as_int())
    state['noise'],state['areas']=candidates(state['mask'],threshold.get_value_as_int())
   editing=not state['overview']
   hole_enabled=editing and show_holes.get_active() and not state['rgb_only'] and not state['drag'] and state['rectangle'] is None
   fill_selected.set_sensitive(hole_enabled and state['hole_selected'].getbbox() is not None)
   fill_all.set_sensitive(hole_enabled and bool(state['hole_areas']))
   hole_info.set_text(f"구멍 후보 {len(state['hole_areas'])}개 / {sum(state['hole_areas'])}px · 영역 선택 모드에서 클릭 → 흰색 선택 · 바깥과 연결된 틈 제외")
   enabled=editing and show_noise.get_active() and not state['rgb_only'] and not state['drag']
   remove_noise.set_sensitive(enabled and bool(state['areas']))
   remove_selected.set_sensitive(editing and not state['rgb_only'] and not state['drag'] and state['selected'].getbbox() is not None)
   batch_remove.set_sensitive(editing and not state['rgb_only'] and not state['drag'] and state['selected'].getbbox() is not None)
   noise_info.set_text(f"현재 선택된 카메라·대상·프레임: 후보 {len(state['areas'])}개 / {sum(state['areas'])}px · 선택 {state['selected'].histogram()[255]}px · 제거 후 Ctrl+Z로 직전 작업 복구")
  def grid_image():
   f=min(n-1,int(frame.get_value()));o=obj.get_active()
   paths=[(prep/f'color/{c}/{f}.png',draft/f'mask/{c}/{o}/{f}.png') for c in range(4)]
   cache_key=('grid',f,o,state['rgb_only'],tuple((image_cache.stamp(r),image_cache.stamp(m)) for r,m in paths))
   cached=image_cache.get(cache_key)
   if cached is not None:return cached
   result=Image.new('RGB',(w*2,h*2));color=(40,220,80) if o==0 else (245,55,185)
   for c,(raw_path,mask_path) in enumerate(paths):
    raw=image_cache.read(raw_path,'RGB');tile=raw.copy()
    if not state['rgb_only']:
     mask=image_cache.read(mask_path,'L') if mask_path.exists() else Image.new('L',(w,h))
     tile.paste(Image.blend(raw,Image.new('RGB',(w,h),color),.45),(0,0),mask)
    draw=ImageDraw.Draw(tile);draw.rectangle((0,0,w-1,h-1),outline=(200,200,200),width=2)
    draw.rectangle((2,2,min(w-1,240),22),fill=(20,25,35));draw.text((6,5),f'CAM {c+1} | Frame {f}/{n-1}',fill='white')
    result.paste(tile,((c%2)*w,(c//2)*h))
   image_cache.put(cache_key,result);return result
  def redraw():
   if state['overview']:
    for button in [fill_selected,fill_all,remove_noise,remove_selected,batch_remove]:button.set_sensitive(False)
    rgb=grid_image()
   elif state['rgb_only']:
    for button in [fill_selected,fill_all,remove_noise,remove_selected,batch_remove]:button.set_sensitive(False)
    rgb=state['rgb'].copy()
   else:
    rgb=state['rgb'].copy();mask=state['mask'];color=(40,220,80) if obj.get_active()==0 else (245,55,185)
    overlay=Image.blend(rgb,Image.new('RGB',(w,h),color),.45);rgb.paste(overlay,(0,0),mask)
    if not state['drag']:
     refresh_noise()
     if show_noise.get_active():
      rgb.paste((255,225,0),(0,0),state['noise'])
     if show_holes.get_active():
      rgb.paste((255,135,30),(0,0),state['holes']);rgb.paste((255,255,255),(0,0),state['hole_selected'])
     selected_overlay=Image.blend(rgb,Image.new('RGB',(w,h),(0,235,255)),.35) if mode.get_active()==3 else (0,235,255)
     rgb.paste(selected_overlay,(0,0),state['selected'])
    if state['selected'].getbbox() and mode.get_active()==3:
     bounds=state['selected'].getbbox();ImageDraw.Draw(rgb).rectangle((bounds[0],bounds[1],bounds[2]-1,bounds[3]-1),outline=(255,255,255),width=1)
    rgb=preview(rgb,state['cursor'],radius.get_value(),mode.get_active() if mode.get_active() in (1,2) else 0)
   canvas_w,canvas_h=rgb.size
   zoom_label.set_text(f'{scale*100:.0f}% · 휠로 확대/축소')
   area.set_size_request(int(canvas_w*scale),int(canvas_h*scale))
   pixels=GLib.Bytes.new(rgb.tobytes());state['pix']=GdkPixbuf.Pixbuf.new_from_bytes(pixels,GdkPixbuf.Colorspace.RGB,False,8,canvas_w,canvas_h,canvas_w*3);picture.set_from_pixbuf(state['pix'].scale_simple(int(canvas_w*scale),int(canvas_h*scale),GdkPixbuf.InterpType.BILINEAR))
  def load(*args):
   # Commit an unfinished brush stroke against the OLD frame before switching.
   if state['drag'] and state['key'] is not None:
    state['drag']=False;save_edit()
   saved_h=scroll.get_hadjustment().get_value();saved_v=scroll.get_vadjustment().get_value()
   state['pan']=None;state['rectangle']=None
   if area.get_window():area.get_window().set_cursor(None)
   c=camera.get_active();o=obj.get_active();f=min(n-1,int(frame.get_value()));state['key']=(c,o,f);state['drag']=False;state['undo']=None;state['cursor']=None
   state['rgb']=image_cache.read(prep/f'color/{c}/{f}.png','RGB');path=draft/f'mask/{c}/{o}/{f}.png';state['mask']=image_cache.read(path,'L') if path.exists() else Image.new('L',(w,h),0)
   state['noise']=None
   if state['overview']:
    for index,check in enumerate(checks):visited.add(index);check.set_sensitive(True)
   visited.add(c);checks[c].set_sensitive(True);info.set_text(f'Camera {c+1} · frame {f}/{n-1} · 마우스 드래그로 편집 · 자동 저장 · ←/→ 프레임 이동');redraw()
   scroll.get_hadjustment().set_value(saved_h);scroll.get_vadjustment().set_value(saved_v)
   if state['overview']:info.set_text(f'4캠 전체 · Frame {f}/{n-1} · ←/→ 이동 · H/B 대상 · O 원본 · 1–4 개별 편집')
  def paint(event):
   if state['rgb_only'] or state['overview']:return
   x,y=source_point(event.x,event.y,(w,h),(int(w*scale),int(h*scale)));state['cursor']=(x,y)
   if 0<=x<w and 0<=y<h:
    state['noise']=None;remove_noise.set_sensitive(False);remove_selected.set_sensitive(False)
    r=radius.get_value();ImageDraw.Draw(state['mask']).ellipse((x-r,y-r,x+r,y+r),fill=255 if mode.get_active()==1 else 0);redraw()
  def update_rectangle(event):
   if state['rectangle'] is None:return
   x,y=source_point(event.x,event.y,(w,h),(int(w*scale),int(h*scale)))
   x=max(0,min(w-1,x));y=max(0,min(h-1,y));sx,sy=state['rectangle']
   state['selected']=Image.new('L',(w,h))
   ImageDraw.Draw(state['selected']).rectangle((min(sx,x),min(sy,y),max(sx,x),max(sy,y)),fill=255)
   redraw()
  def press(widget,event):
   if (state['rgb_only'] or state['overview']) and event.button==1:return True
   if event.button==3:
    if state['drag'] or state['rectangle'] is not None:return True
    focus_canvas();state['cursor']=None
    state['pan']=(event.x_root,event.y_root,scroll.get_hadjustment().get_value(),scroll.get_vadjustment().get_value())
    if area.get_window():area.get_window().set_cursor(Gdk.Cursor.new_from_name(area.get_display(),'grabbing'))
    redraw();return True
   if state['pan'] is not None:return True
   if event.button==1 and mode.get_active()==3:
    focus_canvas();refresh_noise()
    x,y=source_point(event.x,event.y,(w,h),(int(w*scale),int(h*scale)))
    if 0<=x<w and 0<=y<h:state['rectangle']=(x,y);update_rectangle(event)
    return True
   if event.button==1 and mode.get_active()==0:
    focus_canvas();refresh_noise()
    x,y=source_point(event.x,event.y,(w,h),(int(w*scale),int(h*scale)))
    if show_holes.get_active():
     hole=component_at(state['holes'],x,y)
     if hole.getbbox():
      state['hole_selected'].paste(0 if state['hole_selected'].getpixel((x,y)) else 255,(0,0),hole);redraw();return True
    region=component_at(state['mask'],x,y)
    if region.getbbox():
     state['selected'].paste(0 if state['selected'].getpixel((x,y)) else 255,(0,0),region);redraw()
   elif event.button==1 and mode.get_active()!=0:
    focus_canvas();state['batch_undo']=[];state['undo']=state['mask'].copy();state['drag']=True;paint(event)
  def motion(widget,event):
   if state['pan'] is not None:
    x,y,hstart,vstart=state['pan']
    for adjustment,value in [(scroll.get_hadjustment(),hstart+x-event.x_root),(scroll.get_vadjustment(),vstart+y-event.y_root)]:
     adjustment.set_value(max(0,min(max(0,adjustment.get_upper()-adjustment.get_page_size()),value)))
    return True
   if state['overview']:return True
   if state['rectangle'] is not None:update_rectangle(event);return True
   state['cursor']=source_point(event.x,event.y,(w,h),(int(w*scale),int(h*scale)))
   if state['drag']:paint(event)
   elif not state['rgb_only'] and mode.get_active() in (1,2):redraw()
  def save_edit():
   c,o,f=state['key'];path=draft/f'mask/{c}/{o}/{f}.png'
   if path.exists() and image_cache.read(path,'L').tobytes()==state['mask'].tobytes():
    info.set_text('마스크 변경 없음 · 기존 저장/승인 상태 유지');return
   state['grid_cache']=None
   state['batch_undo']=[]
   review.write_mask(project,ep,*state['key'],state['mask']);sync_paths()
   mark_progress_modified()
   info.set_text('초안 저장 완료. 수정 후 4캠을 다시 확인하고 승인하세요.')
  def release(widget,event):
   if event.button==1 and state['rectangle'] is not None:
    update_rectangle(event);state['rectangle']=None
    info.set_text('사각형 구역 지정 완료 · 현재 프레임 제거 또는 범위 확인 후 제거를 선택하세요');return True
   if event.button==3:
    state['pan']=None
    if area.get_window():area.get_window().set_cursor(None)
    return True
   if event.button==1 and state['drag']:state['drag']=False;save_edit();redraw()
  def undo_edit(*args):
   if state['overview']:return
   if state['batch_undo']:
    try:
     while state['batch_undo']:
      c,o,f,path=state['batch_undo'][-1]
      with Image.open(path) as original:review.write_mask(project,ep,c,o,f,original.convert('L'))
      sync_paths();state['batch_undo'].pop()
     mark_progress_modified()
     load();info.set_text('일괄 제거 전체 복구 완료 · 초안 저장됨');return
    except Exception as ex:info.set_text('복구 중단 (Ctrl+Z로 재시도): '+str(ex));return
   if state['undo'] is not None:state['drag']=False;state['mask']=state['undo'];state['undo']=None;state['noise']=None;save_edit();redraw()
  def noise_changed(*args):
   state['noise']=None
   if state['rgb'] is not None:redraw()
  def remove_small(*args):
   if state['drag'] or not show_noise.get_active():return
   refresh_noise()
   if not state['areas']:return
   count=len(state['areas']);pixels=sum(state['areas'])
   state['undo']=state['mask'].copy()
   state['mask']=remove_candidates(state['mask'],state['noise']);state['noise']=None
   save_edit();redraw();focus_canvas()
   info.set_text(f'현재 프레임 후보 {count}개 ({pixels}px) 제거 · 초안 저장 완료 · Ctrl+Z로 복구 가능')
  def clear_selection(*args):
   state['hole_selected']=Image.new('L',(w,h))
   state['selected']=Image.new('L',(w,h));redraw()
  def fill_holes(all_candidates=False):
   if state['drag'] or state['rectangle'] is not None or not show_holes.get_active():return
   refresh_noise()
   selection=state['holes'] if all_candidates else state['hole_selected']
   if selection.getbbox() is None:return
   state['undo']=state['mask'].copy()
   state['mask']=fill_candidates(state['mask'],selection);state['noise']=None
   save_edit();redraw();focus_canvas();info.set_text('내부 구멍 채우기 완료 · 초안 저장됨 · Ctrl+Z로 복구')
  def delete_selection(*args):
   if state['drag']:return
   refresh_noise()
   if state['selected'].getbbox() is None:return
   state['undo']=state['mask'].copy()
   state['mask']=remove_candidates(state['mask'],state['selected']);state['noise']=None
   save_edit();redraw();focus_canvas()
   info.set_text('선택한 영역만 제거하고 초안 저장 완료 · Ctrl+Z로 직전 제거 복구')
  def delete_range(*args):
   if state['drag']:return
   refresh_noise()
   footprint=state['selected'].copy()
   if footprint.getbbox() is None:return
   first=range_start.get_value_as_int();last=range_end.get_value_as_int()
   if not 0<=first<=last<n:info.set_text('프레임 범위를 확인하세요: 0 ≤ 시작 ≤ 끝 < '+str(n));return
   c,o,_=state['key'];affected=[];total=0
   try:
    for f in range(first,last+1):
     path=draft/f'mask/{c}/{o}/{f}.png'
     if not path.exists():continue
     with Image.open(path) as im:
      original=im.convert('L');clean=remove_candidates(original,footprint)
      count=sum(original.histogram()[1:])-sum(clean.histogram()[1:])
     if count:affected.append((f,path));total+=count
    if not affected:info.set_text('지정 범위에서 선택 위치와 겹치는 마스크가 없습니다.');return
    dialog=Gtk.MessageDialog(transient_for=window,modal=True,message_type=Gtk.MessageType.WARNING,buttons=Gtk.ButtonsType.OK_CANCEL,text='같은 위치의 마스크를 일괄 제거할까요?')
    dialog.format_secondary_text(f'Camera {c+1} · {"물체" if o==0 else "손"} · 프레임 {first}–{last} (양끝 포함)\n변경 {len(affected)}프레임 / {total}px\n청록색 선택 구역 안의 마스크를 각 프레임에서 모두 지웁니다. 움직임을 추적하거나 영역 전체로 확장하지 않습니다.\n해당 위치를 실제 물체가 지나가면 그것도 지워집니다. 취소 후 앞뒤 프레임을 확인할 수 있습니다.\nCtrl+Z로 이번 일괄 작업 전체 복구 (다음 수정 또는 창 종료 전).')
    result=dialog.run();dialog.destroy()
    if result!=Gtk.ResponseType.OK:return
    review.revoke(d,'batch mask edit');sync_paths()
    backup=d/'review'/'batch_backups'/uuid.uuid4().hex;backup.mkdir(parents=True)
    footprint.save(backup/'selection.png')
    # Back up every affected frame before writing any draft mask.
    for f,path in affected:(backup/f'{f}.png').write_bytes((draft/f'mask/{c}/{o}/{f}.png').read_bytes())
    (backup/'operation.json').write_text(json.dumps({'camera':c,'object':o,'frames':[f for f,_ in affected],'range':[first,last]},indent=2))
    state['undo']=None;state['batch_undo']=[]
    mark_progress_modified()
    for f,path in affected:
     saved=backup/f'{f}.png';state['batch_undo'].append((c,o,f,saved))
     with Image.open(saved) as original:review.write_mask(project,ep,c,o,f,remove_candidates(original.convert('L'),footprint))
    load();focus_canvas();info.set_text(f'{len(affected)}프레임 일괄 제거 완료 · Ctrl+Z로 전체 복구 · 원본 및 작업 전 백업 보존')
   except Exception as ex:
    load();focus_canvas();info.set_text('일괄 작업 중단: '+str(ex)+' · 반영된 수정은 Ctrl+Z로 복구하세요')
  def set_zoom(value,x=0,y=0):
   nonlocal scale
   if state['drag'] or state['pan'] is not None or state['rectangle'] is not None:return
   state['fit']=False
   old=scale;new=max(.1,min(4.,value))
   if new==old:return
   ha=scroll.get_hadjustment();va=scroll.get_vadjustment()
   vx=x-ha.get_value();vy=y-va.get_value()
   scale=new;state['cursor']=None;redraw()
   def anchor():
    ha.set_value(max(0,min(ha.get_upper()-ha.get_page_size(),x*new/old-vx)))
    va.set_value(max(0,min(va.get_upper()-va.get_page_size(),y*new/old-vy)))
    return False
   GLib.idle_add(anchor)
  def fit_view(*unused):
   allocation=scroll.get_allocation()
   if allocation.width>10 and allocation.height>10:
    set_zoom(min((allocation.width-4)/(w*(2 if state['overview'] else 1)),(allocation.height-4)/(h*(2 if state['overview'] else 1))))
    scroll.get_hadjustment().set_value(0);scroll.get_vadjustment().set_value(0)
   state['fit']=True;state['fit_pending']=False
   return False
  def viewport_resized(*unused):
   if state['fit'] and not state['fit_pending']:
    state['fit_pending']=True;GLib.idle_add(fit_view)
  def toggle_overview(button):
   nonlocal scale
   if state['drag']:state['drag']=False;save_edit()
   entering=button.get_active()
   if entering:
    state['single_view']=(scale,scroll.get_hadjustment().get_value(),scroll.get_vadjustment().get_value(),state['fit'])
   state['overview']=entering;state['grid_cache']=None;state['cursor']=None
   mode.set_sensitive(not entering);radius.set_sensitive(not entering);undo.set_sensitive(not entering)
   load()
   if entering:fit_view()
   elif state['single_view']:
    scale,x,y,fit=state['single_view'];state['fit']=fit;redraw()
    scroll.get_hadjustment().set_value(x);scroll.get_vadjustment().set_value(y)
  def camera_changed(*args):
   if state['overview']:overview_button.set_active(False)
   else:load()
  def wheel(widget,event):
   if event.direction==Gdk.ScrollDirection.UP:delta=-1
   elif event.direction==Gdk.ScrollDirection.DOWN:delta=1
   elif event.direction==Gdk.ScrollDirection.SMOOTH:
    ok,dx,delta=event.get_scroll_deltas()
    if not ok:return False
   else:return False
   set_zoom(scale*1.2**(-max(-3,min(3,delta))),event.x,event.y);return True
  def key_press(widget,event):
   name=Gdk.keyval_name(event.keyval) or ''
   if name in ('5','KP_5'):
    if isinstance(window.get_focus(),Gtk.Entry):return False
    if event.state & (Gdk.ModifierType.CONTROL_MASK|Gdk.ModifierType.MOD1_MASK|Gdk.ModifierType.SHIFT_MASK):return False
    overview_button.set_active(not overview_button.get_active());return True
   if state['overview'] and (name.lower() in ('a','e','n','m') or name in ('bracketleft','bracketright')):return True
   if name.lower()=='o':
    if isinstance(window.get_focus(),Gtk.Entry):return False
    if event.state & (Gdk.ModifierType.CONTROL_MASK|Gdk.ModifierType.MOD1_MASK|Gdk.ModifierType.SHIFT_MASK):return False
    if not state['o_down']:
     state['o_down']=True;rgb_hint.set_active(not rgb_hint.get_active())
    return True
   if name.lower() in ('a','e') or name in ('bracketleft','bracketright'):
    if isinstance(window.get_focus(),Gtk.Entry):return False
    if event.state & (Gdk.ModifierType.CONTROL_MASK|Gdk.ModifierType.MOD1_MASK):return False
    if state['drag']:state['drag']=False;save_edit()
    if name.lower() in ('a','e'):mode.set_active(1 if name.lower()=='a' else 2)
    else:radius.set_value(radius.get_value()+(-1 if name=='bracketleft' else 1))
    return True
   if name.lower() in ('h','b'):
    if isinstance(window.get_focus(),Gtk.Entry):return False
    if event.state & (Gdk.ModifierType.CONTROL_MASK|Gdk.ModifierType.MOD1_MASK|Gdk.ModifierType.SHIFT_MASK):return False
    target=1 if name.lower()=='h' else 0
    if obj.get_active()!=target:obj.set_active(target)
    return True
   if state['rgb_only'] and name.lower() in ('n','m'):return True
   camera_keys={'1':0,'2':1,'3':2,'4':3,'KP_1':0,'KP_2':1,'KP_3':2,'KP_4':3}
   if name in camera_keys:
    if isinstance(window.get_focus(),Gtk.Entry):return False
    if event.state & (Gdk.ModifierType.CONTROL_MASK|Gdk.ModifierType.MOD1_MASK|Gdk.ModifierType.SHIFT_MASK):return False
    if camera.get_active()!=camera_keys[name]:camera.set_active(camera_keys[name])
    elif state['overview']:overview_button.set_active(False)
    return True
   if name=='F11':fullscreen.set_active(not fullscreen.get_active());return True
   if name.lower() in ('n','m'):
    if isinstance(window.get_focus(),Gtk.Entry):return False
    if event.state & (Gdk.ModifierType.CONTROL_MASK|Gdk.ModifierType.MOD1_MASK|Gdk.ModifierType.SHIFT_MASK):return False
    action=remove_noise if name.lower()=='n' else fill_all
    if action.get_sensitive() and not state['drag'] and state['rectangle'] is None and state['pan'] is None:
     action.clicked()
    return True
   if name in ('Left','Right'):
    # Preserve normal text editing and numeric input in focused entry widgets.
    if isinstance(window.get_focus(),Gtk.Entry):return False
    if event.state & (Gdk.ModifierType.CONTROL_MASK|Gdk.ModifierType.MOD1_MASK):return False
    current=min(n-1,int(frame.get_value()))
    target=max(0,min(n-1,current+(-1 if name=='Left' else 1)))
    if target!=current:frame.set_value(target)
    return True
   if event.state & Gdk.ModifierType.CONTROL_MASK and (Gdk.keyval_name(event.keyval) or '').lower()=='z':
    if isinstance(window.get_focus(),Gtk.Entry):return False
    undo_edit();return True
   return False
  def toggle_rgb(button):
   if state['drag']:state['drag']=False;save_edit()
   state['rectangle']=None;state['rgb_only']=button.get_active()
   button.set_label('원본 RGB 표시 중 · 복귀 (O)' if state['rgb_only'] else '원본 RGB 보기 (O)')
   redraw()
  def key_release(widget,event):
   if (Gdk.keyval_name(event.keyval) or '').lower()=='o':
    state['o_down']=False;return True
   return False
  def focus_lost(*args):
   state['o_down']=False
   return False
  def leave(widget,event):
   state['cursor']=None;redraw()
  def brush_changed(*unused):
   size_label.set_text(f'지름 {int(radius.get_value()*2)}px · 원본 기준')
   if state['rgb'] is not None:redraw()
  def accept(*args):
   if not all(c.get_active() for c in checks):info.set_text('4개 카메라의 검수 완료를 먼저 체크하세요.');return
   dialog=Gtk.Dialog(title='검수 승인 · 검수자 확인',transient_for=window,modal=True)
   dialog.add_button('취소',Gtk.ResponseType.CANCEL);confirm=dialog.add_button('이 이름으로 승인 · 저장',Gtk.ResponseType.OK)
   content=dialog.get_content_area();content.set_spacing(10);content.set_border_width(14)
   content.pack_start(Gtk.Label(label='검수자 이름을 입력하거나 확인하세요. 승인 메타데이터에 기록됩니다.',xalign=0),False,False,0)
   name=Gtk.Entry();name.set_placeholder_text('검수자 이름 (필수)');name.set_text(operator.get_text());content.pack_start(name,False,False,0)
   confirm.set_sensitive(bool(name.get_text().strip()))
   name.connect('changed',lambda field:confirm.set_sensitive(bool(field.get_text().strip())))
   dialog.show_all();name.grab_focus();response=dialog.run();reviewer=name.get_text().strip();dialog.destroy()
   if response!=Gtk.ResponseType.OK:return
   if not reviewer:info.set_text('승인 중단: 검수자 이름을 입력하세요.');return
   operator.set_text(reviewer)
   try:
    path=review.approve(project,ep,reviewer,[c.get_active() for c in checks],note.get_text());sync_paths();state['progress_modified']=False;persist_progress();show_approval(review.approval_summary(project,ep));info.set_text('승인 완료 · 검수자: '+reviewer+' · 메타데이터: '+str(path/'approval.json'))
   except Exception as ex:info.set_text('승인 중단: '+str(ex))
  def reject(*args):review.revoke(d,note.get_text() or 'operator hold');sync_paths();show_approval({'status':'held'});info.set_text('보류 저장. 승인 전에는 다음 단계로 진행할 수 없습니다.')
  area.add_events(Gdk.EventMask.BUTTON_PRESS_MASK|Gdk.EventMask.BUTTON_RELEASE_MASK|Gdk.EventMask.POINTER_MOTION_MASK|Gdk.EventMask.LEAVE_NOTIFY_MASK|Gdk.EventMask.SCROLL_MASK|Gdk.EventMask.SMOOTH_SCROLL_MASK)
  for event,fn in [('button-press-event',press),('button-release-event',release),('motion-notify-event',motion),('leave-notify-event',leave)]:area.connect(event,fn)
  area.connect('scroll-event',wheel);reset_zoom.connect('clicked',fit_view);scroll.connect('size-allocate',viewport_resized);remove_selected.connect('clicked',delete_selection);clear_selected.connect('clicked',clear_selection)
  batch_remove.connect('clicked',delete_range)
  overview_button.connect('toggled',toggle_overview);camera.connect('changed',camera_changed);obj.connect('changed',load);frame.connect('value-changed',load);undo.connect('clicked',undo_edit);approve.connect('clicked',accept);hold.connect('clicked',reject)
  hole_limit.connect('value-changed',noise_changed);show_holes.connect('toggled',noise_changed);fill_selected.connect('clicked',lambda *_:fill_holes(False));fill_all.connect('clicked',lambda *_:fill_holes(True))
  threshold.connect('value-changed',noise_changed);show_noise.connect('toggled',noise_changed);remove_noise.connect('clicked',remove_small)
  radius.connect('value-changed',brush_changed);mode.connect('changed',brush_changed);window.connect('key-press-event',key_press);window.connect('key-release-event',key_release);window.connect('focus-out-event',focus_lost);rgb_hint.connect('toggled',toggle_rgb)
  for button in [remove_noise,remove_selected,clear_selected,reset_zoom,batch_remove,fill_selected,fill_all,undo,hold,approve]:
   button.get_style_context().add_provider(button_css,Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
  show_approval(review.approval_summary(project,ep))
  restored=load_progress(draft)
  if restored is None and (d/'review/approved.json').exists():
   try:
    approved_dir=review.approved(project,ep);record=json.loads((approved_dir/'approval.json').read_text())
    restored={'checked':record['checked_cameras'],'operator':record['operator'],'note':record['note']}
   except Exception as ex:info.set_text('이전 승인 상태 확인 필요: '+str(ex))
  if restored:
   state['progress_modified']=bool(restored.get('modified',False))
   operator.set_text(restored.get('operator',operator.get_text()));note.set_text(restored.get('note',''))
   for check,done in zip(checks,restored['checked']):
    check.set_active(done)
    if done:check.set_sensitive(True)
  update_progress_label()
  for check in checks:check.connect('toggled',persist_progress)
  mode.set_tooltip_text('A: 추가 브러시 · E: 지우기 브러시');radius.set_tooltip_text('[: 반경 1px 줄이기 · ]: 반경 1px 키우기')
  window.connect('destroy',lambda *_:Gtk.main_quit());load();window.show_all();GLib.idle_add(fit_view);Gtk.main()
if __name__=='__main__':
 try:main()
 except Exception as ex:print('검수 GUI 오류:',ex,file=sys.stderr);raise SystemExit(1)
