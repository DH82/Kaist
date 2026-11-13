"""Runtime checks and array validation, executed with the PhysTwin Python."""
import argparse,json,os,shutil
from pathlib import Path

def validate(path,frames):
 import numpy as np
 with np.load(path,allow_pickle=False) as data:
  tracks=data['tracks'];visibility=data['visibility'];query=data['query_frame']
  if tracks.ndim!=3 or tracks.shape[0]!=frames or tracks.shape[2]!=2 or tracks.shape[1]<1:raise ValueError('Invalid tracks shape: '+str(tracks.shape))
  count=tracks.shape[1]
  if visibility.shape!=(frames,count) or query.shape!=(count,):raise ValueError('Invalid visibility/query dimensions')
  if not np.isfinite(tracks).all() or not np.isfinite(query).all():raise ValueError('Non-finite tracking data')
  if not ((query>=0)&(query<frames)&(query==np.floor(query))).all():raise ValueError('Invalid query frames')
  if not np.isin(visibility,[0,1]).all():raise ValueError('Invalid visibility')
 return {'frames':frames,'points':count}

def main():
 p=argparse.ArgumentParser();p.add_argument('action',choices=['check','validate']);p.add_argument('--path',type=Path);p.add_argument('--frames',type=int);p.add_argument('--repo',type=Path);a=p.parse_args()
 if a.action=='validate':print(json.dumps(validate(a.path,a.frames)));return
 import torch,cv2,numpy,imageio.v3
 import imageio_ffmpeg
 if not torch.cuda.is_available():raise RuntimeError('CUDA GPU를 사용할 수 없습니다. 일반 터미널에서 NVIDIA 드라이버 상태를 확인하세요.')
 if not a.repo or not (a.repo/'hubconf.py').is_file():raise ValueError('CoTracker repository missing')
 checkpoint=Path(torch.hub.get_dir())/'checkpoints/scaled_online.pth'
 if not checkpoint.is_file():raise ValueError('CoTracker3 checkpoint missing: '+str(checkpoint))
 if not shutil.which('ffmpeg'):raise ValueError('ffmpeg not found')
 print(json.dumps({'gpu':torch.cuda.get_device_name(0),'checkpoint':str(checkpoint),'ffmpeg':shutil.which('ffmpeg'),'video_decoder':imageio_ffmpeg.get_ffmpeg_exe()},ensure_ascii=False))
if __name__=='__main__':main()
