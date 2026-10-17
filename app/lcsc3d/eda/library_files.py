"""Publish library files with exclusive creation or recoverable set replacement."""
from __future__ import annotations

import ctypes
import hashlib
import os
from pathlib import Path
import shutil
import tempfile

from lcsc3d.core.app_paths import temporary_directory, replace_file
from lcsc3d.core.errors import DownloadError
from lcsc3d.core.app_logging import log_event, traced
from lcsc3d.services.output_transaction import output_change, output_directory_removal


def _move_new(source, target):
    if os.name == 'nt':
        from ctypes import wintypes
        move = ctypes.WinDLL('kernel32', use_last_error=True).MoveFileExW
        move.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD]
        move.restype = wintypes.BOOL
        # COPY_ALLOWED | WRITE_THROUGH, deliberately without REPLACE_EXISTING.
        if not move(str(source), str(target), 0x2 | 0x8):
            raise ctypes.WinError(ctypes.get_last_error())
    else:
        # POSIX hard links publish the complete file without replacing a winner.
        os.link(source, target)
        source.unlink()


def commit_library(root, artifacts, check_cancelled=lambda: None, *, overwrite=False):
    """Stage under app data, publish footprints first and symbols last.

    Exclusive creation is the default for callers that require collision errors.
    The exporter passes overwrite=True to replace each named library as a set.
    Failure/cancellation restores prior contents, preserving external edits.
    """
    if overwrite:
        return _replace_libraries(root, artifacts, check_cancelled)
    root = Path(root).resolve()
    paths = [(artifact, (root / artifact.path).resolve()) for artifact in artifacts]
    if len({str(path).casefold() for _, path in paths}) != len(paths):
        raise DownloadError('元件库输出路径冲突')
    pretty_dirs = {path.parent for artifact, path in paths if artifact.format == 'KICAD_FOOTPRINT'}
    for _, path in paths:
        if not path.is_relative_to(root) or path == root:
            raise DownloadError('元件库输出路径越界')
        if path.exists():
            raise DownloadError('KiCad 库已存在，请更换库名或目录；首版不支持追加：' + str(path))
    if any(path.exists() for path in pretty_dirs):
        raise DownloadError('KiCad 封装库目录已存在，请更换库名或目录')
    made_dirs, committed = [], []
    root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='library-', dir=temporary_directory()) as staging:
        staged = []
        for index, (artifact, target) in enumerate(paths):
            check_cancelled()
            source = Path(staging) / str(index)
            with source.open('wb') as stream:
                stream.write(artifact.payload)
                stream.flush()
                os.fsync(stream.fileno())
            staged.append((artifact, source, target))
        try:
            # Independent libraries can live in existing component directories
            # (which may already contain this batch's STEP/OBJ/AD output).
            containers = {path.parent for _, path in paths} | {p.parent for p in pretty_dirs}
            containers -= pretty_dirs
            outer = {c for c in containers if not any(c.is_relative_to(p) for p in pretty_dirs)}
            for container in sorted(outer):
                missing, current = [], container
                while current != root and not current.exists():
                    missing.append(current)
                    current = current.parent
                for directory in reversed(missing):
                    directory.mkdir()
                    made_dirs.append(directory)
            for directory in sorted(pretty_dirs):
                directory.mkdir()  # exclusive reservation; no existing directory is reused
                made_dirs.append(directory)
            for container in sorted(containers - outer):
                missing, current = [], container
                while current != root and not current.exists():
                    missing.append(current)
                    current = current.parent
                for directory in reversed(missing):
                    directory.mkdir()
                    made_dirs.append(directory)
            for artifact, source, target in staged:
                check_cancelled()
                _move_new(source, target)
                committed.append((artifact, target))
        except BaseException:
            for artifact, target in reversed(committed):
                try:
                    if target.read_bytes() == artifact.payload:
                        target.unlink()
                except OSError:
                    pass
            for directory in reversed(made_dirs):
                try:
                    directory.rmdir()
                except OSError:
                    pass
            raise
    return [(artifact, str(target)) for artifact, target in committed]


@traced('library.replace', lambda root, artifacts, *a: {'count': len(artifacts),
        'formats': sorted({a.format for a in artifacts})}, level='INFO')
def _replace_libraries(root, artifacts, check):
    """Replace each selected library as a set; restore old contents on failure.

    Only the named symbol files and .pretty directories participate. Staging and
    rollback copies stay in app data, including across different disk volumes.
    """
    root = Path(root).resolve()
    if not artifacts:
        return []

    def checked(path):
        resolved = path.resolve()
        if resolved == root or not resolved.is_relative_to(root):
            raise DownloadError('元件库输出路径越界')
        for item in (path, *path.parents):
            if item == root:
                break
            if item.is_symlink() or item.is_junction():
                raise DownloadError('元件库路径包含链接，无法安全整体替换：' + str(item))
        return resolved

    paths = [(a, checked(root / a.path)) for a in artifacts]
    if len({str(path).casefold() for _, path in paths}) != len(paths):
        raise DownloadError('元件库输出路径冲突')
    pretty = {p.parent for a,p in paths if a.format == 'KICAD_FOOTPRINT'}
    if any(p == root or p.suffix.lower() != '.pretty' for p in pretty):
        raise DownloadError('KiCad 封装库目录无效')
    old_files, old_dirs = set(), set()
    for directory in pretty:
        checked(directory)
        if not directory.exists():
            continue
        if not directory.is_dir():
            raise DownloadError('KiCad 封装库路径不是目录：' + str(directory))
        for base, dirs, files in os.walk(directory, followlinks=False):
            old_dirs.add(checked(Path(base)))
            for name in dirs:
                checked(Path(base)/name)
            for name in files:
                old_files.add(checked(Path(base)/name))
    for a,path in paths:
        if a.format == 'KICAD_SYMBOL' and path.exists():
            if not path.is_file():
                raise DownloadError('KiCad 符号库路径不是文件：' + str(path))
            old_files.add(path)

    def digest(path):
        with path.open('rb') as stream:
            return hashlib.file_digest(stream, 'sha256').hexdigest()

    base = Path(temporary_directory()).resolve()
    staging = Path(tempfile.mkdtemp(prefix='library-replace-', dir=base)).resolve()
    preserve_recovery = False
    try:
        commit_library(staging/'new', artifacts, check)
        before = {}
        for path in sorted(old_files):
            check()
            backup = staging/'before'/path.relative_to(root)
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, backup)
            before[path] = (backup, digest(backup))
        log_event('INFO', 'library.replacement_prepared', new_files=len(artifacts), previous_files=len(before))
        journal, made_dirs, removed_dirs = [], [], []
        def parents(path):
            missing = []
            parent = path.parent
            while parent != root and not parent.exists():
                missing.append(parent)
                parent = parent.parent
            root.mkdir(parents=True, exist_ok=True)
            for directory in reversed(missing):
                checked(directory).mkdir()
                made_dirs.append(directory)
        def unchanged(path):
            checked(path)
            if path in before and (not path.is_file() or digest(path) != before[path][1]):
                raise DownloadError('元件库在导出期间发生变化，已停止覆盖：' + str(path))
        try:
            def publish(artifact, path):
                check()
                unchanged(path)
                with output_change(path, hashlib.sha256(artifact.payload).hexdigest()):
                    parents(path)
                    source = staging/'new'/artifact.path
                    (replace_file if path in before else _move_new)(source, path)
                journal.append((path, hashlib.sha256(artifact.payload).hexdigest()))
            for artifact,path in paths:
                if artifact.format == 'KICAD_MODEL':
                    publish(artifact, path)
            for artifact,path in paths:
                if artifact.format == 'KICAD_FOOTPRINT':
                    publish(artifact, path)
            selected = {path for _,path in paths}
            for path in sorted(old_files - selected):
                check()
                unchanged(path)
                with output_change(path, None):
                    path.unlink()
                journal.append((path, None))
            for artifact,path in paths:
                if artifact.format not in ('KICAD_MODEL','KICAD_FOOTPRINT'):
                    publish(artifact, path)
            wanted_dirs = {parent for _,path in paths for parent in path.parents if parent.is_relative_to(root)}
            for directory in sorted(old_dirs - wanted_dirs, key=lambda p:len(p.parts), reverse=True):
                with output_directory_removal(directory):
                    checked(directory).rmdir()
                removed_dirs.append(directory)
            check()
            log_event('INFO', 'library.replacement_completed', files=len(paths), removed_files=len(old_files - selected))
        except BaseException as exc:
            log_event('INFO', 'library.rollback_started', changed_files=len(journal), cancelled=type(exc).__name__ == 'Cancelled')
            failures = []
            for path,written_hash in reversed(journal):
                try:
                    checked(path)
                    if (written_hash is None and path.exists()) or (written_hash is not None and
                            (not path.is_file() or digest(path) != written_hash)):
                        raise OSError('文件在回滚前发生变化')
                    if path in before:
                        path.parent.mkdir(parents=True, exist_ok=True)
                        restore = staging/'restore'
                        shutil.copy2(before[path][0], restore)
                        with output_change(path, before[path][1]):
                            (replace_file if path.exists() else _move_new)(restore, path)
                    else:
                        with output_change(path, None):
                            path.unlink()
                except Exception:
                    failures.append(str(path))
            for directory in sorted(removed_dirs, key=lambda p:len(p.parts)):
                try:
                    checked(directory).mkdir(parents=True, exist_ok=True)
                except Exception:
                    failures.append(str(directory))
            for directory in reversed(made_dirs):
                try:
                    checked(directory).rmdir()
                except Exception:
                    pass
            if failures:
                preserve_recovery = True
                log_event('ERROR', 'library.rollback_failed', failure_count=len(failures), recovery_preserved=True)
                raise DownloadError('导出失败，部分文件无法回滚；原库备份保留在：' + str(staging/'before')) from exc
            log_event('INFO', 'library.rollback_completed', restored_files=len(journal))
            raise
    finally:
        # Delete only this operation's verified, application-owned staging tree.
        if (not preserve_recovery and staging.resolve().parent == base and staging.name.startswith('library-replace-')
                and not staging.is_symlink() and not staging.is_junction()):
            shutil.rmtree(staging)
    return [(artifact,str(path)) for artifact,path in paths]
