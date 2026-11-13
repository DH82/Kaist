#!/usr/bin/env python3
"""Native frame viewer for completed four-camera MANO projections."""
import argparse,importlib
from pathlib import Path
from collections import OrderedDict
import gi
gi.require_version('Gtk','3.0')
from gi.repository import Gtk,Gdk,GdkPixbuf,GLib
class Window(Gtk.Window):
 __gtype_name__='WFMHandReviewWindow'
 def __init__(self,out,T):
  super().__init__(title='WFM · MANO 4캠 결과 · '+out.parent.name);self.out=out;self.T=T;self.frame=0;self.playing=False;self.cache=OrderedDict();self.closed=False
  self.set_default_size(1400,900);self.maximize();box=Gtk.Box(orientation=Gtk.Orientation.VERTICAL,spacing=4);self.add(box)
  row=Gtk.Box(spacing=8);box.pack_start(row,False,False,0)
  button=Gtk.Button(label='재생 / 정지 (Space)');button.connect('clicked',lambda *_:self.toggle());row.pack_start(button,False,False,0);self.label=Gtk.Label();row.pack_start(self.label,False,False,0)
  note=Gtk.Label(label='4캠 RGB 위 MANO 결과: 손목 위치·손가락 자세·좌우 손·프레임 튐을 확인하세요. 자동 구조 검사 통과는 정확도 승인이 아닙니다.',xalign=0);note.set_line_wrap(True);box.pack_start(note,False,False,0)
  self.holder=Gtk.ScrolledWindow();self.holder.set_policy(Gtk.PolicyType.AUTOMATIC,Gtk.PolicyType.AUTOMATIC);self.holder.set_size_request(300,200);self.image=Gtk.Image();self.holder.add(self.image);box.pack_start(self.holder,True,True,0)
  self.slider=Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL,0,max(1,T-1),1);self.slider.set_digits(0);self.slider.connect('value-changed',self.seek);box.pack_start(self.slider,False,False,0)
  self.holder.connect('size-allocate',lambda *_:self.render());self.connect('key-press-event',self.key);self.connect('destroy',self.close);self.show_all();self.timer=GLib.timeout_add(67,self.tick);self.render()
 def render(self):
  if self.closed:return
  if self.frame not in self.cache:
   self.cache[self.frame]=GdkPixbuf.Pixbuf.new_from_file(str(self.out/'review/frames'/f'{self.frame:06d}.jpg'))
   while len(self.cache)>8:self.cache.popitem(last=False)
  self.cache.move_to_end(self.frame);im=self.cache[self.frame];scale=min(max(1,self.holder.get_allocated_width()-4)/im.get_width(),max(1,self.holder.get_allocated_height()-4)/im.get_height())
  self.image.set_from_pixbuf(im.scale_simple(max(1,int(im.get_width()*scale)),max(1,int(im.get_height()*scale)),GdkPixbuf.InterpType.BILINEAR));self.label.set_text(f'프레임 {self.frame}/{self.T-1}')
 def seek(self,*_):self.frame=min(self.T-1,int(self.slider.get_value()));self.render()
 def toggle(self):self.playing=not self.playing
 def tick(self):
  if self.closed:return False
  if self.playing:self.slider.set_value((self.frame+1)%self.T)
  return True
 def key(self,_,e):
  key=Gdk.keyval_name(e.keyval)
  if key=='space':self.toggle();return True
  if key in ('Left','Right'):self.playing=False;self.slider.set_value(max(0,min(self.T-1,self.frame+(-1 if key=='Left' else 1))));return True
  return False
 def close(self,*_):
  self.closed=True
  if hasattr(self,'timer'):GLib.source_remove(self.timer)
  if Gtk.main_level():Gtk.main_quit()
def main():
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('input',type=Path);a=p.parse_args()
 try:
  out=a.input.resolve();report=importlib.import_module('18_fit_hands').verify(out)
  if not Gtk.init_check()[0]:raise ValueError('데스크톱 터미널에서 실행하세요.')
  Window(out,report['frames']);Gtk.main()
 except Exception as ex:p.exit(1,'중단: '+str(ex)+'\n')
if __name__=='__main__':main()
