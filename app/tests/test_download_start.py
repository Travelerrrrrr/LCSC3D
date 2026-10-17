"""Exercise real buttons with a deferred storefront and recoverable startup errors."""
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PySide6.QtCore import QEvent, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from lcsc3d.ui import main
from lcsc3d.core import app_logging as logs
from lcsc3d.services import backend
from test_backend import STEP, OBJ


class DownloadStartTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if QApplication.instance() is None:
            QApplication.setAttribute(Qt.AA_ShareOpenGLContexts)
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        env = patch.dict(os.environ, LOCALAPPDATA=str(self.root / 'profile'))
        env.start(); self.addCleanup(env.stop)
        logs.configure_logging('DEBUG', version='test'); self.addCleanup(logs.close_logging)
        for name in ('_setup_preview', 'request_component_info', 'show_preview'):
            mock = patch.object(main.MainWindow, name)
            mock.start(); self.addCleanup(mock.stop)
        self.window = main.MainWindow(settings_enabled=False)
        self.window.ensure_store(deferred=True)
        self.window.input.setPlainText('C2040\nC20197')
        self.window.load_queue()
        self.window.path_input.setText(str(self.root / 'exports'))
        self.window.bind_3d_box.setChecked(False)
        self.entered = threading.Event()
        self.block = True
        data = {part: json.loads((Path(__file__).parent / 'fixtures' / (part + '.json')).read_text('utf-8'))
                for part in ('C2040', 'C20197')}
        test = self
        class Api(backend.NetworkApi):
            def get_cad_data_of_component(self, part):
                if test.block:
                    test.entered.set()
                    self.cancelled.wait(4)
                self.check_cancelled()
                return data[part]
            def get_step_3d_model(self, uuid):
                self.check_cancelled()
                return STEP
            def get_raw_3d_model_obj(self, uuid):
                self.check_cancelled()
                return OBJ
        mock = patch.object(main, 'NetworkApi', Api)
        mock.start(); self.addCleanup(mock.stop)

    def tearDown(self):
        if self.window.worker and self.window.worker.isRunning():
            self.window.worker.cancelled.set()
            self.window.worker.wait(5000)
        self.app.processEvents()
        self.window.close()
        self.window.deleteLater()
        self.app.sendPostedEvents(None, QEvent.DeferredDelete)

    def wait_finished(self):
        end = time.monotonic() + 5
        while self.window.batch_running and time.monotonic() < end:
            QTest.qWait(10)
        self.assertFalse(self.window.batch_running)
        self.assertFalse(self.window.worker.isRunning())

    def test_deferred_anonymous_store_download_cancel_and_retry(self):
        window = self.window
        self.assertFalse(window.favorites_dialog.ui_ready)
        self.assertIsNone(window.favorites_dialog.client.account)
        window.start_button.click()
        self.assertTrue(self.entered.wait(2))
        self.assertTrue(window.batch_running)
        self.assertFalse(window.start_button.isEnabled())
        self.assertFalse(window.favorites_dialog.ui_ready)
        window.stop_button.click()
        self.assertFalse(window.stop_button.isEnabled())
        self.wait_finished()
        self.assertTrue(window.start_button.isEnabled())
        self.assertTrue(all(r.status == '已取消' for r in window.results.values()))
        self.assertIn('下载已停止', [r['title'] for r in window.notifications.history.entries])
        self.block = False
        window.start_button.click()
        self.wait_finished()
        self.assertTrue(all(r.status == '成功' for r in window.results.values()))

    def test_opening_store_while_downloading_keeps_import_disabled_until_finished(self):
        window = self.window
        window.start_button.click()
        self.assertTrue(self.entered.wait(2))
        store = window.ensure_store()
        self.assertTrue(store.ui_ready)
        self.assertFalse(store.import_enabled)
        self.assertFalse(store.cart_panel.import_enabled)
        self.assertFalse(store.orders_panel.detail.import_enabled)
        window.stop_button.click()
        self.wait_finished()
        self.assertTrue(store.cart_panel.import_enabled)
        self.assertTrue(store.orders_panel.detail.import_enabled)

    def test_start_failure_shows_error_logs_stack_and_allows_retry(self):
        window = self.window
        with patch.object(main.BatchWorker, 'start', side_effect=RuntimeError('PRIVATE startup failure')):
            window.start_button.click()
        self.assertFalse(window.batch_running)
        self.assertTrue(window.start_button.isEnabled())
        self.assertFalse(window.stop_button.isEnabled())
        self.assertIn('下载启动失败', window.run_status.text())
        self.assertIn('无法开始下载', [r['title'] for r in window.notifications.history.entries])
        events = [json.loads(line) for line in (logs.get_log_directory() / 'LCSC3D.log').read_text('utf-8').splitlines()]
        error = next(row for row in events if row['event'] == 'download.start_failed')
        self.assertEqual(error['error'], 'RuntimeError')
        self.assertTrue(error['frames'])
        self.assertNotIn('PRIVATE startup failure', json.dumps(events))
        self.block = False
        window.start_button.click()
        self.wait_finished()
        self.assertTrue(all(r.status == '成功' for r in window.results.values()))

    def test_worker_construction_failure_does_not_leave_running_state(self):
        with patch.object(main, 'BatchWorker', side_effect=RuntimeError('construction failed')):
            self.window.start_button.click()
        self.assertFalse(self.window.batch_running)
        self.assertTrue(self.window.start_button.isEnabled())
        self.assertFalse(self.window.stop_button.isEnabled())

    def test_completed_batch_ignores_late_stop(self):
        self.block = False
        self.window.start_button.click()
        self.wait_finished()
        before = self.window.run_status.text()
        self.window.stop_batch()
        self.assertEqual(self.window.run_status.text(), before)
        self.assertFalse(self.window.worker.cancelled.is_set())
