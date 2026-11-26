"""Package source and user-facing documentation, excluding scratch/cache files."""
import argparse
import hashlib
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path, PurePosixPath

workspace = Path(__file__).resolve().parent.parent
app = workspace / 'app'
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--output-dir', type=Path, default=workspace / 'outputs',
                    help='Delivery folder; use a separate folder for a development demo')
outputs = parser.parse_args().output_dir.resolve()
try:
    tracked = subprocess.check_output(
        ['git', '-C', str(workspace), 'ls-files', '-z'], stderr=subprocess.PIPE,
        env=dict(os.environ, GIT_CEILING_DIRECTORIES=str(workspace.parent)),
    )
except (OSError, subprocess.CalledProcessError) as error:
    reason = str(error)
    if isinstance(error, subprocess.CalledProcessError) and error.stderr:
        reason = error.stderr.decode('utf-8', errors='replace').strip() or reason
    parser.exit(1, f'源码 ZIP 需要在 Git 检出中生成（git ls-files 失败）：{reason}\n')

missing = []
files = []
excluded_parts = {'.git', '__pycache__', '.venv', 'build', 'dist', 'runtime', 'work', 'outputs'}
for path in sorted(set(tracked.decode('utf-8').split('\0')) - {''}):
    relative = PurePosixPath(path)
    source = workspace / relative
    if not source.is_file():
        missing.append(path)
    if (relative.suffix.lower() in {'.csv', '.log'} or '.log.' in relative.name
            or relative.name in {'LCSC3D-settings.json', 'store-session.bin'}
            or relative.name.startswith(('.store-session-', '.LCSC3D-settings-'))
            or excluded_parts.intersection(relative.parts)):
        print(f'警告：源码 ZIP 排除已跟踪文件 {path}', file=sys.stderr)
        continue
    files.append((source, 'LCSC3D/' + path))
if missing:
    parser.exit(1, '工作区缺失已跟踪文件：\n' + '\n'.join(missing) + '\n')

outputs.mkdir(parents=True, exist_ok=True)
target = outputs / 'LCSC3D.zip'
with zipfile.ZipFile(target, 'w') as archive:
    for source, name in files:
        # Keep ZIP metadata independent of checkout timestamps and permissions.
        entry = zipfile.ZipInfo(name)
        entry.create_system = 3
        entry.external_attr = 0o100644 << 16
        archive.writestr(entry, source.read_bytes(), compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
shutil.copyfile(app / 'README.md', outputs / '使用说明.md')
for name in ['LCSC3D.exe', target.name]:
    path = outputs / name
    if path.exists():
        with path.open('rb') as stream:
            digest = hashlib.file_digest(stream, 'sha256').hexdigest()
        print(name, path.stat().st_size, digest)
