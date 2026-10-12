"""Thread-safe processing progress, including publication after preparation."""
import threading

from lcsc3d.core.app_logging import log_event


class BatchProgress:
    def __init__(self, parts, callback=None):
        self.values = dict.fromkeys(parts, 0.)
        self.callback = callback
        self.lock = threading.RLock()
        self.last = 0.
        self.logged = -1

    def advance(self, part, fraction):
        with self.lock:
            self.values[part] = max(self.values[part], min(.98, max(0., fraction)))
            self.publish()

    def publish(self):
        fraction = sum(self.values.values()) / len(self.values)
        if fraction <= self.last:
            return
        self.last = fraction
        percent = int(fraction * 100)
        if percent // 10 != self.logged:
            self.logged = percent // 10
            log_event('DEBUG', 'download.progress', percent=percent, count=len(self.values))
        if self.callback:
            self.callback(fraction)

    def finish(self, *, cancelled=False):
        with self.lock:
            if not cancelled:
                self.values = dict.fromkeys(self.values, 1.)
                self.publish()
            log_event('INFO', 'download.progress_finished', percent=round(self.last * 100, 2),
                      cancelled=cancelled, count=len(self.values))
