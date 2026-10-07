# PyInstaller specification. Bundle the native AD encoder and format notices
# alongside the browser runtime; no external converter is needed.
from pathlib import Path

root = Path(SPECPATH)

a = Analysis(
    [str(root / 'launcher.py')],
    pathex=[str(root)],
    binaries=[],
    datas=[(str(root / 'viewer.html'), '.'), (str(root / 'vector_viewer.html'), '.'),
           (str(root / 'assets'), 'assets'), (str(root / 'licenses'), 'licenses')],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['tkinter', 'matplotlib', 'numpy', 'pandas', 'PyQt5', 'PyQt6',
              'PySide6.QtQml', 'PySide6.QtQuick', 'PySide6.QtQuickWidgets',
              'PySide6.QtOpenGL'],
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

# The GUI uses Widgets and WebEngine, not Qt's QML application modules. Keep
# their native DLL dependencies but omit QML plugins, Python bindings, debug
# browser resources, developer tools and unused browser translations.
def needed_resource(entry):
    name = entry[0].replace('\\', '/').lower()
    if '/qml/' in name or '.debug.' in name:
        return False
    if '/qtwebengine_locales/' in name:
        return Path(name).name in {'en-us.pak', 'zh-cn.pak'}
    if name.endswith('/qtwebengine_devtools_resources.pak'):
        return False
    if '/translations/' in name:
        return False
    return True

a.binaries = [entry for entry in a.binaries if needed_resource(entry)]
a.datas = [entry for entry in a.datas if needed_resource(entry)]
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, a.binaries, a.datas, [],
    name='LCSC3D',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    icon=str(root / 'assets' / 'app.ico'),
    version=str(root / 'version_info.txt'),
)
