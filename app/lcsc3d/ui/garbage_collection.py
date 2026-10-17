"""Run Python cycle collection on the GUI thread that owns Qt wrappers."""
import gc
import time

from PySide6.QtCore import QObject, QThread, QTimer, Slot

from lcsc3d.core.app_logging import log_event, record_error


class GuiGarbageCollector(QObject):
    def __init__(self, application):
        super().__init__(application)
        self.was_enabled = gc.isenabled()
        self.stopped = False
        self.timer = QTimer(self)
        self.timer.setInterval(1000)
        self.timer.timeout.connect(self.collect_if_needed)
        application.aboutToQuit.connect(self.stop)
        # Refcount-based cleanup remains active. Only automatic cyclic GC is
        # replaced: otherwise an allocating download worker can finalize dead
        # UI wrappers while the GUI thread is processing their Qt objects.
        if self.was_enabled:
            gc.disable()
            self.timer.start()
        log_event('DEBUG', 'runtime.gui_gc_configured', active=self.was_enabled)

    @Slot()
    def collect_if_needed(self):
        if self.stopped or not self.was_enabled:
            return
        if QThread.currentThread() != self.thread():
            raise RuntimeError('GUI garbage collection must run on its owner thread')
        counts, thresholds = gc.get_count(), gc.get_threshold()
        if not thresholds[0] or counts[0] < thresholds[0]:
            return
        generation = 2 if counts[2] >= thresholds[2] else 1 if counts[1] >= thresholds[1] else 0
        started = time.monotonic()
        try:
            collected = gc.collect(generation)
        except Exception as error:
            record_error(error, 'runtime.gui_gc_failed', stage='collect')
        else:
            log_event('DEBUG', 'runtime.gui_gc_collected', generation=generation,
                      collected=collected, elapsed_ms=round((time.monotonic() - started) * 1000))

    @Slot()
    def stop(self):
        if self.stopped:
            return
        self.stopped = True
        self.timer.stop()
        if self.was_enabled:
            gc.collect()
            gc.enable()


def install_gui_garbage_collector(application):
    """Keep one collector alive for the entire QApplication event loop."""
    collector = getattr(application, '_gui_garbage_collector', None)
    if collector is None or collector.stopped:
        collector = GuiGarbageCollector(application)
        application._gui_garbage_collector = collector
    return collector
