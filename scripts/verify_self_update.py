"""Exercise the real frozen replacement helper and startup handshake in isolation.

The candidate is a byte-identical copy of the tested release. Version discovery
and corrupted downloads are covered by offline tests; this check covers the
actual Windows executable locks, process exit, helper and restarted Qt window.
"""
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

source = root / 'outputs' / 'LCSC3D.exe'
version = re.search(r"VERSION = '([^']+)'", (root / 'app/main.py').read_text(encoding='utf-8')).group(1)
directory = Path(tempfile.mkdtemp(prefix='自更新 验证 ', dir=root / 'work')).resolve()
target = directory / 'LCSC3D.exe'
shutil.copy2(source, target)
settings = directory / 'LCSC3D-settings.json'
settings.write_text(json.dumps({'destination': str(directory / '原有资源'), 'step': True,
                                'obj': True, 'symbol': True, 'footprint': True}), encoding='utf-8')
original_settings = settings.read_bytes()
stage = Path(tempfile.mkdtemp(prefix=STAGE_PREFIX, dir=directory))
shutil.copy2(source, stage / 'new.exe')
helper = stage / 'updater.exe'
shutil.copy2(source, helper)
environment = {key: value for key, value in os.environ.items()
               if not key.startswith(('PYTHON', 'QT_', 'QML_', '_PYI_'))}
windows = Path(os.environ['SystemRoot'])
environment['PATH'] = os.pathsep.join(str(path) for path in (windows / 'System32', windows))
environment['PYINSTALLER_RESET_ENVIRONMENT'] = '1'
started = time.monotonic()
old = subprocess.Popen([str(target), '--self-test', str(directory / '原程序验证')], cwd=directory,
                       env=environment, creationflags=subprocess.CREATE_NO_WINDOW)
manifest = stage / 'plan.json'
nonce = secrets.token_hex(24)
digest = file_hash(target)
manifest.write_text(json.dumps({'target': str(target), 'sha256': digest, 'original_sha256': digest,
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
    report = {'version': version, 'frozen_helper': True, 'original_exited': True,
              'replacement_verified': True, 'startup_acknowledged': True, 'settings_preserved': True,
              'candidate': 'byte-identical copy of the tested release',
              'seconds': round(time.monotonic()-started, 2)}
    (root / 'work' / 'self-update-verification.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
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
