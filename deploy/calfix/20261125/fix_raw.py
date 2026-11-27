"""11-25 10:41·14:41 보정 회차 RAW의 extrinsics.json 이동(T)을 1/s 로 고친다. s 는 교체본 record.json 값 그대로.
원본은 control/calfix_20261125/raw_backup/<ep>/extrinsics.json 에 먼저 백업. 이미 고친 RAW 는 건너뜀."""
import json, sys, os, hashlib, shutil, datetime
NAS = '/mnt/kaist-data'; ROOT = f'{NAS}/control/calfix_20261125'
sha = lambda p: hashlib.sha256(open(p, 'rb').read()).hexdigest()
apply = '--apply' in sys.argv
log = []
for ep in [l.strip() for l in open(sys.argv[1]) if l.strip()]:
    raw = f'{NAS}/rgbd_raw/{ep}/extrinsics.json'
    rec = json.load(open(f'{ROOT}/fixes/{ep}/fps15/record.json'))
    s = rec['scale_s']; cur = sha(raw); bak = f'{ROOT}/raw_backup/{ep}/extrinsics.json'
    if cur != rec['raw_extrinsics_sha256']:
        if os.path.exists(bak) and sha(bak) == rec['raw_extrinsics_sha256']:
            print(ep, '이미 수정'); continue
        print(ep, '건너뜀: 현재 파일이 원본 해시와 다름'); continue
    d = json.load(open(raw))
    for c, t in d['transforms'].items():
        t['T'] = [[v / s for v in row] for row in t['T']]
    if not apply:
        print(ep, f'수정 예정 s={s:.6f}'); continue
    os.makedirs(os.path.dirname(bak), exist_ok=True)
    if not os.path.exists(bak): shutil.copy2(raw, bak)
    assert sha(bak) == cur
    tmp = raw + '.calfix-tmp'
    with open(tmp, 'w') as f: json.dump(d, f, indent=2)
    os.chmod(tmp, 0o777); os.replace(tmp, raw)
    new = sha(raw); log.append({'raw': ep, 'scale_s': s, 'old_sha256': cur, 'new_sha256': new})
    print(ep, '수정', f's={s:.6f}', flush=True)
if apply and log:
    out = f'{ROOT}/raw_updated.json'
    old = json.load(open(out)) if os.path.exists(out) else {'note': 'rgbd_raw extrinsics.json 이동(T)을 1/s 로 수정. 원본: raw_backup/<ep>/extrinsics.json', 'items': []}
    old['items'] += [dict(x, at=datetime.datetime.now().astimezone().isoformat()) for x in log]
    json.dump(old, open(out, 'w'), ensure_ascii=False, indent=1)
