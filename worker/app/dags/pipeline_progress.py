"""Forward native frame/work counters without importing any GPU libraries.

Counters describe the current subtask, never an estimated whole-stage percentage.
"""
import json
import re
import time

PREFIX = 'PIPELINE_PROGRESS '
ANSI = re.compile(r'\x1b\[[0-9;]*[A-Za-z]')


def counter(done, total, label, unit):
    if isinstance(done, bool) or isinstance(total, bool):
        return None
    try:
        d, n = int(done), int(total)
    except (ValueError, TypeError, OverflowError):
        return None
    if d != done or n != total or n <= 0 or not 0 <= d <= n:
        return None
    return dict(done=d, total=n, label=label, unit=unit, percent=round(100*d/n, 1))


def parse(line):
    line = ANSI.sub('', line).strip()
    if line.startswith(PREFIX):
        try:
            value = json.loads(line[len(PREFIX):])
            if value.get('total') is None:
                return {'label': str(value['label']), 'done': None, 'total': None}
            return counter(value['done'], value['total'], str(value['label']), str(value['unit']))
        except (ValueError, KeyError, TypeError):
            return None
    if 'SAM_PROGRESS ' in line:
        try:
            p = json.JSONDecoder().raw_decode(line.split('SAM_PROGRESS ', 1)[1])[0]
            return counter(p['done'], p['frames'], f"SAM3 · {p.get('video', '')} · {p.get('prompt', '')}", '프레임')
        except (ValueError, KeyError, TypeError):
            return None
    # Only progress-bar syntax; avoid treating GPU memory, paths or dates as work.
    match = re.search(r'(?P<label>[^|\r\n]*?):?\s*\d+%\|[^|]*\|\s*(\d+)\s*/\s*(\d+)', line)
    if match:
        return counter(int(match[2]), int(match[3]), match['label'].strip(' :') or '현재 작업', '항목')
    match = re.search(r'^\[export\] \S+ -> cam (\d+): (\d+) frames$', line)
    if match and 0 <= int(match[1]) < 4:
        n = int(match[2])
        return counter((int(match[1])+1)*n, 4*n, '전처리 · RGB·깊이 저장 (카메라 완료 시 갱신)', '카메라 프레임')
    match = re.search(r'^\[sync\] (\d+) aligned frames', line)
    if match:
        return counter(0, 4*int(match[1]), '전처리 · RGB·깊이 저장 (카메라 완료 시 갱신)', '카메라 프레임')
    match = re.search(r'완료 (\d+)/(\d+)(?:구간| \(작업 수 기준\))', line)
    if match:
        return counter(int(match[1]), int(match[2]), '단계 내 작업', '구간')
    return None


def install():
    """Tap live_log's queue before it hides native tqdm/SAM_PROGRESS lines.

    Only this module's queue factory is replaced; subprocesses and global queue
    behavior are unchanged. Native logging and return codes remain authoritative.
    """
    import live_log
    import types
    if getattr(live_log, '_pipeline_progress_installed', False):
        return
    live_log._pipeline_progress_installed = True
    last = [None, 0.0]

    def emit(value):
        if value is None:
            return
        now = time.monotonic()
        key = (value['label'], value.get('total'))
        if key == last[0] and now-last[1] < 1 and value.get('done') != value.get('total'):
            return
        last[:] = [key, now]
        print(PREFIX + json.dumps(value, ensure_ascii=False), flush=True)

    # Completed native steps include validated checkpoint reuse.
    import os
    import resume_steps
    stage = os.environ.get('KAIST_PROGRESS_STAGE')
    names = {'reconstruct': [str(i) for i in range(10, 18)]+['review'],
             'hands': ['18', '19', 'preview']}.get(stage)
    if names:
        original_step = resume_steps.step
        completed = set()
        def step(root, name, *args, **kwargs):
            if name in names:
                emit(counter(len(completed), len(names), f'{stage} · {name}', '작업'))
            result = original_step(root, name, *args, **kwargs)
            if name in names:
                completed.add(name)
                emit(counter(len(completed), len(names), f'{stage} · {name} 완료', '작업'))
            return result
        resume_steps.step = step

    original_queue = live_log.queue
    class ProgressQueue(original_queue.Queue):
        def put(self, item, *args, **kwargs):
            if isinstance(item, str):
                emit(parse(item))
            return super().put(item, *args, **kwargs)
    live_log.queue = types.SimpleNamespace(Queue=ProgressQueue, Empty=original_queue.Empty)
    original_execute = live_log.execute
    def execute(cmd, log, source, env):
        emit({'label': f'{log.name} · 작업 준비 중', 'done': None, 'total': None})
        return original_execute(cmd, log, source, env)
    live_log.execute = execute
    original_render = live_log.Display.render
    def render(self, lines):
        # Native aggregate panels have verified completed subtask counts.
        for line in lines:
            value = parse(line)
            if value:
                emit(value)
        return original_render(self, lines)
    live_log.Display.render = render


if __name__ == '__main__':
    import runpy
    import sys
    from pathlib import Path
    target = sys.argv[1]
    sys.argv = sys.argv[1:]
    sys.path.insert(0, str(Path(target).resolve().parent))
    install()
    runpy.run_path(target, run_name='__main__')
