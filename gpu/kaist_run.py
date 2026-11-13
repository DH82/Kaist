#!/usr/bin/env python3
"""에피소드 하나를 RAW부터 NAS 저장까지 한 번에 실행합니다 (컨테이너 안).

Airflow DAG와 같은 12단계를 같은 Worker 작업 방식(runner.py: 작업 기록·GPU lock·재개)으로 순서대로 실행합니다.
사람 검수가 필요한 두 지점(마스크 승인, 추적 예외 승인)은 자동 승인하지 않습니다.
검수 대기에서 멈추면 UI에서 승인한 뒤 같은 명령에 --resume을 붙여 이어서 실행합니다.

  kaist-run ep_20261112_062904 --prompt "white towel"
  kaist-run ep_20261112_062904 --resume            # 마지막 실행 이어서
  kaist-run ep_20261112_062904 --resume --wait     # 검수 대기 중이면 승인될 때까지 기다림
"""
import argparse, hashlib, json, os, re, sys, time, uuid
from datetime import datetime
from pathlib import Path

W = Path(os.environ['KAIST_WORKER_ROOT'])
sys.path.insert(0, str(W / 'worker'))
import runner  # noqa: E402  Worker와 같은 작업 폴더·상태·lock 규칙

STAGES = ['prepare_raw', 'sam_settings_ready', 'sam3', 'mask_approved', 'preflight', 'track',
          'tracking_approved', 'prepare_reconstruction', 'reconstruct', 'hands', 'validate', 'publish']
LABEL = {'prepare_raw': 'RAW 복사·QC·전처리', 'sam_settings_ready': 'SAM3 설정 확정', 'sam3': 'SAM3 분할',
         'mask_approved': '마스크 승인 대기', 'preflight': '추적 사전 점검', 'track': 'CoTracker 추적',
         'tracking_approved': '추적 승인 대기', 'prepare_reconstruction': '3D 복원 입력 준비',
         'reconstruct': '3D 복원 (TRELLIS·정합)', 'hands': 'HaMBA·MANO 손 피팅', 'validate': '결과 검사',
         'publish': 'NAS 저장'}
GATE_HELP = {
    'sam_settings_ready': 'SAM3 프롬프트가 확정되지 않았습니다. --prompt "물체 이름"으로 새로 실행하세요.',
    'mask_approved': '마스크 검수가 필요합니다. 작업자 UI의 검수 화면에서 마스크를 승인하세요.',
    'tracking_approved': '추적 자동 QC에서 예외 구간이 나왔습니다. 작업자 UI의 추적 검수에서 승인하세요.',
}
RUNS = W / 'worker/cli_runs'


def log(msg):
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def follow(path, offset):
    try:
        with open(path, 'rb') as f:
            f.seek(offset)
            data = f.read()
    except FileNotFoundError:
        return offset
    if data:
        sys.stdout.write(data.decode(errors='replace'))
        sys.stdout.flush()
    return offset + len(data)


def run_stage(req, quiet):
    p = runner.folder(req)
    st = runner.status(p)
    if st['state'] == 'success':
        log(f"✔ {LABEL[req['stage']]} (이미 완료)")
        return 'success'
    if st['state'] in ('failed', 'interrupted'):
        return st['state']
    offset = (p / 'job.log').stat().st_size if (p / 'job.log').exists() else 0
    log(f"▶ {LABEL[req['stage']]}")
    st = runner.start(req)
    while st['state'] in ('queued', 'running'):
        time.sleep(3)
        if not quiet:
            offset = follow(p / 'job.log', offset)
        st = runner.status(p)
    if not quiet:
        follow(p / 'job.log', offset)
    return st['state']


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('episode', help='NAS rgbd_raw 아래 RAW 에피소드 이름 (ep_…)')
    ap.add_argument('--prompt', help='SAM3 물체 프롬프트 (새 실행에 필요)')
    ap.add_argument('--resume', action='store_true', help='이 에피소드의 마지막 실행을 이어서 진행')
    ap.add_argument('--run-id', help='이어서 실행할 run id (--resume 대신 직접 지정)')
    ap.add_argument('--output-episode', help='같은 RAW를 새 이름으로 다시 처리할 때 (예: ep_X_2)')
    ap.add_argument('--wait', action='store_true', help='검수 대기에서 종료하지 않고 승인될 때까지 30초마다 확인')
    ap.add_argument('--retry', action='store_true', help='실패/중단된 단계를 다시 실행')
    ap.add_argument('--until', choices=STAGES, help='이 단계까지만 실행')
    ap.add_argument('--quiet', action='store_true', help='단계 로그를 화면에 출력하지 않음')
    a = ap.parse_args()

    for name in filter(None, [a.episode, a.output_episode]):
        if not re.fullmatch(r'ep_[A-Za-z0-9_]+', name):
            ap.error('에피소드 이름은 ep_로 시작해야 합니다: ' + name)
    cfg = json.loads((W / 'config/gpu_4090_a.json').read_text())
    if not (Path(cfg['raw_root']) / a.episode).is_dir():
        ap.error(f"RAW 에피소드가 없습니다: {Path(cfg['raw_root']) / a.episode}")
    out_ep = a.output_episode or a.episode
    RUNS.mkdir(parents=True, exist_ok=True)
    record = RUNS / f'{out_ep}.json'

    if a.resume or a.run_id:
        if a.run_id:
            base = json.loads(record.read_text()) if record.exists() else {}
            if base.get('run_id') != a.run_id:
                if not a.prompt:
                    ap.error('기록에 없는 --run-id는 --prompt도 함께 지정하세요.')
                base = {'run_id': a.run_id, 'prompt': a.prompt}
        else:
            if not record.exists():
                ap.error(f'{out_ep}의 이전 실행 기록이 없습니다. --prompt로 새로 실행하세요.')
            base = json.loads(record.read_text())
        run_id, prompt = base['run_id'], base['prompt']
        if a.prompt and a.prompt != prompt:
            ap.error('이어서 실행할 때는 프롬프트를 바꿀 수 없습니다. 바꾸려면 --resume 없이 새로 실행하세요.')
    else:
        if not a.prompt or not a.prompt.strip():
            ap.error('새 실행에는 --prompt "물체 이름"이 필요합니다.')
        prompt = a.prompt.strip()
        run_id = f"cli_{datetime.now():%Y%m%dT%H%M%S}_{uuid.uuid4().hex[:6]}"
    record.write_text(json.dumps({'run_id': run_id, 'prompt': prompt, 'raw_episode': a.episode,
                                  'updated': datetime.now().astimezone().isoformat()}, ensure_ascii=False, indent=2))

    log(f"에피소드 {out_ep} (RAW {a.episode}) · run {run_id} · 프롬프트 '{prompt}'")
    log(f"작업 폴더 {cfg['project_dir']} → 결과 {cfg['nas_mount']}/{cfg['nas_results_subdir']}/{out_ep}/")
    stages = STAGES[:STAGES.index(a.until) + 1] if a.until else STAGES
    for stage in stages:
        # 요청 내용은 매번 같아야 합니다 (runner가 같은 작업의 요청 변경을 거부).
        req = {'stage': stage, 'episode': out_ep, 'run_id': run_id, 'sam_prompt': prompt,
               'sam_scene': '2d', 'sam_confirmed': True}
        if out_ep != a.episode:
            req['raw_episode'] = a.episode
        while True:
            state = run_stage(req, a.quiet)
            if state in ('failed', 'interrupted') and a.retry:
                log(f"↻ {LABEL[stage]} 다시 실행 (이전 기록은 attempts/에 보관)")
                runner.retry(req)
                a.retry = False
                continue
            if state == 'waiting' and a.wait:
                log(f"… {LABEL[stage]}: {GATE_HELP.get(stage, '대기 중')} (30초 후 다시 확인)")
                time.sleep(30)
                continue
            break
        if state == 'success':
            log(f"✔ {LABEL[stage]}")
            continue
        p = runner.folder(req)
        if state == 'waiting':
            log(f"⏸ {LABEL[stage]}: {GATE_HELP.get(stage, '대기 중')}")
            log(f"  승인 후 이어서: kaist-run {a.episode}{' --output-episode ' + out_ep if out_ep != a.episode else ''} --resume")
            sys.exit(75)
        log(f"✘ {LABEL[stage]} {state}. 로그: {p / 'job.log'}")
        log(f"  원인을 고친 뒤: kaist-run {a.episode}{' --output-episode ' + out_ep if out_ep != a.episode else ''} --resume --retry")
        sys.exit(1)
    if stages[-1] == 'publish':
        key = hashlib.sha256(run_id.encode()).hexdigest()[:24]  # kaist_runtime과 같은 규칙
        published = Path(cfg['project_dir']) / '_airflow' / out_ep / key / 'published.json'
        dest = json.loads(published.read_text())['destination'] if published.exists() else '(확인 필요)'
        log(f"완료: {dest}")


if __name__ == '__main__':
    main()
