"""Read only STEP bounds in a cancellable, short-lived CAD process.

Export keeps the original resource bytes and never creates a mesh or scaled
copy. Cache only six numbers per content hash, not model payloads or shapes.
"""
from __future__ import annotations

from collections import OrderedDict
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time

from app_paths import data_directory, temporary_directory
from errors import DownloadError

# One kernel at a time bounds peak memory. The process exits after each cache
# miss, returning all native allocations to the OS; cache hits launch nothing.
_WORKERS = threading.Semaphore(1)
_CACHE = OrderedDict()
_CACHE_LIMIT = 128
_DISK_CACHE_LIMIT = 256
_CACHE_VERSION = 'step-bounds-v1'  # Bump for changes to the kernel or bounds algorithm.


def rotation_zxy(angles):
    x, y, z = (math.radians(float(a)) for a in angles)
    cx, cy, cz, sx, sy, sz = math.cos(x), math.cos(y), math.cos(z), math.sin(x), math.sin(y), math.sin(z)
    return ((cy*cz + sy*sx*sz, -cy*sz + sy*sx*cz, sy*cx),
            (cx*sz, cx*cz, -sx),
            (-sy*cz + cy*sx*sz, sy*sz + cy*sx*cz, cy*cx))


def euler_xyz(matrix):
    """Decompose Rz(z) Ry(y) Rx(x); return right-handed X/Y/Z degrees."""
    r = matrix
    y = math.asin(max(-1., min(1., -float(r[2][0]))))
    if abs(math.cos(y)) > 1e-9:
        x, z = math.atan2(float(r[2][1]), float(r[2][2])), math.atan2(float(r[1][0]), float(r[0][0]))
    else:
        x, z = math.atan2(-float(r[1][2]), float(r[1][1])), 0.
    return tuple(math.degrees(v) for v in (x, y, z))


def transform_bounds(bounds, matrix):
    points = [tuple(sum(float(matrix[row*4+j])*p[j] for j in range(3)) + float(matrix[row*4+3])
                    for row in range(3))
              for p in ((x,y,z) for x in (bounds[0], bounds[3]) for y in (bounds[1], bounds[4]) for z in (bounds[2], bounds[5]))]
    return tuple(min(p[i] for p in points) for i in range(3)) + tuple(max(p[i] for p in points) for i in range(3))


def _valid_bounds(value):
    return (isinstance(value, (list, tuple)) and len(value) == 6
            and all(type(v) in (int, float) and math.isfinite(v) and abs(v) < 1e7 for v in value)
            and all(value[i+3] > value[i] for i in range(3)))


def _read_cache(path, digest):
    try:
        if path.stat().st_size > 1024:
            return None
        value = json.loads(path.read_text(encoding='utf-8'))
        if (isinstance(value, dict) and value.get('sha256') == digest
                and _valid_bounds(value.get('bounds'))):
            return tuple(value['bounds'])
    except (OSError, ValueError):
        pass
    return None


def _write_cache(path, digest, bounds):
    staged = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                prefix='bounds-', suffix='.tmp', delete=False) as stream:
            staged = Path(stream.name)
            json.dump({'sha256': digest, 'bounds': bounds}, stream, allow_nan=False)
        os.replace(staged, path)
        entries = [p for p in path.parent.glob('*.json') if len(p.stem) == 64
                   and all(c in '0123456789abcdef' for c in p.stem)]
        for old in sorted(entries, key=lambda p: p.stat().st_mtime, reverse=True)[_DISK_CACHE_LIMIT:]:
            old.unlink(missing_ok=True)
    except OSError:
        pass  # An unavailable optional cache must not prevent export.
    finally:
        if staged is not None:
            try:
                staged.unlink(missing_ok=True)
            except OSError:
                pass


def step_bounds(step, check=lambda: None):
    check()
    if not step or len(step) > 64*1024*1024 or b'ISO-10303-21' not in step[:2048]:
        raise DownloadError('STEP 文件无效或超出模型处理范围')
    digest = hashlib.sha256(step).hexdigest()
    path = data_directory()/'cache'/_CACHE_VERSION/(digest+'.json')
    key = (str(path), digest)
    while not _WORKERS.acquire(timeout=.1):
        check()
    try:
        check()
        result = _CACHE.get(key)
        if result is None:
            result = _read_cache(path, digest)
        if result is None:
            result = _run(step, check)
            check()
            _write_cache(path, digest, result)
        _CACHE[key] = result
        _CACHE.move_to_end(key)
        while len(_CACHE) > _CACHE_LIMIT:
            _CACHE.popitem(last=False)
        check()
        return result
    finally:
        _WORKERS.release()


def _run(step, check):
    with tempfile.TemporaryDirectory(prefix='model-geometry-', dir=temporary_directory()) as directory:
        root = Path(directory)
        (root/'input.step').write_bytes(step)
        request = root/'request.json'
        request.write_text('{}', encoding='utf-8')
        args = ([sys.executable, '--model-geometry', str(request)] if getattr(sys, 'frozen', False)
                else [sys.executable, str(Path(__file__).resolve()), '--model-geometry', str(request)])
        environment = dict(os.environ, PYTHONDONTWRITEBYTECODE='1')
        flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
        check()
        process = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, env=environment, creationflags=flags)
        deadline = time.monotonic() + 120
        try:
            while process.poll() is None:
                check()
                if time.monotonic() > deadline:
                    raise DownloadError('STEP 边界读取超时，已跳过此模型')
                try:
                    process.wait(timeout=.1)
                except subprocess.TimeoutExpired:
                    pass
            check()
            report = root/'result.json'
            if not report.is_file() or report.stat().st_size > 4096:
                raise DownloadError('STEP 边界读取失败，已跳过此模型')
            value = json.loads(report.read_text(encoding='utf-8'))
            if not value.get('success'):
                raise DownloadError(value.get('error', 'STEP 边界读取失败'))
            if not _valid_bounds(value.get('bounds')):
                raise DownloadError('STEP 实体尺寸无效')
            return tuple(value['bounds'])
        finally:
            if process.poll() is None:
                process.kill()
            process.wait()


def _bounds(shape):
    from OCP.Bnd import Bnd_Box
    from OCP.BRepBndLib import BRepBndLib
    box = Bnd_Box()
    BRepBndLib.AddOptimal_s(shape, box, False, False)
    if box.IsVoid():
        raise DownloadError('STEP 没有可用的实体范围')
    low, high = box.CornerMin(), box.CornerMax()
    values = (low.X(),low.Y(),low.Z(),high.X(),high.Y(),high.Z())
    if not all(math.isfinite(v) and abs(v) < 1e7 for v in values) or any(values[i+3] <= values[i] for i in range(3)):
        raise DownloadError('STEP 实体尺寸无效')
    return values


def worker(request):
    root = Path(request).resolve().parent
    base = Path(temporary_directory()).resolve()
    if not root.is_relative_to(base) or root == base:
        return 2
    result = {'success': False}
    try:
        from OCP.IFSelect import IFSelect_RetDone
        from OCP.STEPControl import STEPControl_Reader
        reader = STEPControl_Reader()
        if reader.ReadFile(str(root/'input.step')) != IFSelect_RetDone:
            raise DownloadError('STEP 无法读取，已跳过此模型')
        reader.SetSystemLengthUnit(1.)
        if not reader.TransferRoots():
            raise DownloadError('STEP 没有可用实体')
        result = {'success': True, 'bounds': _bounds(reader.OneShape())}
    except Exception as exc:
        result['error'] = str(exc) if isinstance(exc, DownloadError) else 'STEP 边界读取失败，已跳过此模型'
    (root/'result.json').write_text(json.dumps(result, allow_nan=False), encoding='utf-8')
    return 0 if result['success'] else 1


if __name__ == '__main__':
    sys.exit(worker(sys.argv[2]) if len(sys.argv) == 3 and sys.argv[1] == '--model-geometry' else 2)
