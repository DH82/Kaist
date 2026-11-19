#!/usr/bin/env python3
"""05_sam3.py와 같은 처리(pipeline.init → targets → sam3)를 에피소드 단위 잠금으로 실행합니다.

원본 pipeline.main('05')는 99_history/pipeline.lock(프로젝트 전체)을 잡아서, 한 에피소드의 SAM3가
도는 동안 다른 에피소드의 SAM3와 마스크 검수가 모두 실패했습니다. 잠금 파일은
review_backend.episode_lock()과 같은 99_history/locks/<episode>.lock 입니다.
사용: sam3_episode.py <scripts_dir> <03_prepared/episode> <project> [--new-attempt] [--object-tracked] [--hand-points <json>]

처리는 sam3_fast.sam3()(모델 1회 로드, --object-tracked면 기준선 손만)로 합니다. 원본 파이프라인 코드가
sam3_fast가 작성된 버전과 다르면 원본 pipeline.sam3()로 그대로 실행합니다.
"""
import fcntl,subprocess,sys
from pathlib import Path
def main(argv):
    scripts,prepared,project=Path(argv[0]),Path(argv[1]),Path(argv[2]);new='--new-attempt' in argv[3:];tracked='--object-tracked' in argv[3:]
    # --hand-points <json 파일>: 손 포인트 방식(텍스트 기준선·손 ROI 대신 첫 프레임 포인트에서 손 추적)
    import json
    hand=json.loads(Path(argv[argv.index('--hand-points')+1]).read_text()) if '--hand-points' in argv[3:] else None
    sys.path.insert(0,str(scripts))
    from pipeline_progress import install
    install()
    import pipeline
    try:from pipeline_progress import install
    except ImportError:install=None
    # 진행률 표시는 부가 기능이라 실패해도 계산은 그대로 진행합니다.
    try:install and install()
    except Exception as ex:print('진행률 표시 비활성:',ex,file=sys.stderr)
    try:
        import sam3_fast
        _,source,_=pipeline.runtime()
        fast=sam3_fast.compatible(scripts,source)
        if not fast and hand:raise ValueError('원본 파이프라인 코드가 바뀌어 손 포인트 방식을 쓸 수 없습니다. Worker 업데이트를 확인하세요.')
        if not fast:print('원본 파이프라인 코드가 바뀌어 기존 SAM3 처리(카메라별 모델 로드)로 실행합니다.',flush=True)
        p=project.expanduser().resolve();pipeline.init(p)
        locks=p/'99_history/locks';locks.mkdir(parents=True,exist_ok=True)
        for ep in pipeline.targets(p,[prepared],'05'):
            with (locks/f'{ep}.lock').open('a') as lock:
                # 같은 에피소드의 검수 저장이 끝날 때까지만 기다립니다.
                fcntl.flock(lock,fcntl.LOCK_EX)
                if fast:sam3_fast.sam3(pipeline,p,ep,new,object_tracked=tracked,hand_points=hand)
                else:pipeline.sam3(p,ep,new)
        return 0
    except (ValueError,OSError,KeyError,subprocess.SubprocessError) as ex:
        print('중단:',ex,file=sys.stderr);return 1
if __name__=='__main__':raise SystemExit(main(sys.argv[1:]))
