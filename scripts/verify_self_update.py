"""Exercise the real frozen replacement helper and startup handshake in isolation.

The candidate is a byte-identical copy of the tested release. Version discovery
and corrupted downloads are covered by offline tests; this check covers the
actual Windows executable locks, process exit, helper and restarted Qt window.
"""
import argparse
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root / 'app'))
from updater import STAGE_PREFIX, file_hash

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--output-dir', type=Path, default=root / 'outputs')
parser.add_argument('--report', type=Path, default=root / 'work/self-update-verification.json')
args = parser.parse_args()
source = args.output_dir.resolve() / 'LCSC3D.exe'
version = re.search(r"VERSION = '([^']+)'", (root / 'app/main.py').read_text(encoding='utf-8')).group(1)
directory = Path(tempfile.mkdtemp(prefix='自更新 验证 ', dir=root / 'work')).resolve()
target = directory / 'LCSC3D.exe'
shutil.copy2(source, target)
profile = Path(tempfile.mkdtemp(prefix='LCSC3D 更新用户数据 ')).resolve()
data_directory = profile / 'LCSC3D'
data_directory.mkdir()
settings = data_directory / 'LCSC3D-settings.json'
settings.write_text(json.dumps({'destination': str(directory / '原有资源'), 'step': True,
                                'obj': True, 'store_proxy': 'direct', 'update_proxy': 'system',
                                'log_level': 'DEBUG'}), encoding='utf-8')
original_settings = settings.read_bytes()
updates = data_directory / 'updates'
updates.mkdir()
stage = Path(tempfile.mkdtemp(prefix=STAGE_PREFIX, dir=updates))
shutil.copy2(source, stage / 'new.exe')
helper = stage / 'updater.exe'
shutil.copy2(source, helper)
environment = {key: value for key, value in os.environ.items()
               if not key.startswith(('PYTHON', 'QT_', 'QML_', '_PYI_'))}
windows = Path(os.environ['SystemRoot'])
environment['PATH'] = os.pathsep.join(str(path) for path in (windows / 'System32', windows))
environment['PYINSTALLER_RESET_ENVIRONMENT'] = '1'
environment['LOCALAPPDATA'] = str(profile)
started = time.monotonic()
old = subprocess.Popen([str(target), '--self-test', str(directory / '原程序验证')], cwd=directory,
                       env=environment, creationflags=subprocess.CREATE_NO_WINDOW)
manifest = stage / 'plan.json'
nonce = secrets.token_hex(24)
diagnostic_root = secrets.token_hex(16)
digest = file_hash(target)
manifest.write_text(json.dumps({'target': str(target), 'sha256': digest, 'original_sha256': digest,
                                'target_directory': str(target.parent),
                                'diagnostics': {'root_id': diagnostic_root, 'operation_id': diagnostic_root},
                                'parent_pid': old.pid, 'nonce': nonce, 'version': version}), encoding='utf-8')
process = subprocess.Popen([str(helper), '--apply-update', str(manifest)], cwd=directory,
                           env=environment, creationflags=subprocess.CREATE_NO_WINDOW)
new_pid = None
try:
    deadline = started + 140
    while time.monotonic() < deadline and not (stage / 'result.json').exists():
        if process.poll() is not None:
            break
        time.sleep(0.25)
    result = json.loads((stage / 'result.json').read_text(encoding='utf-8'))
    new_pid = result.get('new_pid')
    assert result['status'] == 'success', result
    assert old.wait(timeout=15) == 0, 'Original EXE verification failed'
    assert process.wait(timeout=15) == 0, 'Replacement helper failed'
    ack = json.loads((stage / 'ack.json').read_text(encoding='ascii'))
    assert ack['nonce'] == nonce
    assert file_hash(target) == digest
    assert file_hash(stage / 'previous.exe') == digest
    assert settings.read_bytes() == original_settings
    assert not (directory / 'LCSC3D-settings.json').exists()
    assert not list(directory.glob(STAGE_PREFIX + '*'))
    assert target.parent != stage.parent
    helper_log = data_directory / 'logs' / 'LCSC3D-update.log'
    events = [json.loads(line) for line in helper_log.read_text(encoding='utf-8').splitlines()]
    stages = [row['stage'] for row in events if row['event'] == 'update.install_stage']
    assert stages == ['wait_for_exit', 'verify_before_replace', 'backup', 'replace', 'restart', 'confirm_startup']
    assert all(row['root_id'] == diagnostic_root for row in events if row['event'].startswith('update.install'))
    report = {'version': version, 'frozen_helper': True, 'original_exited': True,
              'replacement_verified': True, 'startup_acknowledged': True, 'settings_preserved': True,
              'app_data_settings': True, 'app_data_update_stage': True, 'exe_directory_clean': True,
              'cross_volume': target.drive.lower() != stage.drive.lower(),
              'update_log_stages': stages, 'diagnostic_correlation': True,
              'candidate': 'byte-identical copy of the tested release',
              'seconds': round(time.monotonic()-started, 2)}
    args.report.resolve().parent.mkdir(parents=True, exist_ok=True)
    args.report.resolve().write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False), flush=True)
finally:
    # Only these explicitly launched test processes can be stopped here.
    if new_pid is not None:
        subprocess.run(['taskkill', '/PID', str(new_pid), '/T', '/F'], stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW, timeout=15)
    for launched in (old, process):
        if launched.poll() is None:
            subprocess.run(['taskkill', '/PID', str(launched.pid), '/T', '/F'], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW, timeout=15)
    for launched in (old, process):
        launched.wait(timeout=15)
