"""Record mask coverage and create a real-frame contact sheet; never grant approval."""
import argparse,json
from pathlib import Path
import numpy as np
from PIL import Image,ImageDraw
p=argparse.ArgumentParser();p.add_argument('--case',type=Path,required=True);p.add_argument('--out',type=Path,required=True);a=p.parse_args();a.out.mkdir(parents=True,exist_ok=True)
frames=sorted(int(x.stem) for x in (a.case/'color/0').glob('*.png'));chosen=[frames[0],frames[len(frames)//2],frames[-1]]
report={'frames':len(frames),'sampled_frames':chosen,'approval':'requires visual review','cameras':{}}
canvas=Image.new('RGB',(1272,1080),'#eef2f6');draw=ImageDraw.Draw(canvas)
for c in range(4):
 stats={};masks={}
 for obj,label in [(0,'object'),(1,'hand')]:
  areas=[];missing=[]
  for f in frames:
   path=a.case/f'mask/{c}/{obj}/{f}.png'
   if not path.exists():missing.append(f);areas.append(0);continue
   with Image.open(path) as im:mask=np.asarray(im)>0
   areas.append(int(mask.sum()))
   if f in chosen:masks[(obj,f)]=mask
  stats[label]={'files':len(frames)-len(missing),'nonempty':sum(x>0 for x in areas),'missing_frames':missing,'empty_or_missing_frames':[f for f,n in zip(frames,areas) if not n],'min_area':min(areas),'max_area':max(areas)}
 report['cameras'][str(c)]=stats
 for j,f in enumerate(chosen):
  with Image.open(a.case/f'color/{c}/{f}.png') as im:rgb=np.asarray(im.convert('RGB')).astype(float)
  for obj,color in [(0,[30,220,70]),(1,[245,45,190])]:
   m=masks.get((obj,f))
   if m is not None:
    assert m.shape==rgb.shape[:2], 'mask/image dimensions differ'
    rgb[m]=rgb[m]*0.55+np.array(color)*0.45
  canvas.paste(Image.fromarray(rgb.astype('uint8')).resize((424,240)),(j*424,c*270+30))
  draw.text((j*424+8,c*270+8),f'CAM {c+1} / frame {f} | green: object, pink: hand',fill='#172536')
canvas.save(a.out/'mask_contact.jpg',quality=92)
(a.out/'mask_report.json').write_text(json.dumps(report,indent=2));print(json.dumps(report))
