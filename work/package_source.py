"""Package source and user-facing documentation, excluding scratch/cache files."""
import hashlib
import shutil
import zipfile
import re
from pathlib import Path

workspace = Path(__file__).resolve().parent.parent
app = workspace / 'work' / 'app'
upstream = workspace / 'work' / 'upstream'
outputs = workspace / 'outputs'
version = re.search(r"VERSION = '([^']+)'", (app / 'main.py').read_text(encoding='utf-8')).group(1)
target = outputs / f'LCSC3D-Source-v{version}.zip'
with zipfile.ZipFile(target, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
    for base, prefix in [(app, Path('LCSC3D-Source')), (upstream, Path('LCSC3D-Source/upstream'))]:
        for source in base.rglob('*'):
            if not source.is_file():
                continue
            if source.suffix.lower() in {'.csv', '.log'} or source.name == 'LCSC3D-settings.json':
                continue
            relative = source.relative_to(base)
            if any(part in {'.git', '__pycache__', '.venv', 'build', 'dist', 'runtime'} for part in relative.parts):
                continue
            archive.write(source, str(prefix / relative))
shutil.copyfile(app / 'README.md', outputs / f'使用说明-v{version}.md')
for name in [f'LCSC3D-Portable-v{version}.exe', target.name]:
    path = outputs / name
    if path.exists():
        with path.open('rb') as stream:
            digest = hashlib.file_digest(stream, 'sha256').hexdigest()
        print(name, path.stat().st_size, digest)
