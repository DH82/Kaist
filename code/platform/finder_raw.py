"""Inventory a manually copied four-camera RAW without modifying the share."""
import ast
import csv
import hashlib
import json
import os
import re
import stat
import struct
from math import prod
from pathlib import Path


def snapshot(folder):
    if folder.is_symlink() or not folder.is_dir():
        raise ValueError('RAW 폴더를 확인하세요.')
    entries = {}
    for base, dirs, names in os.walk(folder, onerror=raise_walk_error):
        # DSM filesystem metadata is exposed over NFS but hidden from SMB clients.
        dirs[:] = [name for name in dirs if name != "@eaDir"]
        for name in dirs + names:
            path = Path(base) / name
            if path.is_symlink():
                raise ValueError(f'RAW에 심볼릭 링크가 있습니다: {name}')
        for name in names:
            if name == '.DS_Store' or name.startswith('._'):
                continue
            path = Path(base) / name
            info = path.stat()
            if not stat.S_ISREG(info.st_mode) or info.st_size == 0:
                raise ValueError(f'파일이 비었거나 복사 중입니다: {name}')
            entries[path.relative_to(folder).as_posix()] = (info.st_size, info.st_mtime_ns, info.st_ino)
    return entries


def raise_walk_error(error):
    raise error


def check_video(path):
    size = path.stat().st_size
    boxes = set()
    with path.open('rb') as stream:
        while stream.tell() < size:
            start = stream.tell()
            header = stream.read(8)
            if len(header) != 8:
                raise ValueError(f'MP4 복사 미완료: {path.name}')
            length, kind = struct.unpack('>I4s', header)
            if length == 1:
                extended = stream.read(8)
                length = int.from_bytes(extended, 'big') if len(extended) == 8 else -1
            elif length == 0:
                length = size - start
            if length < stream.tell() - start or start + length > size:
                raise ValueError(f'MP4 복사 미완료: {path.name}')
            boxes.add(kind)
            stream.seek(start + length)
    if not {b'moov', b'mdat'} <= boxes:
        raise ValueError(f'MP4 헤더 또는 영상 데이터 누락: {path.name}')


def check_depth(stream, size, name):
    if stream.read(6) != b'\x93NUMPY':
        raise ValueError(f'깊이 파일 형식 오류: {name}')
    version = stream.read(2)
    width = {b'\x01\x00': 2, b'\x02\x00': 4, b'\x03\x00': 4}.get(version)
    if not width:
        raise ValueError(f'깊이 파일 버전 오류: {name}')
    n = int.from_bytes(stream.read(width), 'little')
    if not 0 < n <= 65536:
        raise ValueError(f'깊이 파일 헤더 오류: {name}')
    try:
        header = ast.literal_eval(stream.read(n).decode('ascii').strip())
        shape = header['shape']
        dtype = re.fullmatch(r'[<>=|]?[uif](\d+)', header['descr'])
        valid = len(shape) == 2 and all(type(v) is int and v > 0 for v in shape) and dtype
        if not valid or stream.tell() + prod(shape) * int(dtype[1]) != size:
            raise ValueError
    except (ValueError, UnicodeError, SyntaxError, TypeError, KeyError):
        raise ValueError(f'깊이 프레임 크기 오류: {name}') from None
    stream.seek(0)


def manifest(folder):
    before = snapshot(folder)
    if any(k in before for k in ('upload_complete.json', 'upload_manifest.json', 'final_data.pkl', 'metadata.json')):
        raise ValueError('촬영 RAW 폴더와 업로드 상태를 확인하세요.')
    for prefix in ('extrinsics', *(f'cam_{i}_intrinsics' for i in range(1,5))):
        data = json.loads((folder / (prefix + '.json')).read_bytes())
        if not isinstance(data, dict) or not data:
            raise ValueError(f'캘리브레이션 파일 오류: {prefix}')
    for i in range(1, 5):
        cam = f'cam_{i}'
        check_video(folder / (cam + '.mp4'))
        with (folder / (cam + '_timestamps.csv')).open(encoding='utf-8-sig') as stream:
            indices = [int(row['frame_idx']) for row in csv.DictReader(stream)]
        if not indices or min(indices) < 0 or len(indices) != len(set(indices)):
            raise ValueError(f'타임스탬프 프레임 번호 오류: {cam}')
        expected = {f'{cam}_depth/frame_{idx:06}.npy' for idx in indices}
        actual = {k for k in before if k.startswith(cam+'_depth/') and k.endswith('.npy')}
        if expected != actual:
            raise ValueError(f'깊이 프레임과 타임스탬프 불일치: {cam}')
    files = {}
    for name,(size,_,_) in before.items():
        h = hashlib.sha256()
        with (folder / name).open('rb') as stream:
            if re.fullmatch(r'cam_[1-4]_depth/frame_\d+\.npy', name):
                check_depth(stream, size, name)
            for block in iter(lambda: stream.read(1024*1024), b''):
                h.update(block)
        files[name] = {'size': size, 'sha256': h.hexdigest()}
    if before != snapshot(folder):
        raise ValueError('검사 중 RAW가 변경됐습니다. 복사가 끝난 뒤 다시 검사하세요.')
    date = re.match(r'ep_(\d{4})(\d{2})(\d{2})_', folder.name)
    return {'source': 'manual_nas_copy', 'episode': folder.name,
            'capture_date': '-'.join(date.groups()) if date else '',
            'calibration_file': 'extrinsics.json',
            'calibration_id': 'sha256-'+files['extrinsics.json']['sha256'], 'files': files}
