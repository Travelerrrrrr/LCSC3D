"""Start a private test EXE and loopback update service; no GitHub release needed."""
import argparse
import ctypes
from ctypes import wintypes
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time

from local_update_server import serve_release


ROOT = Path(__file__).resolve().parents[1]


def file_hash(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def previous_version(value):
    parts = [int(part) for part in value.split('.')]
    for index in (2, 1, 0):
        if parts[index]:
            parts[index] -= 1
            return '.'.join(map(str, parts))
    return value


def process_image(pid):
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                ctypes.POINTER(wintypes.DWORD)]
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        return None
    try:
        buffer, length = ctypes.create_unicode_buffer(32768), wintypes.DWORD(32768)
        if kernel.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(length)):
            return Path(buffer.value).resolve()
    finally:
        kernel.CloseHandle(handle)
    return None


def stop_test_process(pid, executable):
    if type(pid) is int and process_image(pid) == executable.resolve():
        subprocess.run(['taskkill', '/PID', str(pid), '/T', '/F'], stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW, timeout=15)


def stop_test_session(folder, executable, data):
    """Find only this session's copies, including a helper not yet in result.json."""
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.K32EnumProcesses.argtypes = [ctypes.POINTER(wintypes.DWORD), wintypes.DWORD,
                                      ctypes.POINTER(wintypes.DWORD)]
    processes = (wintypes.DWORD * 16384)()
    size = wintypes.DWORD()
    if not kernel.K32EnumProcesses(processes, ctypes.sizeof(processes), ctypes.byref(size)):
        return
    matches = []
    for pid in processes[:size.value // ctypes.sizeof(wintypes.DWORD)]:
        image = process_image(pid) if pid else None
        if image == executable or (image is not None and image.name == 'updater.exe'
                                   and image.parent.parent == data / 'updates' and image.is_relative_to(folder)):
            matches.append((image.name != 'updater.exe', pid, image))
    # Stop helpers before their targets so they cannot replace/restart on exit.
    for _, pid, image in sorted(matches):
        stop_test_process(pid, image)


def read_json(path):
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--exe', type=Path, default=ROOT / 'outputs/LCSC3D.exe')
    parser.add_argument('--candidate-exe', type=Path, help='Optional replacement EXE; defaults to the same build')
    parser.add_argument('--target-version', help='Version advertised by the fixture; defaults to the repository version')
    parser.add_argument('--current-version', help='Simulated comparison version; does not change the EXE version')
    parser.add_argument('--scenario', choices=('success', 'bad-checksum', 'no-update', 'check-failure'), default='success')
    parser.add_argument('--delay-ms', type=int, default=5, help='Delay per 256 KiB download chunk')
    parser.add_argument('--auto', action='store_true', help='Drive the real update buttons and verify the result')
    parser.add_argument('--startup', action='store_true', help='With --auto, verify the automatic startup check instead of clicking Check')
    args = parser.parse_args()
    if sys.platform != 'win32':
        parser.error('This test launcher requires Windows')
    if args.startup and not args.auto:
        parser.error('--startup requires --auto')
    version = args.target_version or re.search(r"VERSION = '([^']+)'", (ROOT / 'app/main.py').read_text(encoding='utf-8')).group(1)
    if not re.fullmatch(r'\d+\.\d+\.\d+', version):
        parser.error('Versions must use X.Y.Z')
    current = args.current_version or (version if args.scenario == 'no-update' else previous_version(version))
    if not all(re.fullmatch(r'\d+\.\d+\.\d+', value) for value in (version, current)):
        parser.error('Versions must use X.Y.Z')
    newer = tuple(map(int, version.split('.'))) > tuple(map(int, current.split('.')))
    if (args.scenario == 'no-update' and newer) or (args.scenario != 'no-update' and not newer):
        parser.error('Update scenarios need a newer target; no-update needs a target no newer than current')
    source = args.exe.resolve(strict=True)
    candidate_source = (args.candidate_exe or source).resolve(strict=True)
    test_root = Path(os.environ.get('LOCALAPPDATA', Path.home() / 'AppData/Local')) / 'LCSC3D/update-tests'
    test_root.mkdir(parents=True, exist_ok=True)
    folder = Path(tempfile.mkdtemp(prefix='run-', dir=test_root)).resolve()
    executable = folder / 'app/LCSC3D.exe'
    candidate = folder / 'server/LCSC3D.exe'
    executable.parent.mkdir()
    candidate.parent.mkdir()
    shutil.copy2(source, executable)
    shutil.copy2(candidate_source, candidate)
    profile = folder / 'profile'
    data = profile / 'LCSC3D'
    data.mkdir(parents=True)
    settings = data / 'LCSC3D-settings.json'
    settings.write_text(json.dumps({'store_proxy': 'direct', 'update_proxy': 'system', 'log_level': 'DEBUG',
                                    'destination': str(data / 'downloads'), 'step': True, 'obj': True}), encoding='utf-8')
    expected_settings = json.loads(settings.read_text(encoding='utf-8'))
    original_hash = file_hash(executable)
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith(('PYTHON', 'QT_', 'QML_', 'QSG_', '_PYI_'))}
    environment['LOCALAPPDATA'] = str(profile)
    environment['PYINSTALLER_RESET_ENVIRONMENT'] = '1'
    windows = Path(os.environ['SystemRoot'])
    environment['PATH'] = os.pathsep.join((str(windows / 'System32'), str(windows)))
    report = {'scenario': args.scenario, 'actual_source': str(source), 'test_executable': str(executable),
              'simulated_current_version': current, 'target_version': version,
              'same_build_candidate': original_hash == file_hash(candidate), 'startup_check': args.startup, 'success': False}
    result, result_path, process = None, None, None
    started = time.monotonic()
    print('TEST_DIRECTORY', folder, flush=True)
    try:
        with serve_release(candidate, version, scenario=args.scenario, delay_ms=args.delay_ms) as server:
            command = [str(executable), '--local-update-source', server.origin,
                       '--local-update-current-version', current]
            if args.auto:
                command += ['--self-test-local-update', str(folder)]
            if args.startup:
                command += ['--self-test-startup-update']
            print('LOCAL_SOURCE', server.origin, flush=True)
            print(f'版本比较模拟为 {current} → {version}，EXE 内的实际版本不修改。', flush=True)
            print('启动后发现新版会自动提醒，也可点击“检查更新”→“下载并重启”。手动测试完成后按 Ctrl+C 结束。', flush=True)
            process = subprocess.Popen(command, cwd=executable.parent, env=environment,
                                       creationflags=subprocess.CREATE_NO_WINDOW)
            deadline = time.monotonic() + 160
            notified = False
            while True:
                for path in (data / 'updates').glob('.LCSC3D-update-*/result.json'):
                    value = read_json(path)
                    if value:
                        result, result_path = value, path
                ui = read_json(folder / 'ui-verification.json')
                report['requests'] = list(server.requests)
                if result is not None and not notified:
                    print('UPDATE_RESULT', json.dumps(result, ensure_ascii=False), flush=True)
                    notified = True
                if args.auto:
                    if args.scenario == 'success' and result is not None:
                        assert result['status'] == 'success', result
                        assert ui and ui['outcome'] == 'prepared' and ui['sha256_verified'], ui
                        assert (ui['check_clicked'] or ui['startup_check'] and ui['notification_shown']) and ui['download_clicked'] and ui['local_source_visible']
                        assert process.wait(timeout=15) == 0
                        assert file_hash(executable) == file_hash(candidate)
                        assert read_json(result_path.parent / 'ack.json')
                        saved = read_json(settings)
                        assert all(saved.get(key) == value for key, value in expected_settings.items())
                        assert {path.name for path in executable.parent.iterdir()} == {'LCSC3D.exe'}
                        report.update(success=True, ui=ui, update=result, settings_preserved=True,
                                      exe_directory_clean=True, downloaded_from_local_http=True)
                        break
                    if args.scenario != 'success' and ui and process.poll() is not None:
                        assert process.returncode == 0
                        expected = {'no-update': 'no_update', 'bad-checksum': 'download_failed', 'check-failure': 'check_failed'}[args.scenario]
                        if args.startup and args.scenario in ('no-update', 'check-failure'):
                            expected += '_silent'
                        assert ui['outcome'] == expected, ui
                        assert file_hash(executable) == original_hash and result is None
                        if args.scenario == 'bad-checksum':
                            assert 'SHA-256' in ui['error'], ui
                            assert not list((data / 'updates').glob('.LCSC3D-update-*'))
                        report.update(success=True, ui=ui, original_preserved=True)
                        break
                    expected_outcome = {'success': 'prepared', 'bad-checksum': 'download_failed', 'no-update': 'no_update', 'check-failure': 'check_failed'}[args.scenario]
                    if args.startup and args.scenario in ('no-update', 'check-failure'):
                        expected_outcome += '_silent'
                    if ui and ui['outcome'] != expected_outcome:
                        raise AssertionError(ui)
                    if time.monotonic() > deadline:
                        raise TimeoutError('Local update verification timed out')
                time.sleep(.1)
            report['requests'] = server.requests
    except KeyboardInterrupt:
        report.update(success=bool(result and result.get('status') == 'success'), update=result)
    except Exception as exc:
        report['error'] = type(exc).__name__ + ': ' + str(exc)
        raise
    finally:
        report['seconds'] = round(time.monotonic() - started, 2)
        (folder / 'verification.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        stop_test_session(folder, executable, data)
        if result:
            stop_test_process(result.get('new_pid'), executable)
            if result_path:
                stop_test_process(result.get('helper_pid'), result_path.parent / 'updater.exe')
        if process is not None:
            stop_test_process(process.pid, executable)
            process.wait(timeout=15)
        print('REPORT', folder / 'verification.json', flush=True)
    return 0 if report['success'] or not args.auto else 1


if __name__ == '__main__':
    raise SystemExit(main())
