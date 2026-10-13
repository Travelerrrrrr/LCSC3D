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
        self.total = 0.
        self.compensation = 0.

    def advance(self, part, fraction):
        with self.lock:
            previous = self.values[part]
            value = max(previous, min(.98, max(0., fraction)))
            if value <= previous:
                return
            self.values[part] = value
            # Compensated incremental summation keeps large batches accurate
            # without scanning every other part for each individual update.
            delta = value - previous - self.compensation
            total = self.total + delta
            self.compensation = (total - self.total) - delta
            self.total = total
            self.publish()

    def publish(self):
        fraction = min(1., self.total / len(self.values)) if self.values else 0.
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
                self.total = float(len(self.values))
                self.compensation = 0.
                self.publish()
            log_event('INFO', 'download.progress_finished', percent=round(self.last * 100, 2),
                      cancelled=cancelled, count=len(self.values))
