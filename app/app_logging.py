"""Level-controlled, bounded application logs with explicit safe fields."""
from __future__ import annotations

from datetime import datetime
from contextlib import contextmanager
from contextvars import ContextVar, copy_context
from functools import wraps
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import platform
import re
import sys
import threading
import time
import uuid
import atexit

from app_paths import data_directory


LOG_LEVELS = ('DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL')
MAX_LOG_BYTES = 2 * 1024 * 1024
LOG_BACKUPS = 3
LOGGER = logging.getLogger('LCSC3D')
LOGGER.propagate = False
LOGGER.addHandler(logging.NullHandler())
LOGGER.setLevel(logging.DEBUG)
_LOCK = threading.RLock()
_handler = None
_version = None
_session_id = uuid.uuid4().hex
_context = ContextVar('log_context', default={})
_write_error = None
_role = 'application'
_crash_stream = None
_run_marker = None


def new_context(**fields):
    parent = current_context()
    identity = uuid.uuid4().hex
    return {**parent, **fields, 'operation_id': identity,
            'parent_id': parent.get('operation_id'), 'root_id': parent.get('root_id', identity)}


def current_context():
    return dict(_context.get())


@contextmanager
def log_context(context=None, **fields):
    token = _context.set({**(current_context() if context is None else context), **fields})
    try:
        yield
    finally:
        _context.reset(token)


def contextual(function):
    """Restore the context captured when a Qt worker was created."""
    @wraps(function)
    def wrapper(self, *args, **kwargs):
        with log_context(self.log_context):
            return function(self, *args, **kwargs)
    return wrapper


def submit_logged(pool, function, *args, **kwargs):
    return pool.submit(copy_context().run, function, *args, **kwargs)


@contextmanager
def operation(event, *, level='DEBUG', **fields):
    with log_context(new_context(**fields)):
        started = time.monotonic()
        log_event(level, event + '.started')
        try:
            yield
        except BaseException as exc:
            elapsed = round((time.monotonic() - started) * 1000)
            if type(exc).__name__ == 'Cancelled':
                log_event('INFO', event + '.cancelled', elapsed_ms=elapsed)
            elif type(exc).__name__ == 'CaptchaRequired':
                log_event('INFO', event + '.challenge_required', elapsed_ms=elapsed)
            else:
                record_error(exc, event + '.failed', elapsed_ms=elapsed)
            raise
        else:
            log_event(level, event + '.finished', elapsed_ms=round((time.monotonic() - started) * 1000))


def traced(event, fields=None, *, level='DEBUG'):
    def decorate(function):
        @wraps(function)
        def wrapper(*args, **kwargs):
            # Metadata must never change the application's behavior.
            try:
                details = fields(*args, **kwargs) if fields else {}
            except Exception:
                details = {}
            with operation(event, level=level, **details):
                return function(*args, **kwargs)
        return wrapper
    return decorate


_PRIVATE_KEY = re.compile(r'password|passwd|cookie|authorization|authcode|secret|phone|recipient|username|headers|payload|^body$|^url$|^path$|destination|^message$|^text$|^token$', re.I)


def safe_metadata(values, depth=0):
    """Defense in depth: never serialize arbitrary objects, payloads or secrets."""
    if depth > 6:
        return '<omitted>'
    if isinstance(values, dict):
        return {str(key)[:64]: '<redacted>' if _PRIVATE_KEY.search(str(key)) else safe_metadata(value, depth + 1)
                for key, value in list(values.items())[:64]}
    if isinstance(values, (list, tuple)):
        return [safe_metadata(value, depth + 1) for value in values[:32]]
    if isinstance(values, str):
        if re.search(r'https?://|[A-Za-z]:[\\/]|Bearer\s|[\w.+-]+@[\w.-]+\.[a-z]{2,}', values, re.I):
            return '<redacted>'
        return values[:240]
    if values is None or isinstance(values, (bool, int, float)):
        return values
    return '<' + type(values).__name__ + '>'


def safe_part(value):
    return value.upper() if isinstance(value, str) and re.fullmatch(r'C[0-9]{1,18}', value, re.I) else None


def network_target(url):
    """Only public, known host names; never query strings or user URL paths."""
    from urllib.parse import urlsplit
    try:
        host = urlsplit(url).hostname or ''
        allowed = ('jlc.com', 'szlcsc.com', 'lceda.cn', 'easyeda.com', 'mp.weixin.qq.com',
                   'github.com', 'githubusercontent.com')
        return host if any(host == domain or host.endswith('.' + domain) for domain in allowed) else 'other'
    except ValueError:
        return 'invalid'


def exception_details(error):
    """Keep the cause chain and numeric diagnostics, never messages or locals."""
    chain, seen = [], set()
    current = error
    while isinstance(current, BaseException) and id(current) not in seen and len(chain) < 8:
        seen.add(id(current))
        frames, trace = [], current.__traceback__
        while trace is not None:
            code = trace.tb_frame.f_code
            frames.append({'file': Path(code.co_filename).name, 'function': code.co_name, 'line': trace.tb_lineno})
            trace = trace.tb_next
        details = {'error': type(current).__name__, 'frames': frames[-24:]}
        for key in ('errno', 'winerror', 'code', 'service_code', 'status', 'verify_code', 'lineno', 'colno', 'expected'):
            value = getattr(current, key, None)
            if type(value) is int:
                details[key] = value
        reason = getattr(current, 'reason', None)
        if isinstance(reason, BaseException):
            details['reason_type'] = type(reason).__name__
        partial = getattr(current, 'partial', None)
        if isinstance(partial, bytes):
            details['received_bytes'] = len(partial)
        chain.append(details)
        current = current.__cause__ or (reason if isinstance(reason, BaseException) else None) or current.__context__
    return chain


def record_error(error, event, *, level='ERROR', **fields):
    chain = exception_details(error)
    identity = getattr(error, '_diagnostic_id', None)
    if not isinstance(identity, str) or not re.fullmatch(r'[a-f0-9]{32}', identity):
        identity = uuid.uuid4().hex
        try:
            error._diagnostic_id = identity
        except Exception:
            pass
    return log_event(level, event, error_id=identity, error=type(error).__name__,
                     frames=chain[0]['frames'] if chain else [], causes=chain, **fields)


class JsonFormatter(logging.Formatter):
    def format(self, record):
        entry = {'time': datetime.fromtimestamp(record.created).astimezone().isoformat(timespec='milliseconds'),
                 'level': record.levelname,
                 'version': _version or getattr(sys.modules.get('main'), 'VERSION', 'unknown'),
                 'event': record.getMessage(), 'session_id': _session_id, 'pid': record.process,
                 'thread': record.threadName, 'source': {'file': Path(record.pathname).name,
                                                        'function': record.funcName, 'line': record.lineno}}
        entry.update({key: value for key, value in getattr(record, 'safe_fields', {}).items() if key not in entry})
        return json.dumps(entry, ensure_ascii=False)


class SafeFileHandler(RotatingFileHandler):
    write_ok = True

    def emit(self, record):
        global _write_error
        self.write_ok = True
        super().emit(record)
        if self.write_ok:
            _write_error = None

    def handleError(self, record):
        # Logging failure must not print request data or mask the real failure.
        self.write_ok = False
        global _write_error
        _write_error = type(sys.exc_info()[1]).__name__


def default_log_directory():
    return data_directory() / 'logs'


def get_log_directory():
    with _LOCK:
        return Path(_handler.baseFilename).parent if _handler is not None else default_log_directory()


@contextmanager
def locked_log_files():
    """Flush and hold application writers while taking a snapshot or clearing."""
    with _LOCK:
        if _handler is not None:
            _handler.flush()
        if _crash_stream is not None:
            _crash_stream.flush()
        yield get_log_directory()


def reset_active_log(path):
    """Reset our open files without losing the logging/crash file handles."""
    with _LOCK:
        for stream in ((_handler.stream if _handler is not None else None), _crash_stream):
            if stream is not None and Path(stream.name) == path:
                stream.seek(0)
                stream.truncate(0)
                stream.flush()
                return True
        return False


def diagnostic_status():
    with _LOCK:
        return {'version': _version or getattr(sys.modules.get('main'), 'VERSION', 'unknown'),
                'session_id': _session_id, 'pid': os.getpid(), 'role': _role,
                'log_level': logging.getLevelName(LOGGER.level), 'write_error': _write_error,
                'max_log_bytes': MAX_LOG_BYTES, 'backup_count': LOG_BACKUPS,
                'crash_capture_active': _crash_stream is not None}


def set_log_level(level):
    with _LOCK:
        LOGGER.setLevel(level if level in LOG_LEVELS else 'DEBUG')


def close_logging():
    global _handler
    with _LOCK:
        if _handler is not None:
            LOGGER.removeHandler(_handler)
            _handler.close()
            _handler = None


def configure_logging(level='DEBUG', *, version=None, directory=None, role='application'):
    global _handler, _version, _write_error, _role
    directory = Path(directory) if directory is not None else default_log_directory()
    with _LOCK:
        close_logging()
        set_log_level(level)
        _version = version
        _role = role
        try:
            directory.mkdir(parents=True, exist_ok=True)
            filename = 'LCSC3D-update.log' if role == 'update' else 'LCSC3D.log'
            handler = SafeFileHandler(directory / filename, maxBytes=MAX_LOG_BYTES,
                                      backupCount=LOG_BACKUPS, encoding='utf-8')
            handler.setFormatter(JsonFormatter())
            LOGGER.addHandler(handler)
            _handler = handler
            _write_error = None
            return True
        except OSError as exc:
            _write_error = type(exc).__name__
            return False


def log_event(level, event, **safe_fields):
    """Callers supply fixed event names and metadata, never payloads or URLs."""
    if not LOGGER.isEnabledFor(logging.getLevelNamesMapping()[level]):
        return False
    with _LOCK:
        if not LOGGER.isEnabledFor(logging.getLevelNamesMapping()[level]):
            return False
        if _handler is None and not configure_logging(logging.getLevelName(LOGGER.level), role=_role):
            return False
        try:
            context = current_context()
            context.setdefault('operation_id', _session_id)
            context.setdefault('root_id', context['operation_id'])
            LOGGER.log(logging.getLevelNamesMapping()[level], event,
                       extra={'safe_fields': safe_metadata({**context, **safe_fields})}, stacklevel=2)
            return _handler.write_ok
        except (OSError, ValueError, TypeError):
            return False


def logging_health():
    return _write_error


def install_exception_hooks():
    def uncaught(kind, value, trace):
        record_error(value.with_traceback(trace), 'application.uncaught', level='CRITICAL')

    def thread_uncaught(args):
        record_error(args.exc_value.with_traceback(args.exc_traceback), 'thread.uncaught', level='CRITICAL')

    sys.excepthook = uncaught
    threading.excepthook = thread_uncaught


def log_runtime():
    log_event('INFO', 'application.environment', python=platform.python_version(),
              system=platform.system(), release=platform.release(), architecture=platform.machine(),
              frozen=bool(getattr(sys, 'frozen', False)))


def log_script_error(level, message, line, viewer):
    # Diagnostic code, source line and length identify failures without logging
    # arbitrary page content, SVGs or model data.
    kind = next((value for value in ('TypeError', 'ReferenceError', 'SyntaxError', 'RangeError', 'WebGL',
                                     'CONTEXT_LOST_WEBGL') if value.lower() in message.lower()), 'script_error')
    log_event('ERROR' if getattr(level, 'value', 2) >= 2 else 'WARNING', 'preview.script_error',
              viewer=viewer, reason=kind, script_line=line, message_length=len(message))


def install_qt_logging():
    from PySide6.QtCore import qInstallMessageHandler, qVersion
    def handler(kind, context, message):
        severity = {0: 'DEBUG', 1: 'WARNING', 2: 'ERROR', 3: 'CRITICAL', 4: 'INFO'}.get(kind.value, 'WARNING')
        category = next((value for value in ('webengine', 'opengl', 'qpa', 'network')
                         if value in (context.category or '').lower()), 'qt')
        log_event(severity, 'runtime.qt_message', category=category, source_line=context.line,
                  reason='graphics_context' if 'context' in message.lower() else 'qt_diagnostic',
                  message_length=len(message))
    qInstallMessageHandler(handler)
    log_event('INFO', 'application.qt_environment', qt=qVersion())


def start_crash_capture():
    """Record native faults when Python can handle them; preserve unclean exits."""
    global _crash_stream, _run_marker
    import faulthandler
    folder = get_log_directory()
    try:
        completed = [path for path in folder.glob('LCSC3D-crash-*.log')
                     if not (folder / ('run-' + path.stem.removeprefix('LCSC3D-crash-') + '.json')).exists()]
        for path in sorted(completed, key=lambda value: value.stat().st_mtime, reverse=True)[3:]:
            path.unlink(missing_ok=True)
        markers = sorted(folder.glob('run-*.json'), key=lambda path: path.stat().st_mtime)
        for path in markers:
            try:
                previous = json.loads(path.read_text(encoding='utf-8'))
                pid = previous.get('pid')
                if type(pid) is not int:
                    continue
                # Do not label a second concurrently-running copy as a crash.
                if sys.platform == 'win32':
                    import ctypes
                    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
                    kernel.OpenProcess.restype = ctypes.c_void_p
                    handle = kernel.OpenProcess(0x1000, False, pid)
                    if handle:
                        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
                        kernel.CloseHandle(handle)
                        continue
                    if ctypes.get_last_error() != 87:
                        continue
                else:
                    try:
                        os.kill(pid, 0)
                        continue
                    except ProcessLookupError:
                        pass
                identity = previous.get('session_id')
                log_event('WARNING', 'application.previous_unclean_exit', previous_pid=pid,
                          previous_session=identity if isinstance(identity, str) and re.fullmatch('[a-f0-9]{32}', identity) else None)
                path.unlink()
            except (OSError, ValueError):
                continue
        _run_marker = folder / ('run-' + str(os.getpid()) + '.json')
        _run_marker.write_text(json.dumps({'pid': os.getpid(), 'session_id': _session_id}), encoding='utf-8')
        # Fixed per-process dump; it contains code stacks only, never locals.
        _crash_stream = (folder / ('LCSC3D-crash-' + str(os.getpid()) + '.log')).open('wb')
        faulthandler.enable(file=_crash_stream, all_threads=True)
        atexit.register(stop_crash_capture)
    except (OSError, RuntimeError) as exc:
        record_error(exc, 'application.crash_capture_unavailable', level='WARNING')


def stop_crash_capture():
    global _crash_stream, _run_marker
    import faulthandler
    try:
        if _crash_stream is not None:
            faulthandler.disable()
            path = Path(_crash_stream.name)
            _crash_stream.close()
            _crash_stream = None
            if path.stat().st_size == 0:
                path.unlink()
        if _run_marker is not None:
            _run_marker.unlink(missing_ok=True)
            _run_marker = None
    except OSError:
        pass
