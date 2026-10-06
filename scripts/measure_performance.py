"""Measure the delivered EXE and real model downloads on the current Windows host."""
import argparse
import ctypes
from ctypes import wintypes
import json
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def startup(executable, runs):
    import psutil  # Measurement only; not an application dependency.
    user32 = ctypes.WinDLL('user32', use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.GetWindowTextW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
    user32.GetWindowTextLengthW.argtypes = (wintypes.HWND,)
    user32.GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
    user32.IsWindowVisible.argtypes = (wintypes.HWND,)
    user32.PostMessageW.argtypes = (wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)

    def windows():
        found = {}

        @callback_type
        def visit(hwnd, unused):
            length = user32.GetWindowTextLengthW(hwnd)
            if length and user32.IsWindowVisible(hwnd):
                title = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, title, length + 1)
                if title.value == 'LCSC3D':
                    pid = wintypes.DWORD()
                    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                    found[int(hwnd)] = pid.value
            return True

        user32.EnumWindows(visit, 0)
        return found

    samples = []
    for _ in range(runs):
        existing = windows()
        started = time.perf_counter()
        process = subprocess.Popen([str(executable)], cwd=executable.parent,
                                   creationflags=subprocess.CREATE_NO_WINDOW)
        hwnd = None
        try:
            while time.perf_counter() - started < 45:
                fresh = {key: pid for key, pid in windows().items() if key not in existing}
                if fresh:
                    hwnd, pid = next(iter(fresh.items()))
                    elapsed = time.perf_counter() - started
                    break
                time.sleep(0.025)
            if hwnd is None:
                raise RuntimeError('No LCSC3D window appeared within 45 seconds')
            time.sleep(1)
            app_process = psutil.Process(pid)
            processes = [app_process, *app_process.children(recursive=True)]
            rss = sum(item.memory_info().rss for item in processes if item.is_running())
            samples.append({'window_seconds': round(elapsed, 3), 'rss_bytes': rss,
                            'process_count': len(processes)})
        finally:
            if hwnd is not None:
                user32.PostMessageW(hwnd, 0x0010, 0, 0)
            try:
                process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                for child in psutil.Process(process.pid).children(recursive=True):
                    child.terminate()
                process.terminate()
                process.wait(timeout=10)
    return {'samples': samples, 'median_window_seconds': statistics.median(x['window_seconds'] for x in samples),
            'median_rss_bytes': statistics.median(x['rss_bytes'] for x in samples)}


def downloads(runs):
    sys.path.insert(0, str(ROOT / 'app'))
    import backend
    samples = []
    for index in range(runs):
        with tempfile.TemporaryDirectory(prefix='LCSC3D-benchmark-') as folder:
            started = time.perf_counter()
            options = backend.Options(Path(folder), ('STEP', 'WRL', 'OBJ'))
            if hasattr(backend, 'download_batch'):
                results = backend.download_batch(['C2040', 'C20197'], options, api=backend.NetworkApi(use_cache=False))
            else:
                api = backend.NetworkApi()
                results = [backend.download_part(part, options, api) for part in ('C2040', 'C20197')]
            elapsed = time.perf_counter() - started
            samples.append({'seconds': round(elapsed, 3), 'files': len(list(Path(folder).rglob('*.step'))) +
                            len(list(Path(folder).rglob('*.wrl'))) + len(list(Path(folder).rglob('*.obj'))),
                            'statuses': [result.status for result in results]})
            print('DOWNLOAD_SAMPLE', index, round(elapsed, 3), file=sys.stderr, flush=True)
    successful = [sample['seconds'] for sample in samples if sample['statuses'] == ['成功', '成功']]
    return {'samples': samples, 'median_seconds': statistics.median(successful) if successful else None,
            'failed_runs': len(samples) - len(successful)}


def bundle(executable):
    from PyInstaller.archive.readers import CArchiveReader
    archive = CArchiveReader(str(executable))
    return {'exe_bytes': executable.stat().st_size, 'extracted_bytes': sum(value[2] for value in archive.toc.values()),
            'archive_entries': len(archive.toc)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('startup', 'downloads', 'bundle'))
    parser.add_argument('--runs', type=int, default=3)
    parser.add_argument('--exe', type=Path, default=ROOT / 'outputs/LCSC3D.exe')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    result = {'date': time.strftime('%Y-%m-%d'), 'mode': args.mode}
    result.update(startup(args.exe, args.runs) if args.mode == 'startup' else
                  downloads(args.runs) if args.mode == 'downloads' else bundle(args.exe))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)


if __name__ == '__main__':
    main()
