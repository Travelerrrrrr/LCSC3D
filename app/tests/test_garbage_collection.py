"""GUI wrapper cycles must never be finalized by an allocating worker."""
import gc
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PySide6.QtCore import QObject, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from lcsc3d.core import app_logging
from lcsc3d.ui.garbage_collection import install_gui_garbage_collector


class GuiGarbageCollectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if QApplication.instance() is None:
            QApplication.setAttribute(Qt.AA_ShareOpenGLContexts)
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        env = patch.dict(os.environ, LOCALAPPDATA=folder.name)
        env.start()
        self.addCleanup(env.stop)
        app_logging.configure_logging('DEBUG', version='test')
        self.addCleanup(app_logging.close_logging)
        enabled = gc.isenabled()
        self.addCleanup(gc.enable if enabled else gc.disable)
        gc.enable()

    def collector(self):
        collector = install_gui_garbage_collector(self.app)
        self.addCleanup(collector.stop)
        return collector

    def test_worker_allocations_leave_ui_cycles_for_the_gui_timer(self):
        collector = self.collector()
        self.assertIs(install_gui_garbage_collector(self.app), collector)
        self.assertFalse(gc.isenabled())
        finalized = []

        class WidgetWrapper(QObject):
            def __del__(self):
                finalized.append(threading.get_ident())

        # Parentless Python-owned wrappers in cycles are the dangerous case.
        for _ in range(1000):
            wrapper = WidgetWrapper()
            wrapper.cycle = wrapper
        del wrapper

        def allocate():
            for _ in range(5000):
                cycle = []
                cycle.append(cycle)

        worker = threading.Thread(target=allocate)
        worker.start()
        worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(finalized, [])
        collector.timer.setInterval(10)
        for _ in range(100):
            QTest.qWait(10)
            if len(finalized) == 1000:
                break
        self.assertEqual(finalized, [threading.get_ident()] * 1000)

    def test_direct_collection_from_a_worker_is_rejected(self):
        collector = self.collector()
        errors = []

        def collect():
            try:
                collector.collect_if_needed()
            except RuntimeError as error:
                errors.append(str(error))

        worker = threading.Thread(target=collect)
        worker.start()
        worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, ['GUI garbage collection must run on its owner thread'])

    def test_stop_restores_the_original_gc_policy(self):
        collector = self.collector()
        collector.stop()
        self.assertTrue(gc.isenabled())
        self.assertFalse(collector.timer.isActive())
        collector.stop()
        gc.disable()
        disabled = self.collector()
        self.assertFalse(disabled.timer.isActive())
        disabled.stop()
        self.assertFalse(gc.isenabled())

    def test_collector_failure_is_diagnostic_and_later_collection_recovers(self):
        collector = self.collector()
        with patch('gc.get_count', return_value=(1000, 0, 0)), \
                patch('gc.collect', side_effect=RuntimeError('injected failure')):
            collector.collect_if_needed()
        with patch('gc.get_count', return_value=(1000, 0, 0)):
            collector.collect_if_needed()
        rows = [json.loads(line) for path in self.folder.glob('LCSC3D/logs/*.log')
                for line in path.read_text('utf-8').splitlines()]
        failure = next(row for row in rows if row['event'] == 'runtime.gui_gc_failed')
        self.assertTrue(failure['error_id'])
        self.assertTrue(any(row['event'] == 'runtime.gui_gc_collected' for row in rows))


if __name__ == '__main__':
    unittest.main()
