# PyInstaller specification. The browser runtime is bundled inside the EXE.
from pathlib import Path
import re

root = Path(SPECPATH)
upstream = root / 'upstream'
if not upstream.is_dir():
    upstream = root.parent / 'upstream'
version = re.search(r"VERSION = '([^']+)'", (root / 'main.py').read_text(encoding='utf-8')).group(1)

a = Analysis(
    [str(root / 'launcher.py')],
    pathex=[str(root), str(upstream)],
    binaries=[],
    datas=[(str(root / 'viewer.html'), '.'), (str(root / 'vector_viewer.html'), '.'),
           (str(root / 'assets'), 'assets'), (str(root / 'licenses'), 'licenses')],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['tkinter', 'matplotlib', 'numpy', 'pandas', 'PyQt5', 'PyQt6'],
    noarchive=False,
)
# Qt 6 on Windows uses the OS ICU API with unsuffixed exports. Build machines
# may have Poppler/Conda ICU DLLs on PATH with suffixed exports (ucnv_open_78).
# Shipping those same-name DLLs shadows Windows ICU and breaks QtCore imports.
# Chromium's separate resources/icudtl.dat is intentionally retained.
a.binaries = [entry for entry in a.binaries
    if not (Path(entry[0]).name.lower() in {'icuuc.dll', 'icuin.dll'}
            or (Path(entry[0]).name.lower().startswith('icudt')
                and Path(entry[0]).suffix.lower() == '.dll'))]
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, a.binaries, a.datas, [],
    name=f'LCSC3D-Portable-v{version}',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    icon=str(root / 'assets' / 'app.ico'),
)
