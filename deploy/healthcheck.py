"""Read-only readiness probes for Compose (no credentials in process arguments)."""
import base64
import json
import os
from pathlib import Path
import sys
import urllib.request


def check(mode):
    if mode == 'airflow':
        port = os.environ.get('AIRFLOW_PORT', '18082')
        with urllib.request.urlopen(f'http://127.0.0.1:{port}/api/v2/monitor/health', timeout=5) as r:
            data = json.load(r)
        for name in ('metadatabase', 'scheduler'):
            if data.get(name, {}).get('status') != 'healthy':
                raise RuntimeError(f'{name} is not healthy')
        users = Path('/opt/airflow/simple_auth_manager_passwords.json.generated')
        if not users.is_file() or not json.loads(users.read_text()):
            raise RuntimeError('Airflow users are not initialized')
    elif mode == 'ui':
        port = os.environ.get('UI_PORT', '18083')
        password = Path(os.environ['KAIST_UI_PASSWORD_FILE']).read_text().strip()
        encoded = base64.b64encode(f'operator:{password}'.encode()).decode()
        request = urllib.request.Request(f'http://127.0.0.1:{port}/',
                                         headers={'Authorization': f'Basic {encoded}'})
        with urllib.request.urlopen(request, timeout=5) as response:
            if response.status != 200:
                raise RuntimeError('UI unavailable')
    else:
        raise ValueError(mode)


if __name__ == '__main__':
    check(sys.argv[1])
