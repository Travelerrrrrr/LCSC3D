"""Undo only the files changed by a cancelled download batch."""
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import threading

from lcsc3d.core.app_paths import temporary_directory, replace_file
from lcsc3d.core.app_logging import log_event, record_error


_current = ContextVar('download_outputs', default=None)
_UNKNOWN = object()


class BatchCancellation(threading.Event):
    """Once outputs are finalized, a late click cannot cancel a finished batch."""
    def __init__(self):
        super().__init__()
        self._gate = threading.Lock()
        self._finished = False

    def set(self):
        with self._gate:
            if self._finished:
                return False
            super().set()
            return True

    def finish(self):
        with self._gate:
            self._finished = True
            return self.is_set()


def checked_path(path):
    path = Path(path).absolute()
    for item in (path, *path.parents):
        if item.is_symlink() or item.is_junction():
            raise OSError('输出路径包含链接，已停止修改')
    return path.resolve()


def digest(path):
    if not path.exists():
        return None
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


@contextmanager
def suspend_output_tracking():
    token = _current.set(None)
    try:
        yield
    finally:
        _current.reset(token)


@contextmanager
def output_change(path, expected=_UNKNOWN):
    transaction = _current.get()
    if transaction is None:
        yield
    else:
        with transaction.change(path, expected):
            yield


def track_file_write(function):
    @wraps(function)
    def wrapped(path, *args, **kwargs):
        if _current.get() is None:
            return function(path, *args, **kwargs)
        payload = args[0] if args else kwargs['data']
        with output_change(path, hashlib.sha256(payload).hexdigest()):
            return function(path, *args, **kwargs)
    return wrapped


@contextmanager
def output_directory_removal(path):
    transaction = _current.get()
    if transaction is not None:
        with transaction.lock:
            transaction.removed_directories.add(checked_path(path))
    yield


class OutputTransaction:
    def __init__(self):
        self.lock = threading.RLock()
        self.files = {}
        self.created_directories = set()
        self.removed_directories = set()
        self.recovery = None
        self.preserve_recovery = False

    def __enter__(self):
        self.token = _current.set(self)
        return self

    def __exit__(self, *exception):
        _current.reset(self.token)
        if self.recovery and not self.preserve_recovery:
            # Only our own AppData staging tree is eligible for recursive cleanup.
            root = Path(temporary_directory()).resolve()
            if self.recovery.parent == root and not self.recovery.is_symlink() and not self.recovery.is_junction():
                try:
                    shutil.rmtree(self.recovery)
                except OSError as exc:
                    record_error(exc, 'download.recovery_cleanup_failed', stage='cleanup')

    @contextmanager
    def change(self, target, expected=_UNKNOWN):
        with self.lock:
            path = checked_path(target)
            if path not in self.files:
                before = digest(path)
                if self.recovery is None:
                    self.recovery = Path(tempfile.mkdtemp(prefix='download-rollback-', dir=temporary_directory())).resolve()
                backup = self.recovery / str(len(self.files))
                if before is not None:
                    shutil.copy2(path, backup)
                    if digest(backup) != before or digest(path) != before:
                        raise OSError('输出文件在备份期间发生变化')
                self.files[path] = {'before': before, 'after': before, 'backup': backup}
                parent = path.parent
                while not parent.exists():
                    self.created_directories.add(parent)
                    parent = parent.parent
            prior = digest(path)
            if prior != self.files[path]['after']:
                raise OSError('输出文件在本批次写入后发生变化，保留外部修改')
            try:
                yield
            finally:
                # Record even a write that raises after replacing the target.
                current = digest(checked_path(path))
                if expected is _UNKNOWN or current == prior:
                    self.files[path]['after'] = current
                else:
                    self.files[path]['after'] = expected

    def rollback(self):
        failures = []
        log_event('INFO', 'download.rollback_started', count=len(self.files), stage='rollback')
        for path, entry in reversed(list(self.files.items())):
            try:
                current = digest(checked_path(path))
                if current == entry['before']:
                    continue  # A nested library operation already restored this file.
                if current != entry['after']:
                    raise OSError('文件在取消清理前发生变化，保留外部修改')
                if entry['before'] is None:
                    path.unlink(missing_ok=True)
                else:
                    if digest(entry['backup']) != entry['before']:
                        raise OSError('取消恢复副本损坏')
                    path.parent.mkdir(parents=True, exist_ok=True)
                    stage = self.recovery / 'restore'
                    shutil.copy2(entry['backup'], stage)
                    replace_file(stage, path)
                log_event('DEBUG', 'download.output_reverted', format=path.suffix.lower(), restored=entry['before'] is not None)
            except Exception as exc:
                failures.append(path)
                record_error(exc, 'download.rollback_file_failed', format=path.suffix.lower(), stage='rollback')
        for directory in sorted(self.removed_directories - self.created_directories, key=lambda p: len(p.parts)):
            try:
                checked_path(directory).mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                failures.append(directory)
                record_error(exc, 'download.rollback_directory_failed', stage='restore')
        for directory in sorted(self.created_directories, key=lambda p: len(p.parts), reverse=True):
            try:
                checked_path(directory).rmdir()  # Never recursively remove user output.
            except FileNotFoundError:
                pass
            except OSError as exc:
                # New unrelated files must be preserved. Report that cleanup was incomplete.
                failures.append(directory)
                record_error(exc, 'download.rollback_directory_failed', stage='remove')
        if failures:
            self.preserve_recovery = True
            if self.recovery is None:
                self.recovery = Path(tempfile.mkdtemp(prefix='download-rollback-', dir=temporary_directory())).resolve()
            manifest = {'files': [{'target': str(path), 'original_sha256': row['before'],
                                   'written_sha256': row['after'], 'backup': row['backup'].name}
                                  for path, row in self.files.items()], 'failed': [str(p) for p in failures]}
            try:
                (self.recovery / 'recovery.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), 'utf-8')
            except OSError as exc:
                record_error(exc, 'download.recovery_manifest_failed', stage='rollback')
        log_event('ERROR' if failures else 'INFO', 'download.rollback_failed' if failures else 'download.rollback_completed',
                  count=len(self.files), failure_count=len(failures), recovery_preserved=bool(failures))
        return failures
