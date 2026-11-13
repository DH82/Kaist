#!/usr/bin/env python3
"""Validate captured source contents without importing GPU packages."""
import hashlib
import json
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]

def verify(root=ROOT):
    count=0
    for manifest,folder in [('pipeline/current/workspace.manifest.json','pipeline/current/workspace'),
                            ('pipeline/dependency-source.manifest.json','pipeline/dependency-source'),
                            ('pipeline/current/project/00_scripts.manifest.json','pipeline/current/project/00_scripts')]:
        records=json.loads((root/manifest).read_text())['files']
        for name,expected in records.items():
            path=root/folder/name
            if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest()!=expected:
                raise ValueError(f'Source mismatch: {path}')
            count+=1
    print(f'{count} source files verified')

if __name__=='__main__':verify()
