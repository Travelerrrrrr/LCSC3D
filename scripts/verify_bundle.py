"""Compare frozen application modules/assets and an optional source ZIP to this checkout."""
import argparse
import json
from pathlib import Path
import types
import zipfile

from PyInstaller.archive.readers import CArchiveReader


def normalized(code):
    return code.replace(co_filename='', co_consts=tuple(
        normalized(value) if isinstance(value, types.CodeType) else value for value in code.co_consts))


def verify(executable, source_zip=None):
    root = Path(__file__).resolve().parents[1]
    archive = CArchiveReader(str(executable))
    pyz = archive.open_embedded_archive('PYZ.pyz')
    modules = []
    for source in sorted((root / 'app').glob('*.py')):
        if source.stem == 'launcher':
            continue  # PyInstaller rewrites the entry script; application code lives in PYZ.
        code = compile(source.read_text(encoding='utf-8'), '', 'exec', dont_inherit=True)
        assert normalized(pyz.extract(source.stem)) == normalized(code), source.name
        modules.append(source.stem)
    entries = {name.replace('\\', '/'): name for name in archive.toc}
    sources = [root / 'app' / name for name in ('viewer.html', 'vector_viewer.html')]
    sources += [p for folder in ('assets', 'licenses') for p in (root / 'app' / folder).rglob('*') if p.is_file()]
    for source in sources:
        name = source.relative_to(root / 'app').as_posix()
        assert archive.extract(entries[name]) == source.read_bytes(), name
    assert 'qt_material' in pyz.toc and 'jinja2' in pyz.toc and 'markupsafe' in pyz.toc
    assert 'qt_material/material.qss.template' in entries
    assert any(name.startswith('qt_material/resources/source/') and name.endswith('.svg') for name in entries)
    assert not any(name.startswith('qt_material/fonts/') for name in entries), 'Unexpected bundled fonts'
    count = 0
    if source_zip:
        with zipfile.ZipFile(source_zip) as source:
            assert source.testzip() is None
            for name in source.namelist():
                assert name.startswith('LCSC3D/')
                relative = Path(name.removeprefix('LCSC3D/'))
                assert '..' not in relative.parts and not relative.is_absolute()
                assert not any(part in {'work', 'outputs', '.venv', '__pycache__', 'runtime'} for part in relative.parts)
                assert relative.name not in {'LCSC3D-settings.json', 'store-session.bin'}
                assert source.read(name) == (root / relative).read_bytes(), name
                count += 1
    return dict(executable=str(executable.resolve()), modules=modules, resources=len(sources),
                qt_material_bundled=True, source_files=count, success=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--exe', type=Path, required=True)
    parser.add_argument('--source-zip', type=Path)
    parser.add_argument('--report', type=Path)
    args = parser.parse_args()
    result = verify(args.exe, args.source_zip)
    if args.report:
        args.report.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False))
