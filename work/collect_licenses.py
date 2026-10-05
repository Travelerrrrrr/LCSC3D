from pathlib import Path
from importlib.metadata import distribution
import shutil
import requests
import sys

root = Path(__file__).parent / 'app' / 'licenses'
for name, target in [('pyinstaller', 'PyInstaller-COPYING.txt'), ('certifi', 'certifi-LICENSE.txt')]:
    dist = distribution(name)
    source = next(p for p in dist.files if '/licenses/' in str(p) and ('COPYING' in str(p) or 'LICENSE' in str(p)))
    shutil.copyfile(dist.locate_file(source), root / target)
shutil.copyfile(Path(sys.base_prefix) / 'LICENSE.txt', root / 'Python-LICENSE.txt')
for name, url in [('LGPL-3.0.txt', 'https://raw.githubusercontent.com/qt/qtbase/dev/LICENSES/LGPL-3.0-only.txt'), ('GPL-3.0.txt', 'https://raw.githubusercontent.com/qt/qtbase/dev/LICENSES/GPL-3.0-only.txt')]:
    response = requests.get(url, timeout=25)
    response.raise_for_status()
    (root / name).write_bytes(response.content)
print('License notices collected')
