#!/usr/bin/env python3
"""로컬 추적 검수 창: 물체·손 동기 재생, 승인·보류 기록."""
import argparse
from pathlib import Path
from tracking_review import resolve

def main():
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('input',type=Path);a=p.parse_args()
 try:
  from tracking_local_review import run
  run(resolve(a.input))
 except (ValueError,OSError,KeyError,ImportError) as ex:p.exit(1,'중단: '+str(ex)+'\n')
if __name__=='__main__':main()
