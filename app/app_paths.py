"""All application-owned files live beneath the current user's local app data."""
from __future__ import annotations

import errno
import os
from pathlib import Path
import sys
import tempfile


def data_directory():
    local = os.environ.get('LOCALAPPDATA')
    base = Path(local) if local else Path.home() / 'AppData' / 'Local'
    return base / 'LCSC3D'


def temporary_directory():
    path = data_directory() / 'temp'
    path.mkdir(parents=True, exist_ok=True)
    return path


def updates_directory():
    path = data_directory() / 'updates'
    path.mkdir(parents=True, exist_ok=True)
    return path


def configure_runtime_paths():
    path = str(temporary_directory())
    os.environ['TEMP'] = path
    os.environ['TMP'] = path
    os.environ['TMPDIR'] = path
    tempfile.tempdir = None


def replace_file(source, target):
    """Use a flushed Windows move when app data and the destination differ."""
    try:
        os.replace(source, target)
    except OSError as exc:
        if sys.platform != 'win32' or (exc.errno != errno.EXDEV and getattr(exc, 'winerror', None) != 17):
            raise
        from app_logging import log_event
        log_event('DEBUG', 'file.cross_volume_move', stage='copy_and_replace')
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        move = kernel.MoveFileExW
        move.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD]
        move.restype = wintypes.BOOL
        # REPLACE_EXISTING | COPY_ALLOWED | WRITE_THROUGH; no sidecar at target.
        if not move(str(source), str(target), 0x1 | 0x2 | 0x8):
            raise ctypes.WinError(ctypes.get_last_error())
