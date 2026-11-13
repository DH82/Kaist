"""Bounded, session-only image cache; file replacement invalidates cached pixels."""
from collections import OrderedDict
from PIL import Image

class ReviewImageCache:
 def __init__(self,max_bytes=256*1024*1024):
  self.max_bytes=max_bytes;self.bytes=0;self.items=OrderedDict()
 @staticmethod
 def stamp(path):
  try:
   s=path.stat();return (str(path),s.st_ino,s.st_mtime_ns,s.st_ctime_ns,s.st_size)
  except FileNotFoundError:return (str(path),None)
 def get(self,key):
  value=self.items.get(key)
  if value is None:return None
  self.items.move_to_end(key);return value[0].copy()
 def put(self,key,image):
  size=image.width*image.height*len(image.getbands())
  if size>self.max_bytes:return
  if key in self.items:self.bytes-=self.items.pop(key)[1]
  self.items[key]=(image.copy(),size);self.bytes+=size
  while self.bytes>self.max_bytes:
   _,(_,size)=self.items.popitem(last=False);self.bytes-=size
 def read(self,path,mode):
  key=('file',mode,self.stamp(path));found=self.get(key)
  if found is not None:return found
  with Image.open(path) as im:result=im.convert(mode)
  # Do not cache an image under a stamp changed during decoding.
  if key[-1]==self.stamp(path):self.put(key,result)
  return result
