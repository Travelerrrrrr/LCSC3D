"""Saved preferences and live routing for storefront and update requests."""
from __future__ import annotations

from dataclasses import dataclass, asdict
import json
import os
from pathlib import Path
import tempfile
import threading
import urllib.request
from urllib.parse import urlsplit
from app_logging import traced, log_event, record_error


PROXY_OPTIONS = (('使用系统代理', 'system'), ('不使用系统代理', 'direct'))
LOG_LEVELS = ('DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL')


@dataclass(frozen=True)
class Preferences:
    store_proxy: str = 'system'
    update_proxy: str = 'system'
    log_level: str = 'DEBUG'

    @classmethod
    def from_mapping(cls, values):
        values = values if isinstance(values, dict) else {}
        return cls(store_proxy=values.get('store_proxy') if values.get('store_proxy') in ('system', 'direct') else 'system',
                   update_proxy=values.get('update_proxy') if values.get('update_proxy') in ('system', 'direct') else 'system',
                   log_level=values.get('log_level') if values.get('log_level') in LOG_LEVELS else 'DEBUG')

    def to_mapping(self):
        return asdict(self)


_LOCK = threading.RLock()
_preferences = Preferences()


def get_preferences():
    with _LOCK:
        return _preferences


def set_preferences(value):
    global _preferences
    with _LOCK:
        _preferences = Preferences.from_mapping(value.to_mapping())


def proxy_mode(service):
    preferences = get_preferences()
    if service == 'store':
        return preferences.store_proxy
    if service == 'update':
        return preferences.update_proxy
    raise ValueError('Unknown network service')


def proxy_settings(service):
    # An empty explicit ProxyHandler also bypasses environment proxy variables.
    return urllib.request.getproxies() if proxy_mode(service) == 'system' else {}


def proxy_for_url(url, service):
    if proxy_mode(service) == 'direct':
        return None
    parsed = urlsplit(url)
    if urllib.request.proxy_bypass(parsed.hostname or ''):
        return None
    return urllib.request.getproxies().get(parsed.scheme)


@traced('settings.write')
def write_settings(path, values):
    """Keep the previous configuration intact if saving is interrupted."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                         prefix='.LCSC3D-settings-', suffix='.tmp', delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(values, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


@traced('settings.read')
def read_settings(path, legacy_path=None):
    path = Path(path)
    if path.exists():
        try:
            value = json.loads(path.read_text(encoding='utf-8'))
            if not isinstance(value, dict):
                log_event('WARNING', 'settings.invalid_structure')
                return {}
            log_event('DEBUG', 'settings.loaded', origin='app_data')
            return value
        except (OSError, ValueError) as exc:
            record_error(exc, 'settings.read_failed', level='WARNING')
            return {}
    if legacy_path is None:
        return {}
    legacy_path = Path(legacy_path)
    try:
        original = legacy_path.read_bytes()
        value = json.loads(original)
        if not isinstance(value, dict):
            return {}
    except FileNotFoundError:
        log_event('DEBUG', 'settings.defaults_used')
        return {}
    except (OSError, ValueError) as exc:
        record_error(exc, 'settings.legacy_read_failed', level='WARNING')
        return {}
    try:
        write_settings(path, value)
        # Remove only the exact old configuration that has been preserved.
        if legacy_path.read_bytes() == original:
            legacy_path.unlink()
        log_event('INFO', 'settings.migrated')
    except OSError as exc:
        # Migration can be retried later; the old settings remain usable.
        record_error(exc, 'settings.migration_failed', level='WARNING')
    return value


def initial_log_level(path):
    """Read only the threshold before startup diagnostics are configured."""
    try:
        return Preferences.from_mapping(json.loads(Path(path).read_text(encoding='utf-8'))).log_level
    except (OSError, ValueError):
        return 'DEBUG'
