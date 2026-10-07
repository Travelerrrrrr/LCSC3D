"""Package source and user-facing documentation, excluding scratch/cache files."""
import hashlib
import shutil
import zipfile
from pathlib import Path

workspace = Path(__file__).resolve().parent.parent
app = workspace / 'app'
outputs = workspace / 'outputs'
outputs.mkdir(exist_ok=True)
target = outputs / 'LCSC3D.zip'
with zipfile.ZipFile(target, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
    for base, prefix in [(workspace / folder, Path('LCSC3D') / folder)
                         for folder in ('app', 'scripts', 'docs', '.github')]:
        for source in base.rglob('*'):
            if not source.is_file():
                continue
            if source.suffix.lower() in {'.csv', '.log'} or source.name == 'LCSC3D-settings.json':
                continue
            relative = source.relative_to(base)
            if any(part in {'.git', '__pycache__', '.venv', 'build', 'dist', 'runtime'} for part in relative.parts):
                continue
            archive.write(source, str(prefix / relative))
    for filename in ('README.md', 'LICENSE', 'CONTRIBUTING.md', '.gitignore', '.gitattributes'):
        archive.write(workspace / filename, str(Path('LCSC3D') / filename))
shutil.copyfile(app / 'README.md', outputs / '使用说明.md')
for name in ['LCSC3D.exe', target.name]:
    path = outputs / name
    if path.exists():
        with path.open('rb') as stream:
            digest = hashlib.file_digest(stream, 'sha256').hexdigest()
        print(name, path.stat().st_size, digest)
