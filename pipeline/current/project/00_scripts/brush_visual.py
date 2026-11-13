"""Display-only cursor; never paints into stored RGB or mask data."""
from PIL import ImageDraw

def preview(image,cursor,radius,mode):
 out=image.copy()
 if cursor is None or mode==0:return out
 x,y=cursor;r=float(radius)
 if not (0<=x<out.width and 0<=y<out.height):return out
 draw=ImageDraw.Draw(out);box=(x-r,y-r,x+r,y+r)
 draw.ellipse(box,outline=(0,0,0),width=3)
 draw.ellipse(box,outline=(70,240,130) if mode==1 else (255,220,70),width=1)
 draw.line((x-3,y,x+3,y),fill=(255,255,255),width=1);draw.line((x,y-3,x,y+3),fill=(255,255,255),width=1)
 return out
