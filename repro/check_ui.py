#!/usr/bin/env python3
"""Verify authenticated UI -> Airflow/Worker connections without launching jobs."""
import argparse
import base64
import http.cookiejar
import json
from pathlib import Path
import re
import urllib.request

from start_stack import read_env, ROOT


def verify_ui(env_file):
    config = read_env(env_file)
    state = Path(config.get('STACK_STATE_DIR') or ROOT)
    base = f'http://127.0.0.1:{config.get("UI_PORT") or "18083"}'
    password = (state / 'secrets/ui_password').read_text().strip()
    basic = base64.b64encode(f'operator:{password}'.encode()).decode()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    csrf = ''

    def request(path, data=None):
        headers = {'Authorization': f'Basic {basic}'}
        if data is not None:
            headers.update({'Content-Type': 'application/json', 'Origin': base, 'X-Platform-CSRF': csrf})
        req = urllib.request.Request(base + path, data=None if data is None else json.dumps(data).encode(), headers=headers)
        with opener.open(req, timeout=60) as response:
            return response.read()

    html = request('/').decode()
    match = re.search(r"const csrf='([^']+)'", html)
    if not match:
        raise RuntimeError('UI CSRF token not found')
    csrf = match[1]
    users = json.loads((state / 'state/simple_auth_manager_passwords.json.generated').read_text())
    request('/api/login', dict(name='deployment-check', role='admin', password=users['admin']))
    try:
        info = json.loads(request('/api/info'))
        if not info.get('central_ready') or not info.get('server_ready') or not info.get('nas_ready'):
            raise RuntimeError('UI reports Airflow/Worker/NAS unavailable')
        print('PASS UI -> Airflow -> GPU Worker / NAS')
        for route in ('/api/workers', '/api/jobs', '/api/intake', '/api/operators', '/api/pc1/mano'):
            json.loads(request(route))
            print(f'PASS {route}')
        for route in ('/assets/outputs.js', '/assets/reviews.js', '/assets/mask-editor.js', '/assets/intake.js', '/assets/session.js'):
            if not request(route):
                raise RuntimeError(f'Empty asset: {route}')
        print('PASS UI review / result / intake assets')
    finally:
        request('/api/logout', {})


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--env-file', type=Path, default=ROOT / '.env')
    verify_ui(parser.parse_args().env_file.resolve())
