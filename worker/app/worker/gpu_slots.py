"""GPU 동시 실행 슬롯 (기본 2개).

같은 gpu_4090_a.lock 파일 하나로 슬롯을 나눕니다. compose가 이 파일 하나만 호스트 Worker와
컨테이너에 공유하므로 새 파일을 만들지 않습니다.
- 슬롯 0: 파일 전체 flock (예전 Worker 코드와 같은 방식 → 예전 코드와도 서로 막힘)
- 슬롯 1..: 같은 파일의 바이트 범위 OFD 잠금 (flock과 독립)
두 잠금 모두 열린 파일(fd)에 붙으므로 pass_fds로 넘긴 하위 프로세스가 살아 있는 동안 유지됩니다.
"""
import errno,fcntl,os,struct,time
from pathlib import Path
F_OFD_SETLK=getattr(fcntl,'F_OFD_SETLK',37)
def slot_count():
    try:return max(1,int(os.environ.get('KAIST_GPU_SLOTS','2')))
    except ValueError:return 2
def _try(path,slot):
    f=Path(path).open('a')
    try:
        if slot==0:fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)
        else:fcntl.fcntl(f,F_OFD_SETLK,struct.pack('hhqqi4x',fcntl.F_WRLCK,os.SEEK_SET,slot-1,1,0))
        return f
    except OSError as e:
        f.close()
        if isinstance(e,BlockingIOError) or e.errno in (errno.EAGAIN,errno.EACCES):return None
        raise
def acquire(path,block=True,poll=3.0):
    """빈 슬롯의 열린 파일 객체와 번호를 돌려줍니다. block=False이고 빈 슬롯이 없으면 (None,None)."""
    while True:
        for slot in range(slot_count()):
            f=_try(path,slot)
            if f:return f,slot
        if not block:return None,None
        time.sleep(poll)
