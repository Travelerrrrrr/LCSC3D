"""Exercise the real Windows preview compositor without remote services."""
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import main
from backend import DownloadError
from PySide6.QtCore import QEvent, QObject, Qt, QPoint, QPointF
from PySide6.QtGui import QWheelEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from test_library_preview import fixture


class WindowEvents(QObject):
    def __init__(self, window):
        super().__init__(window)
        self.events = []
        window.installEventFilter(self)

    def eventFilter(self, watched, event):
        if event.type() in (QEvent.Hide, QEvent.WinIdChange):
            self.events.append(event.type().name)
        return False


@unittest.skipUnless(sys.platform == 'win32', 'Native Windows window regression')
class PreviewWindowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        QApplication.setAttribute(Qt.AA_ShareOpenGLContexts)
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setStyle('Fusion')
        cls.app.setStyleSheet(main.STYLES)

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.window = main.MainWindow(settings_enabled=False)
        root = Path(self.directory.name)
        (root / 'viewer.html').write_text('''<!doctype html><html><body>
<canvas id="canvas"></canvas><script>
const part = __PART_JSON__;
const canvas = document.getElementById('canvas');
const gl = canvas.getContext('webgl2') || canvas.getContext('webgl');
console.log('LCSC3D_STATE:' + JSON.stringify({
  status: gl ? 'ready' : 'error', message: part + (gl ? ' WebGL ready' : ' WebGL unavailable')
}));
</script></body></html>''', encoding='utf-8')
        self.root_patch = patch.object(main, 'ROOT', root)
        self.root_patch.start()
        self.window.input.setPlainText('C2040\nC20197')
        self.window.load_queue()

    def tearDown(self):
        if self.window.library_worker is not None:
            self.window.library_worker.cancelled.set()
            self.window.library_worker.wait(5000)
            self.app.processEvents()
        self.root_patch.stop()
        self.window.close()
        self.window.deleteLater()
        self.app.sendPostedEvents(None, QEvent.DeferredDelete)
        self.directory.cleanup()

    def wait_for_preview(self, part):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if self.window.preview_state in ('ready', 'error'):
                break
            QTest.qWait(20)
        self.assertEqual(self.window.preview_state, 'ready', self.window.preview_status.text())
        self.assertEqual(self.window.current_preview, part)
        self.assertEqual(self.window.preview_status.text(), part + ' WebGL ready')

    def assert_stable(self, hwnd, events, geometry, maximized=False):
        self.assertEqual(int(self.window.winId()), hwnd, 'Preview recreated the native window')
        self.assertEqual(events.events, [], 'Preview hid or recreated the main window')
        self.assertEqual(self.window.geometry(), geometry)
        self.assertEqual(self.window.isMaximized(), maximized)

    def wait_for_library(self):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and (self.window.preview_state == 'loading' or self.window.library_worker is not None):
            QTest.qWait(20)
        self.assertEqual(self.window.preview_state, 'ready', self.window.preview_status.text())

    def test_library_switch_uses_one_request_and_returns_to_3d_without_flicker(self):
        self.window.show()
        hwnd, geometry = int(self.window.winId()), self.window.geometry()
        events = WindowEvents(self.window)
        api = SimpleNamespace(get_cad_data_of_component=lambda part: fixture(part), check_cancelled=lambda: None)
        with patch('main.NetworkApi', return_value=api) as network:
            self.window.preview_mode_buttons['symbol'].click()
            self.wait_for_library()
            self.assertIs(self.window.preview_stack.currentWidget(), self.window.symbol_view)
            self.assertEqual(self.window.symbol_view.document.count, 57)
            self.window.preview_mode_buttons['footprint'].click()
            self.assertIs(self.window.preview_stack.currentWidget(), self.window.footprint_view)
            self.assertEqual(self.window.footprint_view.document.count, 57)
            self.assertEqual(network.call_count, 1)
            self.window.table.selectRow(1)
            self.wait_for_library()
            self.assertEqual(self.window.current_preview, 'C20197')
            self.assertEqual(self.window.footprint_view.document.count, 8)
            self.window.preview_mode_buttons['symbol'].click()
            self.assertEqual(self.window.symbol_view.document.count, 8)
            self.assertEqual(network.call_count, 2)
        self.window.preview_mode_buttons['3d'].click()
        self.wait_for_preview('C20197')
        self.assert_stable(hwnd, events, geometry)

    def test_vector_zoom_pan_and_fit(self):
        self.window.show()
        api = SimpleNamespace(get_cad_data_of_component=lambda part: fixture(part), check_cancelled=lambda: None)
        with patch('main.NetworkApi', return_value=api):
            self.window.preview_mode_buttons['symbol'].click()
            self.wait_for_library()
        view = self.window.symbol_view
        fitted = view.transform().m11()
        center = view.viewport().rect().center()
        wheel = QWheelEvent(QPointF(center), QPointF(view.viewport().mapToGlobal(center)), QPoint(), QPoint(0, 360),
                            Qt.NoButton, Qt.NoModifier, Qt.NoScrollPhase, False)
        self.app.sendEvent(view.viewport(), wheel)
        self.assertGreater(view.transform().m11(), fitted)
        origin = view.mapToScene(center)
        QTest.mousePress(view.viewport(), Qt.LeftButton, pos=center)
        QTest.mouseMove(view.viewport(), center + QPoint(30, 30))
        QTest.mouseRelease(view.viewport(), Qt.LeftButton, pos=center + QPoint(30, 30))
        self.assertNotEqual(view.mapToScene(center), origin)
        self.window.fit_button.click()
        self.assertAlmostEqual(view.transform().m11(), fitted, places=5)

    def test_failed_request_can_be_reloaded(self):
        api = SimpleNamespace(get_cad_data_of_component=lambda part: fixture(part), check_cancelled=lambda: None)
        with patch('main.NetworkApi', side_effect=[DownloadError('offline'), api]) as network:
            self.window.preview_mode_buttons['symbol'].click()
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and self.window.library_worker is not None:
                QTest.qWait(20)
            self.assertEqual(self.window.preview_state, 'error')
            self.assertNotIn('C2040', self.window.library_cache)
            self.window.reload_button.click()
            self.wait_for_library()
            self.assertEqual(network.call_count, 2)

    def test_superseded_preview_never_replaces_current_part(self):
        started, release = threading.Event(), threading.Event()

        class Api:
            def __init__(self, cancelled):
                self.cancelled = cancelled

            def check_cancelled(self):
                if self.cancelled.is_set():
                    raise main.Cancelled()

            def get_cad_data_of_component(self, part):
                if part == 'C2040':
                    started.set()
                    if not release.wait(3):
                        raise RuntimeError('Test release timed out')
                return fixture(part)

        with patch('main.NetworkApi', side_effect=Api):
            self.window.preview_mode_buttons['symbol'].click()
            try:
                self.assertTrue(started.wait(1))
                self.window.table.selectRow(1)
                self.window.preview_mode_buttons['footprint'].click()
            finally:
                release.set()
            self.wait_for_library()
        self.assertEqual(self.window.current_preview, 'C20197')
        self.assertEqual(self.window.footprint_view.document.count, 8)
        self.assertNotIn('C2040', self.window.library_cache)

    def test_close_cancels_library_request_and_waits_without_blocking(self):
        started = threading.Event()

        class Api:
            def __init__(self, cancelled):
                self.cancelled = cancelled

            def get_cad_data_of_component(self, part):
                started.set()
                self.cancelled.wait(3)
                raise main.Cancelled()

        self.window.show()
        with patch('main.NetworkApi', side_effect=Api):
            self.window.preview_mode_buttons['symbol'].click()
            self.assertTrue(started.wait(1))
            self.assertFalse(self.window.close())
            self.assertTrue(self.window.close_when_finished)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and self.window.library_worker is not None:
                QTest.qWait(20)
        self.assertIsNone(self.window.library_worker)
        self.assertFalse(self.window.isVisible())

    def test_multi_unit_selector_and_missing_footprint(self):
        data = fixture('C20197')
        data['subparts'] = [{'dataStr': data['dataStr']}, {'dataStr': {'shape': ['C~0~0~5~#000000~1~0~none~id~0']}}]
        data.pop('packageDetail')
        api = SimpleNamespace(get_cad_data_of_component=lambda part: data, check_cancelled=lambda: None)
        self.window.show()
        with patch('main.NetworkApi', return_value=api):
            self.window.preview_mode_buttons['symbol'].click()
            self.wait_for_library()
            self.assertTrue(self.window.symbol_unit_box.isVisible())
            self.window.symbol_unit_box.setCurrentIndex(1)
            self.assertEqual(self.window.symbol_view.document.count, 0)
            self.window.preview_mode_buttons['footprint'].click()
            self.assertEqual(self.window.preview_state, 'error')
            self.window.preview_mode_buttons['symbol'].click()
            self.assertEqual(self.window.preview_state, 'ready')
            self.assertEqual(self.window.symbol_unit_box.currentIndex(), 1)

    def test_first_click_immediately_after_show_preserves_window(self):
        self.window.show()
        hwnd, geometry = int(self.window.winId()), self.window.geometry()
        events = WindowEvents(self.window)
        self.assertIsNot(self.window.preview_stack.currentWidget(), self.window.web)
        self.assertEqual(self.window.preview_state, 'empty')
        self.window.preview_button.click()
        self.wait_for_preview('C2040')
        self.assert_stable(hwnd, events, geometry)

    def test_maximized_double_click_switch_and_reload_preserve_window(self):
        self.window.showMaximized()
        self.assertTrue(QTest.qWaitForWindowExposed(self.window, 5000))
        QTest.qWait(100)
        hwnd, geometry = int(self.window.winId()), self.window.geometry()
        events = WindowEvents(self.window)
        first_cell = self.window.table.visualItemRect(self.window.table.item(0, 0))
        QTest.mouseClick(self.window.table.viewport(), Qt.LeftButton, pos=first_cell.center())
        QTest.mouseDClick(self.window.table.viewport(), Qt.LeftButton, pos=first_cell.center())
        self.wait_for_preview('C2040')
        self.assert_stable(hwnd, events, geometry, maximized=True)

        self.window.table.selectRow(1)
        self.window.preview_button.click()
        self.wait_for_preview('C20197')
        self.window.reload_button.click()
        self.wait_for_preview('C20197')
        self.assert_stable(hwnd, events, geometry, maximized=True)

    def test_batch_completion_preserves_results_without_csv_output(self):
        step = b'ISO-10303-21;\nEND-ISO-10303-21;'
        model = SimpleNamespace(name='QFN/56', uuid='test-uuid')

        class Api:
            check_cancelled = staticmethod(lambda: None)

            def get_cad_data_of_component(self, part):
                if part == 'C20197':
                    raise DownloadError('Not found')
                return {'title': 'RP2040'}

            def get_step_3d_model(self, uuid):
                return step

        destination = Path(self.directory.name) / 'download'
        self.window.path_input.setText(str(destination))
        finished = []
        self.window.batch_done.connect(lambda: finished.append(True))
        with patch('main.NetworkApi', return_value=Api()), \
                patch('backend.Easyeda3dModelImporter', return_value=SimpleNamespace(output=model)):
            self.window.start_batch()
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and (not finished or self.window.worker.isRunning()):
                QTest.qWait(20)
            self.assertTrue(finished, 'Batch did not complete')
            self.assertTrue(self.window.worker.wait(5000))
        self.assertEqual(self.window.results['C2040'].status, '成功')
        self.assertEqual(self.window.results['C20197'].status, '失败')
        self.assertEqual(self.window.summary.text(), '1 / 2 成功')
        self.assertEqual({path.name for path in destination.iterdir()}, {'RP2040_C2040'})
        self.assertEqual((destination / 'RP2040_C2040/C2040_QFN_56.step').read_bytes(), step)
        self.assertFalse(list(destination.rglob('*.csv')))

if __name__ == '__main__':
    unittest.main()
