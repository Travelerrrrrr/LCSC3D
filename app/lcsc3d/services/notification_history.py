"""Bounded records of displayed notices, separate from diagnostic logs."""
from datetime import datetime
import json
import uuid

from PySide6.QtCore import QObject, Signal, QSaveFile, QIODevice

from lcsc3d.core.app_logging import log_event, record_error
from lcsc3d.core.app_paths import data_directory

MAX_MESSAGES = 500
MAX_HISTORY_BYTES = 1024 * 1024
MAX_TEXT = 32768
SEVERITIES = ('info', 'success', 'warning', 'error')


class NotificationHistory(QObject):
    changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.path = data_directory() / 'messages' / 'history.json'
        self.entries = []
        self.save_failed = False
        self.load()

    def load(self):
        try:
            if not self.path.exists():
                return
            if self.path.stat().st_size > MAX_HISTORY_BYTES:
                raise ValueError('Message history exceeds its size limit')
            with self.path.open('rb') as stream:
                raw = stream.read(MAX_HISTORY_BYTES + 1)
            if len(raw) > MAX_HISTORY_BYTES:
                raise ValueError('Message history exceeds its size limit')
            data = json.loads(raw)
            if data.get('version') != 1 or not isinstance(data.get('entries'), list):
                raise ValueError('Unsupported message history')
            entries = data['entries']
            for item in entries:
                if (not isinstance(item, dict) or set(item) != {'id', 'time', 'severity', 'title', 'message', 'details'}
                        or any(not isinstance(v, str) for v in item.values())
                        or item['severity'] not in SEVERITIES
                        or any(len(item[key]) > MAX_TEXT for key in ('title', 'message', 'details'))
                        or len(item['id']) != 32 or any(c not in '0123456789abcdef' for c in item['id'])):
                    raise ValueError('Invalid message record')
                datetime.fromisoformat(item['time'])
            self.entries = entries[:MAX_MESSAGES]
            log_event('DEBUG', 'notification.history_loaded', count=len(self.entries))
        except (OSError, ValueError, TypeError, AttributeError) as exc:
            self.save_failed = True
            record_error(exc, 'notification.history_load_failed', level='WARNING')

    def add(self, title, message, severity, details=''):
        entry = dict(id=uuid.uuid4().hex, time=datetime.now().astimezone().isoformat(timespec='seconds'),
                     severity=severity if severity in SEVERITIES else 'info',
                     title=str(title)[:MAX_TEXT], message=str(message)[:MAX_TEXT], details=str(details)[:MAX_TEXT])
        self.entries.insert(0, entry)
        self.entries = self.entries[:MAX_MESSAGES]
        self.save()
        self.changed.emit()
        return entry

    def save(self):
        file = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            raw = self.encode()
            while len(raw) > MAX_HISTORY_BYTES:
                self.entries.pop()
                raw = self.encode()
            file = QSaveFile(str(self.path))
            # A failed write must preserve the preceding complete history.
            file.setDirectWriteFallback(False)
            if not file.open(QIODevice.WriteOnly) or file.write(raw) != len(raw) or not file.commit():
                raise OSError('Unable to save message history')
            self.save_failed = False
        except (OSError, ValueError) as exc:
            if file is not None:
                file.cancelWriting()
            self.save_failed = True
            record_error(exc, 'notification.history_save_failed', level='WARNING')

    def encode(self):
        return json.dumps(dict(version=1, entries=self.entries), ensure_ascii=False).encode('utf-8')

    def clear(self):
        count = len(self.entries)
        self.entries = []
        self.save()
        log_event('INFO', 'notification.history_cleared', count=count, saved=not self.save_failed)
        self.changed.emit()
