"""Build the offline Altium converter and collect its private Windows DLLs."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

import pefile  # Provided by the Windows PyInstaller build dependency.


def source_hash(root: Path) -> str:
    digest = hashlib.sha256()
    sources = [root / 'CMakeLists.txt', root / 'main.cpp', root / 'normalize.h']
    sources += sorted((root / 'vendor').rglob('*.h')) + sorted((root / 'vendor').rglob('*.cpp'))
    for path in sources:
        digest.update(path.relative_to(root).as_posix().encode('utf-8'))
        digest.update(path.read_bytes())
    return digest.hexdigest()


def verify_runtime(root: Path):
    runtime = root / 'runtime'
    manifest = json.loads((runtime / 'runtime-manifest.json').read_text(encoding='utf-8'))
    if manifest.get('source_sha256') != source_hash(root):
        raise RuntimeError('Native sources changed; rebuild the Altium converter before packaging')
    for filename, expected in manifest['files'].items():
        if hashlib.sha256((runtime / filename).read_bytes()).hexdigest() != expected:
            raise RuntimeError(f'Native runtime file is missing or changed: {filename}')


def build(qt_prefix: Path, compiler_prefix: Path, ninja: str | None = None) -> Path:
    root = Path(__file__).resolve().parent
    qt_prefix, compiler_prefix = qt_prefix.resolve(), compiler_prefix.resolve()
    if not (qt_prefix / 'lib/cmake/Qt6').is_dir():
        raise RuntimeError('Qt 6 development files were not found at --qt-prefix')
    env = os.environ.copy()
    env['PATH'] = os.pathsep.join([str(qt_prefix / 'bin'), str(compiler_prefix), env.get('PATH', '')])
    compiler = compiler_prefix / 'g++.exe'
    if not compiler.is_file():
        raise RuntimeError('This Windows build uses MinGW; g++.exe was not found')
    ninja = ninja or shutil.which('ninja', path=env['PATH'])
    if not ninja:
        raise RuntimeError('Install Ninja or supply --ninja')
    build_dir = root / 'build'
    subprocess.run([
        'cmake', '-S', str(root), '-B', str(build_dir), '-G', 'Ninja',
        '-DCMAKE_BUILD_TYPE=Release', f'-DCMAKE_PREFIX_PATH={qt_prefix.as_posix()}',
        f'-DCMAKE_CXX_COMPILER={compiler.as_posix()}', f'-DCMAKE_MAKE_PROGRAM={Path(ninja).resolve().as_posix()}',
    ], check=True, env=env)
    subprocess.run(['cmake', '--build', str(build_dir), '--parallel', '4'], check=True, env=env)
    runtime = root / 'runtime'
    runtime.mkdir(exist_ok=True)
    search_dirs = [qt_prefix / 'bin', compiler_prefix]
    candidates = {path.name.lower(): path for folder in reversed(search_dirs) for path in folder.glob('*.dll')}
    system = Path(os.environ.get('WINDIR', r'C:\Windows')) / 'System32'
    pending, collected = [build_dir / 'lcsc-altium.exe'], {}
    while pending:
        source = pending.pop()
        key = source.name.lower()
        if key in collected:
            continue
        collected[key] = source
        with pefile.PE(str(source), fast_load=True) as binary:
            binary.parse_data_directories(directories=[pefile.DIRECTORY_ENTRY['IMAGE_DIRECTORY_ENTRY_IMPORT']])
            for entry in getattr(binary, 'DIRECTORY_ENTRY_IMPORT', []):
                name = entry.dll.decode('ascii').lower()
                if name in candidates:
                    pending.append(candidates[name])
                elif not name.startswith(('api-ms-', 'ext-ms-')) and not (system / name).is_file():
                    raise RuntimeError(f'Missing runtime dependency: {name} (from {source.name})')
    # Remove obsolete generated binaries only inside the verified private runtime directory.
    for old in runtime.iterdir():
        if old.suffix.lower() in ('.exe', '.dll') and old.name.lower() not in collected:
            old.unlink()
    for source in collected.values():
        shutil.copyfile(source, runtime / source.name)
    # Preserve package notices supplied with both Qt and the compiler toolchain.
    for prefix in {qt_prefix, compiler_prefix.parent}:
        notices = prefix / 'share/licenses'
        if notices.is_dir():
            for directory in notices.iterdir():
                if directory.is_dir() and any(term in directory.name.lower() for term in (
                    'qt6-base', 'icu', 'pcre2', 'double-conversion', 'libb2', 'zlib', 'zstd',
                    'gcc-libs', 'libpng', 'harfbuzz', 'freetype', 'brotli', 'graphite', 'libjpeg',
                    'glib2', 'libiconv', 'gettext', 'bzip2', 'md4c', 'winpthreads',
                )):
                    shutil.copytree(directory, runtime / 'licenses' / directory.name, dirs_exist_ok=True)
    manifest = {
        'converter': 'EasyKiConverter',
        'source_sha256': source_hash(root),
        'upstream_commit': json.loads((root / 'vendor/EasyKiConverter/upstream.json').read_text())['commit'],
        'files': {source.name: hashlib.sha256((runtime / source.name).read_bytes()).hexdigest()
                  for source in sorted(collected.values())},
    }
    (runtime / 'runtime-manifest.json').write_text(json.dumps(manifest, indent=2) + '\n', encoding='utf-8')
    smoke_env = {key: value for key, value in env.items()
                 if not key.startswith(('QT_', 'QML_', 'PYTHON'))}
    smoke_env['PATH'] = str(system)
    check = subprocess.run([str(runtime / 'lcsc-altium.exe'), '--version'], check=True,
                           env=smoke_env, capture_output=True, timeout=15,
                           creationflags=subprocess.CREATE_NO_WINDOW)
    print(check.stdout.decode('utf-8').strip())
    return runtime


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--qt-prefix', type=Path)
    parser.add_argument('--compiler-prefix', type=Path, help='Directory containing g++.exe and MinGW DLLs')
    parser.add_argument('--ninja')
    parser.add_argument('--verify', action='store_true', help='Verify a previously built private runtime')
    args = parser.parse_args()
    if args.verify:
        verify_runtime(Path(__file__).resolve().parent)
        print('Native runtime verified')
    elif args.qt_prefix and args.compiler_prefix:
        print(build(args.qt_prefix, args.compiler_prefix, args.ninja))
    else:
        parser.error('Supply --qt-prefix and --compiler-prefix, or --verify')
