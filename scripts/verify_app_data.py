"""Run and close the real EXE, checking migration and a clean EXE directory."""
import argparse
import ctypes
from ctypes import wintypes
import json
import os
import re
from pathlib import Path
import shutil
import subprocess
import tempfile
import time


root = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--output-dir', type=Path, default=root / 'outputs')
parser.add_argument('--verify-dir', type=Path, required=True)
args = parser.parse_args()
directory = args.verify_dir.resolve()
directory.mkdir(parents=True, exist_ok=True)
executable = directory / '软件目录' / 'LCSC3D.exe'
executable.parent.mkdir(exist_ok=True)
shutil.copy2(args.output_dir.resolve() / 'LCSC3D.exe', executable)
marker = executable.parent / '原有文件.txt'
marker.write_text('Preserve existing user files.', encoding='utf-8')

user = ctypes.WinDLL('user32', use_last_error=True)
kernel = ctypes.WinDLL('kernel32', use_last_error=True)
callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
user.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
user.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel.OpenProcess.restype = wintypes.HANDLE
kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                            ctypes.POINTER(wintypes.DWORD)]
kernel.CloseHandle.argtypes = [wintypes.HANDLE]


def own_window():
    found = []

    @callback_type
    def inspect(hwnd, unused):
        title = ctypes.create_unicode_buffer(128)
        user.GetWindowTextW(hwnd, title, len(title))
        if title.value != 'LCSC3D':
            return True
        pid = wintypes.DWORD()
        user.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        handle = kernel.OpenProcess(0x1000, False, pid.value)
        if handle:
            try:
                path = ctypes.create_unicode_buffer(32768)
                size = wintypes.DWORD(len(path))
                if kernel.QueryFullProcessImageNameW(handle, 0, path, ctypes.byref(size)):
                    if Path(path.value).resolve() == executable:
                        found.append(hwnd)
            finally:
                kernel.CloseHandle(handle)
        return True

    user.EnumWindows(inspect, 0)
    return found[0] if found else None


version = re.search(r"VERSION = '([^']+)'", (root / 'app/main.py').read_text(encoding='utf-8')).group(1)
report = {'version': version, 'cases': {}}
for case in ('legacy_migration', 'new_profile'):
    profile = Path(tempfile.mkdtemp(prefix='LCSC3D 数据验证 ')).resolve()
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith(('PYTHON', 'QT_', 'QML_', 'QSG_', '_PYI_'))}
    windows = Path(os.environ['SystemRoot'])
    environment['PATH'] = os.pathsep.join((str(windows / 'System32'), str(windows)))
    environment['LOCALAPPDATA'] = str(profile)
    legacy = executable.parent / 'LCSC3D-settings.json'
    if case == 'legacy_migration':
        old = {'destination': str(directory / '主动选择的导出目录'), 'step': True, 'obj': True,
               'store_proxy': 'direct', 'update_proxy': 'system', 'log_level': 'WARNING'}
        legacy.write_text(json.dumps(old, ensure_ascii=False), encoding='utf-8')
    process = subprocess.Popen([str(executable)], cwd=executable.parent, env=environment,
                               creationflags=subprocess.CREATE_NO_WINDOW)
    try:
        deadline = time.monotonic() + 30
        hwnd = None
        while time.monotonic() < deadline and process.poll() is None:
            hwnd = own_window()
            if hwnd is not None:
                break
            time.sleep(0.1)
        assert hwnd is not None, 'Test application did not create its window'
        runtime = profile / 'LCSC3D' / 'runtime'
        assert any(runtime.glob('_MEI*')), 'Runtime was extracted outside app data'
        # Close only the window whose process image matches this copied EXE.
        assert user.PostMessageW(hwnd, 0x10, 0, 0)
        assert process.wait(timeout=30) == 0
        settings_path = profile / 'LCSC3D' / 'LCSC3D-settings.json'
        settings = json.loads(settings_path.read_text(encoding='utf-8'))
        if case == 'legacy_migration':
            assert not legacy.exists()
            assert all(settings[key] == value for key, value in old.items())
        else:
            assert settings['destination'] == str(profile / 'LCSC3D' / 'downloads')
        assert {path.name for path in executable.parent.iterdir()} == {executable.name, marker.name}
        assert marker.read_text(encoding='utf-8') == 'Preserve existing user files.'
        assert (profile / 'LCSC3D' / 'logs' / 'LCSC3D.log').is_file()
        report['cases'][case] = {'exit_code': 0, 'exe_directory_clean': True,
                                 'app_data_settings': True, 'app_data_logs': True,
                                 'app_data_runtime': True}
    finally:
        if process.poll() is None:
            subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           creationflags=subprocess.CREATE_NO_WINDOW, timeout=15)
            process.wait(timeout=15)
report['success'] = True
(directory / 'app-data-verification.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
print(json.dumps(report, ensure_ascii=False))
