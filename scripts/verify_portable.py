"""Run LCSC3D from an isolated Chinese directory with only the system PATH."""
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

root = Path(__file__).resolve().parent.parent
source = root / 'outputs' / 'LCSC3D.exe'
sandbox = root / 'work' / '便携验证'
sandbox.mkdir(exist_ok=True)
executable = sandbox / source.name
shutil.copy2(source, executable)
environment = os.environ.copy()
for key in list(environment):
    if key.startswith(('PYTHON', 'QT_', 'QML_', 'QSG_', 'LCSC3D_')):
        environment.pop(key)
windows = Path(os.environ['SystemRoot'])
environment['PATH'] = os.pathsep.join(str(path) for path in (windows / 'System32', windows))
destination = sandbox / '验证结果'
if destination.exists():
    if destination.is_symlink() or destination.resolve().parent != sandbox.resolve():
        raise ValueError('Verification output must stay inside the sandbox')
    shutil.rmtree(destination)
started = time.monotonic()
try:
    with (sandbox / 'stdout.log').open('wb') as stdout, (sandbox / 'stderr.log').open('wb') as stderr:
        process = subprocess.run([str(executable), '--self-test', str(destination)],
                                 cwd=sandbox, env=environment, stdout=stdout, stderr=stderr,
                                 creationflags=subprocess.CREATE_NO_WINDOW, timeout=150)
    print('EXIT', process.returncode, 'SECONDS', round(time.monotonic() - started, 1), flush=True)
    assert process.returncode == 0, (sandbox / 'stderr.log').read_text(encoding='utf-8', errors='replace')
    report = json.loads((destination / 'verification.json').read_text(encoding='utf-8'))
    assert report['application_name'] == report['window_title'] == 'LCSC3D'
    assert report['automatic_preview']
    assert report['viewer_page_loads'] == 1 and report['second_3d_ready']
    assert report['frozen'] and report['preview'] == 'ready'
    titles = report['titles_before_download']
    assert titles['C2040'] == 'RP2040' and titles['C20197'] == '4D03WGJ0102T5E'
    assert titles['C163691'] not in ('', '—', '查询中…', '查询失败', '未提供型号')
    assert report['preview_window'] == {'hwnd_preserved': True, 'events': []}
    assert [result['status'] for result in report['results']] == ['成功', '成功', '失败']
    assert report['download_selection'] == {
        'checked_ids': ['C2040', 'C20197', 'C999999999999'],
        'unchecked_ids': ['C163691'], 'selected_only': True}
    assert not any('C163691' in path.name for path in (destination / '批量下载测试').iterdir())
    assert source.read_bytes() == executable.read_bytes()
    print('NAME_VERIFIED LCSC3D', flush=True)
finally:
    executable.unlink(missing_ok=True)
