"""GPU 동시 실행 슬롯 (기본 2개).

같은 gpu_4090_a.lock 파일 하나로 슬롯을 나눕니다. compose가 이 파일 하나만 호스트 Worker와
컨테이너에 공유하므로 새 파일을 만들지 않습니다.
- 슬롯 0: 파일 전체 flock (예전 Worker 코드와 같은 방식 → 예전 코드와도 서로 막힘)
- 슬롯 1..: 같은 파일의 바이트 범위 OFD 잠금 (flock과 독립)
두 잠금 모두 열린 파일(fd)에 붙으므로 pass_fds로 넘긴 하위 프로세스가 살아 있는 동안 유지됩니다.

SAM3 전용 제한: RTX 4090(24GB)에서는 SAM3 두 개가 함께 돌면 메모리가 모자라므로 SAM3 단계는 한 번에 1개만 실행합니다.
같은 파일의 SAM_BYTE.. 바이트 잠금을 쓰고, GPU 슬롯보다 먼저 잡습니다. 그래서 순서를 기다리는 SAM3는 GPU 슬롯을
차지하지 않고, 남은 슬롯에서 다른 단계(추적·3D 복원·MANO)가 SAM3 1개와 함께 실행됩니다.

RTX 5090(32GB)은 SAM3 2개까지 함께 실행하되, 검수 화면의 마법봉(SAM3.1, 약 7GB)이 그 두 자리 중 마지막 자리를 함께 씁니다.
SAM3 2개(약 25GB)가 돌면 마법봉이 들어갈 메모리가 없기 때문입니다. 마법봉이 떠 있는 동안은 SAM3가 1개만 실행되고,
SAM3 2개가 도는 중에 마법봉을 누르면 '마법봉 대기' 표시(WANT_BYTE 잠금)가 켜져 다음 SAM3가 마지막 자리를 비워 둡니다.
"""
import errno,fcntl,os,struct,subprocess,time
from pathlib import Path
F_OFD_SETLK=getattr(fcntl,'F_OFD_SETLK',37)
def slot_count():
    try:return max(1,int(os.environ.get('KAIST_GPU_SLOTS','2')))
    except ValueError:return 2
SAM_BYTE=1024   # GPU 슬롯이 쓰는 바이트(0..)와 겹치지 않는 자리
WANT_BYTE=2048  # 마법봉이 SAM 자리를 기다리는 중이라는 표시
def _byte_lock(f,byte):fcntl.fcntl(f,F_OFD_SETLK,struct.pack('hhqqi4x',fcntl.F_WRLCK,os.SEEK_SET,byte,1,0))
def _try(path,slot,sam=False):
    f=Path(path).open('a')
    try:
        if sam:_byte_lock(f,slot if slot==WANT_BYTE else SAM_BYTE+slot)
        elif slot==0:fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)
        else:_byte_lock(f,slot-1)
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
def gpu_name(uuid=None):
    """Worker가 쓰는 GPU 이름(예: 'NVIDIA GeForce RTX 4090'). 조회하지 못하면 ''."""
    try:
        rows=[[v.strip() for v in line.split(',')] for line in subprocess.check_output(
            ['nvidia-smi','--query-gpu=uuid,name','--format=csv,noheader'],text=True,timeout=10).splitlines() if ',' in line]
    except (OSError,subprocess.SubprocessError):return ''
    return next((n for u,n in rows if u==uuid),rows[0][1] if rows else '')
def sam_limit(uuid=None):
    """SAM3 단계 동시 실행 수. KAIST_SAM3_SLOTS(0=제한 없음)가 우선이고, 없으면 RTX 4090은 1개, RTX 5090은 2개(마지막 자리는 마법봉과 공유)입니다. None=제한 없음."""
    value=os.environ.get('KAIST_SAM3_SLOTS','').strip()
    if value:
        try:return max(0,int(value)) or None
        except ValueError:pass
    name=gpu_name(uuid)
    return 1 if '4090' in name else 2 if '5090' in name else None
def acquire_sam(path,uuid=None,poll=3.0,waiting=None):
    """SAM3 실행 순서를 잡습니다(제한이 없는 GPU면 None). 다른 SAM3가 돌고 있으면 끝날 때까지 기다리며, 처음 기다릴 때 waiting()을 부릅니다."""
    limit=sam_limit(uuid)
    if not limit:return None
    while True:
        # 마법봉이 자리를 기다리는 중이면 마법봉과 함께 쓰는 마지막 자리는 비워 둡니다.
        for i in range(limit-1 if limit>1 and wand_wanted(path) else limit):
            f=_try(path,i,sam=True)
            if f:return f
        if waiting:waiting();waiting=None
        time.sleep(poll)
def acquire_wand(path,uuid=None):
    """마법봉용 SAM 자리. (자리가 필요한 GPU인지, 잡은 잠금 또는 None)을 돌려줍니다. SAM3를 2개 이상 함께 돌리는 GPU에서만 필요합니다."""
    limit=sam_limit(uuid)
    if not limit or limit<2:return False,None
    return True,_try(path,limit-1,sam=True)
def want_wand(path):
    """'마법봉 대기' 표시를 켭니다. 돌려준 파일을 닫으면 꺼집니다."""
    return _try(path,WANT_BYTE,sam=True)
def wand_wanted(path):
    f=_try(path,WANT_BYTE,sam=True)
    if f:f.close()
    return f is None
