#!/usr/bin/env python3
"""사람별 MANO 형상(베타) 프로필: 만들기·검증·찾기.

프로필 형식 kaist-mano-shape-profile/v1 (JSON)
  {"schema": "kaist-mano-shape-profile/v1", "profile_id": "W07", "betas": {"right": [10개], "left": [10개]?},
   "mano_model": {"MANO_RIGHT.pkl": sha256, ...}, "created": ..., "method": ..., "sources": {...}}
- 18_fit_hands.py --hand-profile <ID 또는 경로> (또는 episodes.all.json의 "hand_profile")로 쓰면
  공유 형상 보정 대신 이 베타를 고정합니다(알고리즘 mano-geometric-profile-shape-v2, v7 손가락 설정).
- 프로필 파일의 sha256이 실행 설정·계약에 고정되므로, 같은 ID라도 내용을 바꾸면 기존 결과는 새 시도로 다시 돌려야 합니다.
  내용을 고칠 때는 새 ID(예: W07_v2)로 만드는 것을 권합니다.
- ID로 찾는 위치: 환경변수 KAIST_HAND_PROFILES, 없으면 <pipeline_root>/hand_profiles/<ID>.json

만들기: python3 hand_profile.py build --id W07 --mano-dir <Hamba/_DATA/data/mano> --out W07.json <shared_shape.json>...
  (각 shared_shape.json은 --shape_only 형상 보정 결과. 여러 개면 베타를 평균합니다.)
확인:   python3 hand_profile.py show W07.json
"""
import argparse, datetime, hashlib, json, os, re, sys, time
from pathlib import Path

SCHEMA = 'kaist-mano-shape-profile/v1'
ALGORITHM = 'mano-geometric-profile-shape-v3'  # v8 설정(손가락·재검출·삼각측량·깊이)과 함께 쓰는 프로필 피팅 (v1 = v6, v2 = v7)
ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$')


def sha256(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda: f.read(1 << 20), b''): h.update(block)
    return h.hexdigest()


def profile_root(pipeline_root):
    env = os.environ.get('KAIST_HAND_PROFILES')
    return Path(env) if env else Path(pipeline_root) / 'hand_profiles'


def validate(profile, sides=('right',)):
    if profile.get('schema') != SCHEMA: raise ValueError(f'지원하지 않는 프로필 형식: {profile.get("schema")!r} (필요: {SCHEMA})')
    if not ID.fullmatch(str(profile.get('profile_id', ''))): raise ValueError('프로필 ID가 없거나 형식이 잘못됐습니다.')
    if not profile.get('mano_model'): raise ValueError('프로필에 MANO 모델 해시(mano_model)가 없습니다.')
    for side in sides:
        b = (profile.get('betas') or {}).get(side)
        if not isinstance(b, list) or len(b) != 10 or not all(isinstance(v, (int, float)) and abs(v) < 10 for v in b):
            raise ValueError(f'프로필 {profile.get("profile_id")}에 {side} 손 베타 10개가 없습니다.')
    return profile


def load(path, sides=('right',)):
    """(프로필 dict, sha256) - 형식만 검사합니다. MANO 모델 일치는 피팅 스크립트가 실제 모델 파일로 검사합니다."""
    path = Path(path)
    return validate(json.loads(path.read_text()), sides), sha256(path)


def resolve(ref, pipeline_root):
    """ID 또는 파일 경로 → 프로필 파일 경로."""
    ref = str(ref)
    p = Path(ref).expanduser()
    if p.suffix == '.json' or os.sep in ref:
        if not p.is_file(): raise ValueError(f'손 형상 프로필 파일이 없습니다: {p}')
        return p.resolve()
    if not ID.fullmatch(ref): raise ValueError(f'잘못된 손 형상 프로필 ID: {ref!r}')
    p = profile_root(pipeline_root) / f'{ref}.json'
    if not p.is_file(): raise ValueError(f'손 형상 프로필 {ref}을(를) 찾지 못했습니다: {p}')
    return p.resolve()


def build(pid, sources, mano_dir, out, side='right', note='', measurement=None):
    """measurement: 형상 보정에 쓴 코드·설정 기록(선택). 프로필에 그대로 남깁니다."""
    if not ID.fullmatch(pid): raise ValueError(f'잘못된 프로필 ID: {pid!r}')
    rows, record = [], {}
    for s in sources:
        s = Path(s)
        d = json.loads(s.read_text())['diagnostics'][side]
        b = d['betas'][0] if isinstance(d['betas'][0], list) else d['betas']
        if len(b) != 10: raise ValueError(f'베타가 10개가 아닙니다: {s}')
        rows.append([float(v) for v in b])
        pkl = s.with_suffix('.pkl')
        record[str(s)] = {'betas': rows[-1], 'frames': d.get('frames'), 'json_sha256': sha256(s),
                          'pkl_sha256': sha256(pkl) if pkl.exists() else None}
    if not rows: raise ValueError('형상 보정 결과(shared_shape.json)를 하나 이상 주세요.')
    mean = [round(sum(c) / len(c), 6) for c in zip(*rows)]
    models = {p.name: sha256(p) for p in sorted(Path(mano_dir).glob('*.pkl'))}
    if not models: raise ValueError(f'MANO 모델 파일(*.pkl)이 없습니다: {mano_dir}')
    profile = {'schema': SCHEMA, 'profile_id': pid, 'betas': {side: mean}, 'mano_model': models,
               'created': datetime.datetime.now().astimezone().isoformat(timespec='seconds'),
               'method': f'{len(rows)}개 에피소드의 공유 형상 보정(--shape_only) 베타 평균', 'note': note, 'sources': record}
    if measurement: profile['measurement'] = measurement
    validate(profile, (side,))
    out = Path(out)
    if out.exists(): raise ValueError(f'이미 있는 파일은 덮어쓰지 않습니다(새 ID로 만드세요): {out}')
    out.parent.mkdir(parents=True, exist_ok=True)
    # 같은 ID를 두 곳에서 동시에 만들어도 덮어쓰지 않도록 임시 파일을 하드 링크로 붙입니다(이미 있으면 실패).
    tmp = out.with_name(f'.{out.name}.{os.getpid()}.{time.time_ns()}.tmp')
    try:
        tmp.write_text(json.dumps(profile, ensure_ascii=False, indent=1) + '\n')
        try: os.link(tmp, out)
        except FileExistsError: raise ValueError(f'이미 있는 파일은 덮어쓰지 않습니다(새 ID로 만드세요): {out}') from None
    finally:
        tmp.unlink(missing_ok=True)
    return profile, sha256(out)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest='cmd', required=True)
    b = sub.add_parser('build'); b.add_argument('sources', nargs='+'); b.add_argument('--id', required=True)
    b.add_argument('--mano-dir', required=True); b.add_argument('--out', required=True); b.add_argument('--side', default='right', choices=['right', 'left'])
    b.add_argument('--note', default='')
    s = sub.add_parser('show'); s.add_argument('path')
    a = p.parse_args()
    try:
        if a.cmd == 'build':
            profile, digest = build(a.id, a.sources, a.mano_dir, a.out, a.side, a.note)
            print(f'프로필 저장: {a.out}\nID {profile["profile_id"]} · sha256 {digest}\n{a.side} 베타 {profile["betas"][a.side]}')
        else:
            profile, digest = load(a.path, ())
            print(json.dumps({'profile_id': profile['profile_id'], 'sha256': digest, 'betas': profile['betas'],
                              'mano_model': profile['mano_model'], 'created': profile.get('created'), 'method': profile.get('method')}, ensure_ascii=False, indent=1))
    except (ValueError, OSError, KeyError) as e:
        p.exit(1, f'중단: {e}\n')


if __name__ == '__main__':
    main()
