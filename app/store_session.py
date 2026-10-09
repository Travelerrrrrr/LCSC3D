"""Remember storefront cookies using Windows current-user DPAPI encryption."""
from __future__ import annotations

import ctypes
import http.cookiejar
import json
import os
import tempfile
import threading
import time
from pathlib import Path

from store import StoreError, check_cancelled
from app_paths import data_directory
from app_logging import traced, log_event, record_error

MAGIC = b'LCSC3D-STORE-1\0'
COOKIE_DOMAINS = ('jlc.com', 'szlcsc.com')


class Blob(ctypes.Structure):
    _fields_ = [('size', ctypes.c_ulong), ('data', ctypes.POINTER(ctypes.c_ubyte))]


@traced('session.crypt', lambda data, decrypt=False: {'stage': 'decrypt' if decrypt else 'encrypt'})
def crypt(data, *, decrypt=False):
    if os.name != 'nt':
        raise StoreError('记住登录需要 Windows 用户加密功能。')
    buffer = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    source = Blob(len(data), buffer)
    entropy_buffer = (ctypes.c_ubyte * len(MAGIC)).from_buffer_copy(MAGIC)
    entropy = Blob(len(MAGIC), entropy_buffer)
    output = Blob()
    library = ctypes.WinDLL('crypt32', use_last_error=True)
    function = library.CryptUnprotectData if decrypt else library.CryptProtectData
    function.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p if decrypt else ctypes.c_wchar_p,
                         ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong,
                         ctypes.POINTER(Blob)]
    function.restype = ctypes.c_int
    description = None if decrypt else 'LCSC3D storefront session'
    if not function(ctypes.byref(source), description, ctypes.byref(entropy), None, None, 1, ctypes.byref(output)):
        log_event('ERROR', 'session.dpapi_failed', winerror=ctypes.get_last_error())
        raise StoreError('无法读取或保存本机的加密登录状态。')
    try:
        return ctypes.string_at(output.data, output.size)
    finally:
        free = ctypes.WinDLL('kernel32').LocalFree
        free.argtypes, free.restype = [ctypes.c_void_p], ctypes.c_void_p
        free(ctypes.cast(output.data, ctypes.c_void_p))


class SessionVault:
    def __init__(self, path=None, *, domains=COOKIE_DOMAINS):
        self.path = Path(path) if path is not None else data_directory() / 'store-session.bin'
        self.domains = domains
        self.lock = threading.RLock()
        self.generation = 0

    def exists(self):
        return self.path.is_file()

    def allowed_domain(self, domain):
        host = domain.lstrip('.').lower()
        return any(host == value or host.endswith('.' + value) for value in self.domains)

    @traced('session.save', level='INFO')
    def save(self, client, ticket, cancelled=None):
        # Snapshot before locking the vault; network requests can own the cookie lock.
        with client.session.lock:
            records = [{'name': c.name, 'value': c.value, 'domain': c.domain, 'path': c.path,
                        'secure': c.secure, 'expires': c.expires, 'rest': dict(c._rest)}
                       for c in client.session.cookies if self.allowed_domain(c.domain) and not c.is_expired()]
        check_cancelled(cancelled)
        if not records:
            raise StoreError('本次登录成功，但没有可记住的商城会话。')
        plain = json.dumps({'version': 1, 'saved_at': int(time.time()), 'cookies': records}, ensure_ascii=False).encode('utf-8')
        encrypted = MAGIC + crypt(plain)
        with self.lock:
            check_cancelled(cancelled)
            if ticket != self.generation:
                log_event('DEBUG', 'session.stale_save_skipped')
                return False
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(dir=self.path.parent, prefix='.store-session-', delete=False) as stream:
                    temporary = Path(stream.name)
                    stream.write(encrypted)
                    stream.flush()
                    os.fsync(stream.fileno())
                check_cancelled(cancelled)
                os.replace(temporary, self.path)
            except OSError as exc:
                record_error(exc, 'session.write_failed')
                raise StoreError('本次登录成功，但无法保存登录状态，请检查本机目录权限。') from None
            finally:
                if temporary:
                    temporary.unlink(missing_ok=True)
        return True

    @traced('session.load', level='INFO')
    def load_into(self, client):
        with self.lock:
            if not self.exists():
                log_event('DEBUG', 'session.no_saved_state')
                return False
            try:
                if self.path.stat().st_size > 256 * 1024:
                    raise ValueError()
                data = self.path.read_bytes()
                if not data.startswith(MAGIC):
                    raise ValueError()
                value = json.loads(crypt(data[len(MAGIC):], decrypt=True))
                records = value['cookies']
                if value.get('version') != 1 or not isinstance(records, list) or len(records) > 300:
                    raise ValueError()
                for record in records:
                    domain, name, cookie_value = record['domain'], record['name'], record['value']
                    if (not isinstance(domain, str) or not self.allowed_domain(domain) or not isinstance(name, str)
                            or not isinstance(cookie_value, str) or len(name) > 256 or len(cookie_value) > 16384):
                        continue
                    expires = record.get('expires')
                    if expires is not None and (not isinstance(expires, (int, float)) or expires <= time.time()):
                        continue
                    cookie = http.cookiejar.Cookie(0, name, cookie_value, None, False, domain, True,
                                                  domain.startswith('.'), record.get('path') or '/', True,
                                                  bool(record.get('secure')), expires, expires is None,
                                                  None, None, record.get('rest') or {}, False)
                    client.session.cookies.set_cookie(cookie)
                present = bool(list(client.session.cookies))
                log_event('INFO', 'session.load_result', usable=present)
                return present
            except (StoreError, OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
                record_error(exc, 'session.saved_state_invalid', level='WARNING')
                self.delete()
                return False

    @traced('session.delete', level='INFO')
    def delete(self):
        with self.lock:
            self.generation += 1
            try:
                self.path.unlink(missing_ok=True)
            except OSError as exc:
                record_error(exc, 'session.delete_failed')
                raise StoreError('无法清除本机的登录状态，请检查目录权限。') from None


class MemoryOnlyVault:
    generation = 0

    def exists(self):
        return False

    def delete(self):
        self.generation += 1

    def load_into(self, client):
        return False

    def save(self, client, ticket, cancelled=None):
        return False
