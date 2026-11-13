#!/usr/bin/env python3
"""승인된 추적만 3D 단계 입력으로 준비합니다. 3D 추론은 실행하지 않습니다."""
import argparse
from pathlib import Path
from mask_review import lock
from tracking_paths import resolve_input
from tracking_review import resolve,prepare_reconstruction

def main():
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('input',type=Path);a=p.parse_args()
 try:
  case=resolve(a.input)
  with lock(resolve_input(case)[0]):print('승인된 3D 입력 준비:',prepare_reconstruction(case))
 except (ValueError,OSError,KeyError) as ex:p.exit(1,'중단: '+str(ex)+'\n')
if __name__=='__main__':main()
