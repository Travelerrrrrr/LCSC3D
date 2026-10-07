"""Create native OLE compound files using Windows structured storage.

Each export owns its COM apartment, storage and HGLOBAL. No file is left on
disk if serialization fails; callers commit the returned bytes atomically.
"""
from __future__ import annotations

import ctypes as ct
from functools import lru_cache
import sys

from errors import DownloadError


@lru_cache(maxsize=1)
def _api():
    if sys.platform != 'win32':
        raise DownloadError('AD 元件库导出需要 Windows 10/11')
    ole = ct.WinDLL('ole32')
    kernel = ct.WinDLL('kernel32', use_last_error=True)
    pointer = ct.c_void_p
    out = ct.POINTER(pointer)
    declarations = {
        'CoInitializeEx': ([pointer, ct.c_uint32], ct.c_int32),
        'CoUninitialize': ([], None),
        'CreateILockBytesOnHGlobal': ([pointer, ct.c_int32, out], ct.c_int32),
        'StgCreateDocfileOnILockBytes': ([pointer, ct.c_uint32, ct.c_uint32, out], ct.c_int32),
        'GetHGlobalFromILockBytes': ([pointer, out], ct.c_int32),
    }
    for name, (arguments, result) in declarations.items():
        function = getattr(ole, name)
        function.argtypes, function.restype = arguments, result
    kernel.GlobalSize.argtypes, kernel.GlobalSize.restype = [pointer], ct.c_size_t
    kernel.GlobalLock.argtypes, kernel.GlobalLock.restype = [pointer], pointer
    kernel.GlobalUnlock.argtypes, kernel.GlobalUnlock.restype = [pointer], ct.c_int32
    return ole, kernel


def _check(hr):
    if hr < 0:
        raise DownloadError(f'AD 复合文件写入失败（HRESULT 0x{hr & 0xffffffff:08X}）')


def _method(interface, slot, result, *argument_types):
    table = ct.cast(interface, ct.POINTER(ct.POINTER(ct.c_void_p))).contents
    return ct.WINFUNCTYPE(result, ct.c_void_p, *argument_types)(table[slot])


def _release(interface):
    if interface:
        _method(interface, 2, ct.c_uint32)(interface)


def compound_file(streams: dict[str, bytes], check_cancelled=lambda: None) -> bytes:
    """Write slash-separated stream paths into an in-memory CFB v3 file."""
    ole, kernel = _api()
    initialized = ole.CoInitializeEx(None, 0)
    # An existing GUI COM apartment is also valid for structured storage.
    if initialized != -2147417850:  # RPC_E_CHANGED_MODE
        _check(initialized)
    lock_bytes, root = ct.c_void_p(), ct.c_void_p()
    storages = {}
    try:
        _check(ole.CreateILockBytesOnHGlobal(None, 1, ct.byref(lock_bytes)))
        _check(ole.StgCreateDocfileOnILockBytes(lock_bytes, 0x1012, 0, ct.byref(root)))
        storages[''] = root
        for path, payload in streams.items():
            check_cancelled()
            parts = path.split('/')
            if any(not part or len(part.encode('utf-16le')) > 62 or any(c in part for c in '\\:!') for part in parts):
                raise DownloadError('AD 库存储名称无效：' + path)
            parent_path = ''
            for part in parts[:-1]:
                key = parent_path + '/' + part if parent_path else part
                if key not in storages:
                    storage = ct.c_void_p()
                    create = _method(storages[parent_path], 5, ct.c_int32,
                                     ct.c_wchar_p, ct.c_uint32, ct.c_uint32, ct.c_uint32, ct.POINTER(ct.c_void_p))
                    _check(create(storages[parent_path], part, 0x1012, 0, 0, ct.byref(storage)))
                    storages[key] = storage
                parent_path = key
            stream = ct.c_void_p()
            try:
                create = _method(storages[parent_path], 3, ct.c_int32,
                                 ct.c_wchar_p, ct.c_uint32, ct.c_uint32, ct.c_uint32, ct.POINTER(ct.c_void_p))
                _check(create(storages[parent_path], parts[-1], 0x1012, 0, 0, ct.byref(stream)))
                if payload:
                    written = ct.c_uint32()
                    write = _method(stream, 4, ct.c_int32, ct.c_void_p, ct.c_uint32, ct.POINTER(ct.c_uint32))
                    _check(write(stream, payload, len(payload), ct.byref(written)))
                    if written.value != len(payload):
                        raise DownloadError('AD 库存储写入不完整')
            finally:
                _release(stream)
        _check(_method(root, 9, ct.c_int32, ct.c_uint32)(root, 0))
        check_cancelled()
        memory = ct.c_void_p()
        _check(ole.GetHGlobalFromILockBytes(lock_bytes, ct.byref(memory)))
        size = kernel.GlobalSize(memory)
        address = kernel.GlobalLock(memory)
        if not address:
            raise DownloadError('无法读取生成的 AD 库文件')
        try:
            return ct.string_at(address, size)
        finally:
            kernel.GlobalUnlock(memory)
    finally:
        for storage in reversed(list(storages.values())):
            _release(storage)
        _release(lock_bytes)
        if initialized >= 0:
            ole.CoUninitialize()
