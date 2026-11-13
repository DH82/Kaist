#!/usr/bin/env python3
"""Native rotating 3D inspection: measured trajectories and static meshes."""
import argparse,json,math,importlib
from pathlib import Path
from PIL import Image,ImageDraw
import gi
gi.require_version('Gtk','3.0')
from gi.repository import Gtk,Gdk,GdkPixbuf,GLib

from reconstruction_noise_ui import NoiseControls
from point_display_ui import PointDisplay
from view_rotation import initial_rotation,rotate_view,transform,roll_view

class Viewer(Gtk.Window,NoiseControls,PointDisplay):
 __gtype_name__="WfmThreeDViewer"
 def __init__(self,data,case=None):
  super().__init__(title='WFM · 3D 결과 · '+data['episode']);self.data=data;self.frame=0;self.rotation=initial_rotation();self.zoom=1.;self.pan=[0.,0.];self.drag=None;self.playing=False;self.closed=False
  self.set_default_size(1400,900);self.maximize();self.set_border_width(4)
  box=Gtk.Box(orientation=Gtk.Orientation.VERTICAL,spacing=3);self.add(box)
  row=Gtk.Box(spacing=8);box.pack_start(row,False,False,0);self.mode=Gtk.ComboBoxText()
  for s in ['실측 물체·손 3D 추적 (m)','정합 물체 메시 (m · 기준 프레임 0)','생성 물체 메시 (정합 전 · 정규화 좌표)']:self.mode.append_text(s)
  self.mode.set_active(0);self.mode.connect('changed',self.change);row.pack_start(self.mode,False,False,0)
  for title,fn in [('재생 / 정지',self.toggle),('시점 초기화',self.reset)]:b=Gtk.Button(label=title);b.connect('clicked',fn);row.pack_start(b,False,False,0)
  self.label=Gtk.Label(xalign=0);row.pack_start(self.label,False,False,0)
  self.noise_controls(box,case)
  self.point_controls(box)
  self.holder=Gtk.ScrolledWindow();self.holder.set_policy(Gtk.PolicyType.AUTOMATIC,Gtk.PolicyType.AUTOMATIC);self.holder.set_size_request(300,200)
  self.events=Gtk.EventBox();self.events.set_can_focus(True);self.image=Gtk.Image();self.events.add(self.image);self.holder.add(self.events);box.pack_start(self.holder,True,True,0)
  self.events.add_events(Gdk.EventMask.BUTTON_PRESS_MASK|Gdk.EventMask.BUTTON_RELEASE_MASK|Gdk.EventMask.POINTER_MOTION_MASK|Gdk.EventMask.SCROLL_MASK|Gdk.EventMask.SMOOTH_SCROLL_MASK)
  self.events.connect('button-press-event',self.press);self.events.connect('button-release-event',lambda *_:setattr(self,'drag',None));self.events.connect('motion-notify-event',self.motion);self.events.connect('scroll-event',self.scroll)
  self.slider=Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL,0,max(1,data['frames']-1),1);self.slider.set_digits(0);self.slider.connect('value-changed',self.seek);box.pack_start(self.slider,False,False,0)
  help_text='좌드래그: 화면 축 회전 · 우/중드래그: 이동 · 좌우 스크롤: Yaw · 상하 휠: 확대 · Q/E: Roll · R: 초기화 · ←/→: 프레임 · Space: 재생\n정상 점은 표시용 샘플, 후보는 모두 표시합니다. 적용은 추적점 정제본만 저장하며 메시/final_data는 재생성하지 않습니다. 메시는 기준 프레임 0의 정적 형상이며 손 점군은 MANO가 아닙니다.'
  note=Gtk.Label(label='좌드래그 회전 · 우/중드래그 이동 · 휠 확대 · Q/E Roll · Tab 설정 · F11 전체화면 · ←/→ 프레임',xalign=0);note.set_tooltip_text(help_text);box.pack_start(note,False,False,0)
  self.is_fullscreen=False

  self.holder.connect('size-allocate',lambda *_:self.render());self.connect('key-press-event',self.key);self.connect('destroy',self.close);self.bounds();self.show_all();self.timer=GLib.timeout_add(67,self.tick)
 def bounds(self):
  mode=self.mode.get_active()
  points=[p for frames in self.data['points'].values() for frame in frames for p in frame] if mode==0 else self.data['meshes']['aligned' if mode==1 else 'generated']['vertices']
  if mode==0 and self.noise:points=[item['p'] for frames in self.noise['points'].values() for frame in frames for item in frame]
  if mode==0 and getattr(self,'full_points',None):points=[p for bounds in self.full_points['bounds'].values() if bounds for p in bounds]
  if not points:points=[[0,0,0],[1,1,1]]
  lo=[min(p[k] for p in points) for k in range(3)];hi=[max(p[k] for p in points) for k in range(3)];self.center=[(a+b)/2 for a,b in zip(lo,hi)];self.extent=max(1e-6,max(b-a for a,b in zip(lo,hi)))
 def change(self,*_):
  if not hasattr(self,'slider'):return
  self.playing=False;self.zoom=1;self.pan=[0.,0.];self.slider.set_sensitive(self.mode.get_active()==0);self.bounds();self.render()
 def reset(self,*_):self.rotation=initial_rotation();self.zoom=1;self.pan=[0.,0.];self.render()
 def project(self,p,w,h):
  x=p[0]-self.center[0];y=p[1]-self.center[1];z=p[2]-self.center[2];a,b,c=self.rotation
  xx=a[0]*x+a[1]*y+a[2]*z;yy=b[0]*x+b[1]*y+b[2]*z;depth=c[0]*x+c[1]*y+c[2]*z;s=min(w,h)*.8*self.zoom/self.extent
  return (w/2+(xx+self.pan[0])*s,h/2+(-yy+self.pan[1])*s,depth)
 def render(self):
  if not hasattr(self,'center') or self.closed:return
  w=max(10,self.holder.get_allocated_width()-4);h=max(10,self.holder.get_allocated_height()-4);im=Image.new('RGB',(w,h),(16,24,36));draw=ImageDraw.Draw(im);mode=self.mode.get_active()
  if mode==0:
   variant=self.noise_mode.get_active() if self.noise else 0
   panels=[(0,w,variant)] if variant!=3 else [(0,w//2,0),(w//2,w-w//2,2)]
   for offset,panel_width,kind in panels:
    points=[(self.project(p,panel_width,h),color,radius,group,reason) for p,color,radius,group,reason in self.noise_glyphs(kind)]
    for (x,y,z),color,radius,group,reason in sorted(points,key=lambda item:item[0][2]):
     rect=(offset+x-radius,y-radius,offset+x+radius,y+radius)
     glyph=draw.ellipse if group=='object' else draw.rectangle
     glyph(rect,fill=color)
     if kind==1 and reason:
      # Outline encodes detector, inner colour/shape retains semantic identity.
      base=(85,170,255) if group=='object' else (255,170,85)
      glyph((offset+x-1,y-1,offset+x+1,y+1),fill=base)
    if variant==3:draw.text((offset+12,12),'BEFORE' if kind==0 else 'AFTER (preview)',fill=(240,240,240))
   if variant==3:draw.line((w//2,0,w//2,h),fill=(100,115,130))
  else:
   mesh=self.data['meshes']['aligned' if mode==1 else 'generated'];vertices=[self.project(p,w,h) for p in mesh['vertices']]
   for face in sorted(mesh['faces'],key=lambda ids:sum(vertices[i][2] for i in ids)):
    coords=[vertices[i][:2] for i in face];draw.polygon(coords,fill=(116,152,184),outline=(65,90,112))
  self.label.set_text(f'프레임 {self.frame}/{self.data["frames"]-1}' if mode==0 else '정적 형상 · 기준 프레임 0')
  if mode==0:self.update_point_counts(variant)
  self.image.set_from_pixbuf(GdkPixbuf.Pixbuf.new_from_bytes(GLib.Bytes.new(im.tobytes()),GdkPixbuf.Colorspace.RGB,False,8,w,h,w*3))
 def seek(self,*_):self.frame=min(self.data['frames']-1,int(self.slider.get_value()));self.render()
 def toggle(self,*_):
  if self.mode.get_active()==0:self.playing=not self.playing
 def tick(self):
  if self.closed:return False
  if self.playing:self.slider.set_value((self.frame+1)%self.data['frames'])
  return True
 def press(self,_,e):
  if e.button not in (1,2,3):return False
  self.events.grab_focus()
  action='pan' if e.button in (2,3) or e.state&Gdk.ModifierType.SHIFT_MASK else 'rotate'
  self.drag=(e.x,e.y,action);return True
 def motion(self,_,e):
  if not self.drag:return False
  x,y,action=self.drag;dx=e.x-x;dy=e.y-y
  if action=='pan':
   w=max(10,self.holder.get_allocated_width()-4);h=max(10,self.holder.get_allocated_height()-4)
   if self.mode.get_active()==0 and self.noise and self.noise_mode.get_active()==3:w/=2
   scale=min(w,h)*.8*self.zoom/self.extent
   self.pan[0]+=dx/scale;self.pan[1]+=dy/scale
  else:self.rotation=rotate_view(self.rotation,dx*.01,dy*.01)
  self.drag=(e.x,e.y,action);self.render();return True
 def scroll(self,_,e):
  dx=dy=0.
  if e.direction==Gdk.ScrollDirection.SMOOTH:
   valid,dx,dy=e.get_scroll_deltas()
   if not valid:return False
  elif e.direction==Gdk.ScrollDirection.LEFT:dx=-1.
  elif e.direction==Gdk.ScrollDirection.RIGHT:dx=1.
  elif e.direction==Gdk.ScrollDirection.UP:dy=-1.
  elif e.direction==Gdk.ScrollDirection.DOWN:dy=1.
  else:return False
  # Horizontal scrolling rotates around the current screen vertical axis.
  self.rotation=rotate_view(self.rotation,dx*.12,0.)
  self.zoom=max(.1,min(10,self.zoom*math.exp(-dy*.12)));self.render();return True
 def key(self,_,e):
  k=Gdk.keyval_name(e.keyval)
  if k=='F11':
   self.is_fullscreen=not self.is_fullscreen
   if self.is_fullscreen:self.fullscreen()
   else:self.unfullscreen()
   return True
  if isinstance(self.get_focus(),Gtk.Entry):return False
  if k=='Tab':self.settings_expander.set_expanded(not self.settings_expander.get_expanded());self.events.grab_focus();return True
  if k in ('Left','Right') and self.mode.get_active()==0:self.playing=False;self.slider.set_value(max(0,min(self.data['frames']-1,self.frame+(-1 if k=='Left' else 1))));return True
  if k=='space':self.toggle();return True
  if k in ('r','R'):self.reset();return True
  if k in ('q','Q','e','E'):
   self.rotation=roll_view(self.rotation,math.radians(5 if k.lower()=='q' else -5));self.render();return True
  return False
 def close(self,*_):
  self.closed=True
  if hasattr(self,'timer'):GLib.source_remove(self.timer)
  if Gtk.main_level():Gtk.main_quit()

def main():
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('input',type=Path);a=p.parse_args()
 try:
  case=a.input.resolve();print('3D 승인 입력·산출물 무결성 확인 중…',flush=True);importlib.import_module('10_reconstruct').verify_outputs(case)
  if not Gtk.init_check()[0]:raise ValueError('데스크톱 터미널에서 실행하세요.')
  Viewer(json.loads((case/'review/view3d.json').read_text()),case);Gtk.main()
 except Exception as ex:p.exit(1,'중단: '+str(ex)+'\n')
if __name__=='__main__':main()
