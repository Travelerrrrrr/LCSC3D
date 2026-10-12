"""Collect redistribution notices for the pinned Windows OCP wheel and zstd.

Run explicitly when updating native dependencies. This does not run at build
or application startup. See app/licenses/CAD-SOURCES.md for collected hashes.
"""
from concurrent.futures import ThreadPoolExecutor
import hashlib
from importlib.metadata import distribution
from pathlib import Path
import urllib.request

ROOT = Path(__file__).resolve().parents[1] / 'app/licenses'
SOURCES = {
    'OCP-APACHE-2.0.txt':'https://raw.githubusercontent.com/CadQuery/OCP/master/LICENSE',
    'OCCT-LGPL-2.1.txt':'https://raw.githubusercontent.com/Open-Cascade-SAS/OCCT/V8_0_1/LICENSE_LGPL_21.txt',
    'OCCT-LGPL-EXCEPTION.txt':'https://raw.githubusercontent.com/Open-Cascade-SAS/OCCT/V8_0_1/OCCT_LGPL_EXCEPTION.txt',
    'FreeImage-LICENSE.txt':'https://raw.githubusercontent.com/WinMerge/freeimage/master/license-fi.txt',
    'FreeType-LICENSE.txt':'https://raw.githubusercontent.com/freetype/freetype/master/docs/FTL.TXT',
    'OpenEXR-LICENSE.txt':'https://raw.githubusercontent.com/AcademySoftwareFoundation/openexr/main/LICENSE.md',
    'Imath-LICENSE.txt':'https://raw.githubusercontent.com/AcademySoftwareFoundation/Imath/main/LICENSE.md',
    'libjpeg-turbo-LICENSE.txt':'https://raw.githubusercontent.com/libjpeg-turbo/libjpeg-turbo/main/LICENSE.md',
    'libjpeg-IJG.txt':'https://raw.githubusercontent.com/libjpeg-turbo/libjpeg-turbo/main/README.ijg',
    'lcms2-LICENSE.txt':'https://raw.githubusercontent.com/mm2/Little-CMS/master/LICENSE',
    'Lerc-LICENSE.txt':'https://raw.githubusercontent.com/Esri/lerc/master/LICENSE',
    'liblzma-COPYING.txt':'https://raw.githubusercontent.com/tukaani-project/xz/master/COPYING',
    'liblzma-0BSD.txt':'https://raw.githubusercontent.com/tukaani-project/xz/master/COPYING.0BSD',
    'libpng-LICENSE.txt':'https://raw.githubusercontent.com/pnggroup/libpng/libpng16/LICENSE',
    'libwebp-LICENSE.txt':'https://raw.githubusercontent.com/webmproject/libwebp/main/COPYING',
    'OpenJPEG-LICENSE.txt':'https://raw.githubusercontent.com/uclouvain/openjpeg/master/LICENSE',
    'OpenJPH-LICENSE.txt':'https://raw.githubusercontent.com/aous72/OpenJPH/master/LICENSE',
    'LibRaw-COPYRIGHT.txt':'https://raw.githubusercontent.com/LibRaw/LibRaw/master/COPYRIGHT',
    'LibRaw-LICENSE.txt':'https://raw.githubusercontent.com/LibRaw/LibRaw/master/LICENSE.LGPL',
    'libtiff-LICENSE.txt':'https://raw.githubusercontent.com/libsdl-org/libtiff/master/LICENSE.md',
    'zlib-LICENSE.txt':'https://raw.githubusercontent.com/madler/zlib/develop/LICENSE',
    'zstd-LICENSE.txt':'https://raw.githubusercontent.com/facebook/zstd/dev/LICENSE',
    'libdeflate-LICENSE.txt':'https://raw.githubusercontent.com/ebiggers/libdeflate/master/COPYING',
}


def fetch(item):
    name,url = item
    with urllib.request.urlopen(url,timeout=30) as response:
        data = response.read()
    if not data or b'<html' in data[:200].lower():
        raise ValueError('Not a license text: '+url)
    (ROOT/name).write_bytes(data)
    return f'| {name} | [Upstream]({url}) | `{hashlib.sha256(data).hexdigest()}` |'


if __name__=='__main__':
    with ThreadPoolExecutor(max_workers=6) as pool:
        rows = list(pool.map(fetch,SOURCES.items()))
    dist = distribution('zstandard')
    license_file = next(p for p in dist.files if str(p).endswith('/licenses/LICENSE'))
    data = dist.locate_file(license_file).read_bytes()
    (ROOT/'python-zstandard-LICENSE.txt').write_bytes(data)
    rows.append(f'| python-zstandard-LICENSE.txt | zstandard {dist.version} wheel | `{hashlib.sha256(data).hexdigest()}` |')
    (ROOT/'CAD-SOURCES.md').write_text('# CAD dependency notice sources\n\n'
        'Pinned runtime: cadquery-ocp-novtk 8.0.1.1.0 (OCCT 8.0.1) and zstandard 0.25.0. '
        'FreeImage and libtiff license texts are retrieved from maintained source mirrors. '
        'These copies preserve upstream copyright notices.\n\n'
        '| File | Source | SHA-256 |\n|---|---|---|\n'+'\n'.join(rows)+'\n','utf-8')
    print('Collected',len(rows),'CAD dependency notices')
