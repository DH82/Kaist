#!/usr/bin/env python3
"""11-25 촬영분 한정: 11-25 10:41·14:41 보정(보드 크기 설정 오류 — 카메라 간 거리 0.867배·0.932배)으로
전처리된 에피소드의 03_prepared/<ep>/calibrate.pkl · world_reorient_M.npy 를 교체본으로 바꾼다.
교체본 = 그 RAW 자체 보정의 회전은 그대로, 이동만 1/배율 (gen_calfix.py, sessions.json 참고).
11-25 16:29 보정(23a443a9)으로 찍힌 촬영은 정상이라 대상이 아니다.

색·깊이·마스크·추적은 그대로. 3D 복원 → MANO 는 바뀐 파일로 진행된다.
기본은 점검만(dry-run). 실제 교체는 --apply, 되돌리기는 --revert.

  python3 /mnt/kaist-data/control/calfix_20261125/apply_calfix.py            # 점검
  python3 /mnt/kaist-data/control/calfix_20261125/apply_calfix.py --apply    # 교체
  python3 /mnt/kaist-data/control/calfix_20261125/apply_calfix.py --revert   # 되돌리기

교체 조건(하나라도 어긋나면 건너뜀):
  - 마스크 승인(04_masks/approved/<ep>) 완료 → SAM3 시작 시 검사를 이미 지남
  - 3D 복원 미시작(06_reconstruction/<ep>/_resume 없음)
  - 현재 파일이 그 RAW 의 원래 보정값으로 만든 것과 바이트 단위 일치
  - 전처리 fps 와 같은 fps 의 교체본이 있음
"""
import argparse, datetime, hashlib, json, os, re, shutil, socket, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
FILES = ('calibrate.pkl', 'world_reorient_M.npy')
BACKUP = '99_history/calfix_20261125'


def sha(p):
    h = hashlib.sha256()
    with open(p, 'rb') as f:
        for b in iter(lambda: f.read(1 << 20), b''):
            h.update(b)
    return h.hexdigest()


def raw_name(ep):
    m = re.match(r'^(ep_\d{8}_\d{6})(_\d+)?$', ep)
    return m.group(1) if m else None


def prepared_fps(case):
    try:
        return float(json.loads((case / 'preparation.json').read_text())['contract']['settings']['fps'])
    except Exception:
        return 30.0


def find_work(arg):
    if arg:
        return [Path(arg).expanduser()]
    found = []
    for p in sorted((Path.home() / 'kaist_work').glob('pc*')):
        if (p / '03_prepared').is_dir():
            found.append(p)
    return found


def replace(src, dst):
    tmp = dst.with_name('.' + dst.name + '.calfix-tmp')
    shutil.copyfile(src, tmp)
    os.replace(tmp, dst)


def run(work, mode):
    rows = []
    for case in sorted((work / '03_prepared').glob('ep_20261125_*')):
        ep = case.name
        raw = raw_name(ep)
        if not raw or not case.is_dir():
            continue
        fps = prepared_fps(case)
        # 동기화 기준별 교체본: fps15(host_time_s, 11-27 이전 전처리) / fps15_hw(카메라 시계, 11-27 PC2 커밋 e86b5ab 이후)
        cands = [ROOT / 'fixes' / raw / f'fps{fps:g}{v}' for v in ('', '_hw')]
        cands = [d for d in cands if (d / 'record.json').exists()]
        if not cands:
            if (ROOT / 'fixes' / raw).exists():
                rows.append((ep, '건너뜀', f'{fps:g}fps 교체본 없음'))
            continue  # 대상 회차가 아님(16:29 정상 보정으로 찍힌 촬영)
        bak = work / BACKUP / ep
        cur = {n: sha(case / n) for n in FILES}
        recs = [(d, json.loads((d / 'record.json').read_text())) for d in cands]
        if mode == 'revert' and (bak / 'record.json').exists():
            saved = json.loads((bak / 'record.json').read_text())
            recs = [x for x in recs if x[1]['new'] == saved['new']] or recs
        # 현재 파일이 이미 교체본이거나, 그 교체본의 원래 파일과 같은 쪽을 고른다.
        fix, rec = next((x for x in recs if cur in (x[1]['new'], x[1]['expected_current'])), recs[0])

        if mode == 'revert':
            if not (bak / 'calibrate.pkl').exists():
                continue
            if (work / '06_reconstruction' / ep / '_resume').exists():
                rows.append((ep, '건너뜀', '3D 복원이 이미 시작됨 — 되돌리면 3D 계약과 어긋남'))
                continue
            for n in FILES:
                replace(bak / n, case / n)
            rows.append((ep, '되돌림', '원래 파일 복원'))
            continue

        if cur == rec['new']:
            rows.append((ep, '이미 교체', ''))
            continue
        if cur != rec['expected_current']:
            rows.append((ep, '건너뜀', '현재 파일이 예상(원래 보정값)과 다름 — 확인 필요'))
            continue
        if not (work / '04_masks' / 'approved' / ep).exists():
            rows.append((ep, '대기', '마스크 승인 전 — 승인 후 다시 실행'))
            continue
        if (work / '06_reconstruction' / ep / '_resume').exists():
            rows.append((ep, '건너뜀', '3D 복원이 이미 시작됨(틀린 보정값으로 진행/실패) — 전처리부터 다시 필요'))
            continue
        for n in FILES:
            if sha(fix / n) != rec['new'][n]:
                rows.append((ep, '오류', f'교체본 {n} 해시 불일치'))
                break
        else:
            if mode == 'dry':
                rows.append((ep, '교체 예정', ''))
                continue
            # 교체 직전에 3D 시작 여부를 한 번 더 확인
            if (work / '06_reconstruction' / ep / '_resume').exists():
                rows.append((ep, '건너뜀', '방금 3D 복원이 시작됨'))
                continue
            try:
                bak.mkdir(parents=True, exist_ok=True)
                for n in FILES:
                    if not (bak / n).exists():
                        shutil.copy2(case / n, bak / n)
                (bak / 'record.json').write_text(json.dumps(rec, ensure_ascii=False, indent=1))
                for n in FILES:
                    replace(fix / n, case / n)
            except OSError as e:
                rows.append((ep, '오류', f'파일 쓰기 실패: {e}'))
                continue
            if {n: sha(case / n) for n in FILES} != rec['new']:
                for n in FILES:
                    replace(bak / n, case / n)
                rows.append((ep, '오류', '교체 후 확인 실패 — 원래 파일로 되돌림'))
                continue
            rows.append((ep, '교체', ''))
    return rows


def main():
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group()
    g.add_argument('--apply', action='store_true')
    g.add_argument('--revert', action='store_true')
    ap.add_argument('--work', help='작업 폴더(기본: ~/kaist_work/pc*)')
    ap.add_argument('--quiet', action='store_true', help='바뀐 것(교체·되돌림·오류)만 출력·기록(업데이터 정기 실행용)')
    a = ap.parse_args()
    mode = 'apply' if a.apply else 'revert' if a.revert else 'dry'
    works = find_work(a.work)
    if not works:
        sys.exit('작업 폴더(~/kaist_work/pcN)를 찾지 못했습니다. --work 로 지정하세요.')
    report = {'host': socket.gethostname(), 'mode': mode, 'time': datetime.datetime.now().astimezone().isoformat(), 'works': {}}
    changed = False
    for w in works:
        rows = run(w, mode)
        summary = {}
        for _, st, _ in rows:
            summary[st] = summary.get(st, 0) + 1
        shown = [r for r in rows if r[1] in ('교체', '되돌림', '오류')] if a.quiet else rows
        changed |= any(r[1] in ('교체', '되돌림', '오류') for r in rows)
        if shown or not a.quiet:
            print(f'== {w}  ({mode})')
            for ep, st, msg in shown:
                print(f'  {ep:24s} {st:8s} {msg}')
            print('  합계:', summary or '대상 없음')
        report['works'][str(w)] = {'summary': summary, 'rows': rows}
    if a.quiet and not changed:
        return
    try:
        out = ROOT / 'applied' / f"{socket.gethostname()}-{'-'.join(w.name for w in works)}-{mode}-{datetime.datetime.now():%H%M%S}.json"
        out.write_text(json.dumps(report, ensure_ascii=False, indent=1))
    except OSError:
        pass


if __name__ == '__main__':
    main()
