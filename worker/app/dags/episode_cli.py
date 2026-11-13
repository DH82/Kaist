#!/usr/bin/env python3
"""원본 단계 스크립트(07_track, 10_prepare_reconstruction, 10_reconstruct, 18_fit_hands 등)를
에피소드 단위 잠금으로 실행합니다.

원본 스크립트는 `from mask_review import lock`으로 프로젝트 전체 잠금(99_history/pipeline.lock)을
비차단으로 잡습니다. 그래서 MANO처럼 오래 걸리는 단계가 하나 돌면 다른 에피소드의 추적·복원·MANO·검수가
"추론 또는 다른 작업이 실행 중입니다"로 바로 실패했습니다. 여기서는 스크립트를 import 하기 전에
mask_review.lock을 99_history/locks/<episode>.lock 잠금으로 바꿉니다. 이 파일은
review_backend.episode_lock()·sam3_episode.py와 같아서, 같은 에피소드끼리는 그대로 서로 막습니다.

사용: episode_cli.py <episode> <script.py> [script args...]
"""
import contextlib,fcntl,re,runpy,sys
from pathlib import Path

def main(argv):
    episode,script,args=argv[0],Path(argv[1]).resolve(),argv[2:]
    if not re.fullmatch(r'ep_[A-Za-z0-9_]+',episode):raise SystemExit('Invalid episode id')
    # 원본 스크립트 폴더를 먼저 찾도록 합니다(직접 실행할 때와 같은 import).
    sys.path.insert(0,str(script.parent))
    import mask_review
    held={'depth':0}

    @contextlib.contextmanager
    def episode_lock(project):
        # 같은 프로세스 안에서 다시 잡으면(중첩) 그대로 통과합니다.
        if held['depth']:
            held['depth']+=1
            try:yield
            finally:held['depth']-=1
            return
        folder=Path(project)/'99_history/locks';folder.mkdir(parents=True,exist_ok=True)
        with (folder/f'{episode}.lock').open('a') as f:
            # 같은 에피소드의 검수 저장(수 초) 등이 끝날 때까지만 기다립니다.
            fcntl.flock(f,fcntl.LOCK_EX)
            held['depth']=1
            try:yield
            finally:held['depth']=0

    mask_review.lock=episode_lock
    # 진행률 표시(pipeline_progress.py, 같은 dags 폴더)가 있으면 함께 켭니다.
    try:from pipeline_progress import install
    except ImportError:install=None
    # 진행률 표시는 부가 기능이라 실패해도 계산은 그대로 진행합니다.
    try:install and install()
    except Exception as ex:print('진행률 표시 비활성:',ex,file=sys.stderr)
    sys.argv=[str(script),*args]
    runpy.run_path(str(script),run_name='__main__')

if __name__=='__main__':main(sys.argv[1:])
