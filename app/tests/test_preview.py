"""Exercise the real Windows preview compositor without remote services."""
import sys
import json
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
from updater import Release


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
        self.info_patch = patch.object(main.MainWindow, 'request_component_info')
        self.info_patch.start()
        self.addCleanup(self.info_patch.stop)
        # Most window regressions use a tiny local WebGL shell. Model I/O has
        # its own tests; never issue a live request from these UI tests.
        self.model_patch = patch.object(main.ModelPreviewWorker, 'run', new=lambda worker: None)
        self.model_patch.start()
        self.addCleanup(self.model_patch.stop)
        self.window = main.MainWindow(settings_enabled=False)
        root = Path(self.directory.name)
        (root / 'viewer.html').write_text('''<!doctype html><html><body>
<canvas id="canvas"></canvas><script>
const canvas = document.getElementById('canvas');
const gl = canvas.getContext('webgl2') || canvas.getContext('webgl');
window.loadLcscPart = function(part, token) {
console.log('LCSC3D_STATE:' + JSON.stringify({
  token,
  status: gl ? 'ready' : 'error', message: part + (gl ? ' WebGL ready' : ' WebGL unavailable')
}));
return true;
};
window.loadLcscPart(__PART_JSON__, __REVISION_JSON__);
</script></body></html>''', encoding='utf-8')
        self.root_patch = patch.object(main, 'ROOT', root)
        self.root_patch.start()
        self.window.input.setPlainText('C2040\nC20197')
        self.window.load_queue()

    def tearDown(self):
        if self.window.info_worker is not None:
            self.window.info_pending = None
            self.window.info_worker.cancelled.set()
            self.window.info_worker.wait(5000)
            self.app.processEvents()
        if self.window.worker is not None:
            self.window.worker.cancelled.set()
            self.window.worker.wait(5000)
            self.app.processEvents()
        if self.window.library_worker is not None:
            self.window.library_worker.cancelled.set()
            self.window.library_worker.wait(5000)
            self.app.processEvents()
        if self.window.model_worker is not None:
            self.window.model_pending = None
            self.window.model_worker.cancelled.set()
            self.window.model_worker.wait(5000)
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

    def wait_for_component_info(self):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and self.window.info_worker is not None:
            QTest.qWait(20)
        self.assertIsNone(self.window.info_worker, 'Component query did not finish')

    def test_legacy_wrl_only_settings_migrate_to_step(self):
        settings_path = Path(self.directory.name) / 'settings.json'
        settings_path.write_text(json.dumps({'step': False, 'wrl': True, 'obj': False}), encoding='utf-8')
        self.window.settings_enabled = True
        with patch.object(main, 'SETTINGS_PATH', settings_path):
            self.window._restore_settings()
            self.assertTrue(self.window.step_box.isChecked())
            self.assertFalse(self.window.obj_box.isChecked())
            self.window.save_settings()
        self.window.settings_enabled = False
        self.assertEqual(set(json.loads(settings_path.read_text(encoding='utf-8'))),
                         {'destination', 'step', 'obj', 'schlib', 'pcblib', 'store_proxy', 'update_proxy', 'log_level',
                          'merge_schlib', 'merge_pcblib', 'schlib_name', 'pcblib_name',
                          'keep_schlib', 'keep_pcblib', 'schlib_target', 'pcblib_target', 'project_path',
                          'library_mode', 'keep_individual', 'import_existing_to_project'})

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
        api = SimpleNamespace(get_svg_data_of_component=lambda part: fixture(part), check_cancelled=lambda: None)
        with patch('main.NetworkApi', return_value=api) as network:
            self.window.preview_mode_buttons['symbol'].click()
            self.wait_for_library()
            self.assertIs(self.window.preview_stack.currentWidget(), self.window.symbol_view)
            self.assertEqual(self.window.symbol_view.document.count, 57)
            self.window.preview_mode_buttons['footprint'].click()
            self.wait_for_library()
            self.assertIs(self.window.preview_stack.currentWidget(), self.window.footprint_view)
            self.assertEqual(self.window.footprint_view.document.count, 57)
            self.assertEqual(network.call_count, 1)
            self.window.table.selectRow(1)
            self.wait_for_library()
            self.assertEqual(self.window.current_preview, 'C20197')
            self.assertEqual(self.window.footprint_view.document.count, 8)
            self.window.preview_mode_buttons['symbol'].click()
            self.wait_for_library()
            self.assertEqual(self.window.symbol_view.document.count, 8)
            self.assertEqual(network.call_count, 2)
        self.window.preview_mode_buttons['3d'].click()
        self.wait_for_preview('C20197')
        self.assert_stable(hwnd, events, geometry)

    def test_vector_zoom_pan_and_fit(self):
        self.window.show()
        api = SimpleNamespace(get_svg_data_of_component=lambda part: fixture(part), check_cancelled=lambda: None)
        with patch('main.NetworkApi', return_value=api):
            self.window.preview_mode_buttons['symbol'].click()
            self.wait_for_library()
        view = self.window.symbol_view
        QTest.qWait(100)  # Allow the visible canvas to receive its final layout size.
        fitted = self.svg_state(view)
        target = view.focusProxy()
        center = target.rect().center()
        wheel = QWheelEvent(QPointF(center), QPointF(target.mapToGlobal(center)), QPoint(), QPoint(0, 360),
                            Qt.NoButton, Qt.NoModifier, Qt.NoScrollPhase, False)
        self.app.sendEvent(target, wheel)
        QTest.qWait(100)
        scaled = self.svg_state(view)
        self.assertGreater(scaled['zoom'], fitted['zoom'])
        QTest.mousePress(target, Qt.LeftButton, pos=center)
        QTest.mouseMove(target, center + QPoint(30, 30), delay=20)
        QTest.mouseRelease(target, Qt.LeftButton, pos=center + QPoint(30, 30))
        QTest.qWait(100)
        self.assertNotEqual(self.svg_state(view)['box']['x'], scaled['box']['x'])
        self.window.fit_button.click()
        QTest.qWait(100)
        self.assertEqual(self.svg_state(view), fitted)

    def evaluate(self, view, javascript):
        values = []
        view.page().runJavaScript('JSON.stringify(' + javascript + ')', values.append)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and not values:
            QTest.qWait(10)
        self.assertTrue(values, 'Browser did not return its rendered state')
        return json.loads(values[0])

    def svg_state(self, view):
        return self.evaluate(view, 'window.getSvgPreviewState()')

    def test_official_pad_css_and_pin_text_survive_browser_rendering(self):
        self.window.show()
        api = SimpleNamespace(get_svg_data_of_component=lambda part: fixture(part), check_cancelled=lambda: None)
        with patch('main.NetworkApi', return_value=api):
            self.window.preview_mode_buttons['footprint'].click()
            self.wait_for_library()
            fill = self.evaluate(self.window.footprint_view,
                "getComputedStyle(document.querySelector('g[c_partid=part_pad][layerid=\"1\"] polygon:not([c_padid])')).fill")
            self.assertEqual(fill, 'rgb(255, 0, 0)')
            numbers = self.evaluate(self.window.footprint_view,
                "Array.from(document.querySelectorAll('g[data-pad-numbers] text'), e => e.textContent)")
            self.assertEqual(set(numbers), {str(number) for number in range(1, 58)})
            self.window.preview_mode_buttons['symbol'].click()
            self.wait_for_library()
            texts = self.evaluate(self.window.symbol_view,
                "Array.from(document.querySelectorAll('#canvas svg text'), e => e.textContent)")
            self.assertIn('GPIO29_ADC3', texts)
            self.assertIn('RP2040', texts)

    def test_large_official_svg_exceeds_sethtml_limit_and_still_renders(self):
        self.window.show()
        data = fixture('C20197')
        data['result'][0]['svg'] = data['result'][0]['svg'].replace('</svg>', '<!--' + 'x' * 2100000 + '--></svg>')
        api = SimpleNamespace(get_svg_data_of_component=lambda part: data, check_cancelled=lambda: None)
        with patch('main.NetworkApi', return_value=api):
            self.window.preview_mode_buttons['symbol'].click()
            self.wait_for_library()
        self.assertTrue(self.svg_state(self.window.symbol_view)['loaded'])
        self.assertEqual(self.window.symbol_view.document.count, 8)

    def test_failed_request_can_be_reloaded(self):
        self.window.show()
        api = SimpleNamespace(get_svg_data_of_component=lambda part: fixture(part), check_cancelled=lambda: None)
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
        self.window.show()
        started, release = threading.Event(), threading.Event()

        class Api:
            def __init__(self, cancelled):
                self.cancelled = cancelled

            def check_cancelled(self):
                if self.cancelled.is_set():
                    raise main.Cancelled()

            def get_svg_data_of_component(self, part):
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

            def get_svg_data_of_component(self, part):
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
        data['result'] = [data['result'][0], {'docType': 2, 'svg':
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 20 20"><circle cx="10" cy="10" r="5"/></svg>'}]
        api = SimpleNamespace(get_svg_data_of_component=lambda part: data, check_cancelled=lambda: None)
        self.window.show()
        with patch('main.NetworkApi', return_value=api):
            self.window.preview_mode_buttons['symbol'].click()
            self.wait_for_library()
            self.assertTrue(self.window.symbol_unit_box.isVisible())
            self.window.symbol_unit_box.setCurrentIndex(1)
            self.wait_for_library()
            self.assertEqual(self.window.symbol_view.document.count, 0)
            self.window.preview_mode_buttons['footprint'].click()
            self.assertEqual(self.window.preview_state, 'error')
            self.window.preview_mode_buttons['symbol'].click()
            self.wait_for_library()
            self.assertEqual(self.window.preview_state, 'ready')
            self.assertEqual(self.window.symbol_unit_box.currentIndex(), 1)

    def test_automatic_preview_immediately_after_show_preserves_window(self):
        self.window.close()
        self.window.deleteLater()
        self.app.sendPostedEvents(None, QEvent.DeferredDelete)
        self.window = main.MainWindow(settings_enabled=False)
        self.window.input.setPlainText('C2040\nC20197')
        self.window.show()
        hwnd, geometry = int(self.window.winId()), self.window.geometry()
        events = WindowEvents(self.window)
        self.assertEqual(self.window.preview_state, 'empty')
        self.window.load_queue()
        self.assertIs(self.window.preview_stack.currentWidget(), self.window.web)
        self.wait_for_preview('C2040')
        self.assert_stable(hwnd, events, geometry)

    def test_maximized_single_click_switch_and_reload_preserve_window(self):
        self.window.showMaximized()
        self.assertTrue(QTest.qWaitForWindowExposed(self.window, 5000))
        QTest.qWait(100)
        hwnd, geometry = int(self.window.winId()), self.window.geometry()
        events = WindowEvents(self.window)
        first_cell = self.window.table.visualItemRect(self.window.table.item(0, main.PART_COLUMN))
        QTest.mouseClick(self.window.table.viewport(), Qt.LeftButton, pos=first_cell.center())
        self.wait_for_preview('C2040')
        self.assert_stable(hwnd, events, geometry, maximized=True)

        second_cell = self.window.table.visualItemRect(self.window.table.item(1, main.PART_COLUMN))
        QTest.mouseClick(self.window.table.viewport(), Qt.LeftButton, pos=second_cell.center())
        self.wait_for_preview('C20197')
        self.window.reload_button.click()
        self.wait_for_preview('C20197')
        self.assert_stable(hwnd, events, geometry, maximized=True)

    def test_replacing_queue_automatically_previews_new_first_part(self):
        self.window.show()
        self.wait_for_preview('C2040')
        self.window.input.setPlainText('C20197')
        self.assertTrue(self.window.load_queue())
        self.wait_for_preview('C20197')
        self.window.input.setPlainText('C2040\nC20197')
        self.assertTrue(self.window.load_queue())
        self.wait_for_preview('C2040')

    def test_result_updates_keep_preview_interactive_without_reloading(self):
        self.window.show()
        self.wait_for_preview('C2040')
        self.evaluate(self.window.web, '(window.retainedPreview = 42)')
        result = main.Result('C2040', '成功', message='下载完成')
        self.window.set_running(True)
        self.window.update_result(0, result)
        self.assertTrue(self.window.web.isEnabled())
        self.assertTrue(self.window.preview_mode_buttons['symbol'].isEnabled())
        self.assertEqual(self.evaluate(self.window.web, 'window.retainedPreview'), 42)
        self.assertIs(self.window.preview_stack.currentWidget(), self.window.web)

    def test_3d_selection_reuses_viewer_page_and_ignores_old_callbacks(self):
        self.window.show()
        self.wait_for_preview('C2040')
        first_revision = self.window.web_revision
        self.evaluate(self.window.web, '(window.retainedEngine = 42)')
        self.window.table.selectRow(1)
        self.wait_for_preview('C20197')
        self.assertEqual(self.window.viewer_page_loads, 1)
        self.assertEqual(self.evaluate(self.window.web, 'window.retainedEngine'), 42)
        self.window.on_web_preview_state(first_revision, 'error', 'late response for C2040')
        self.assertEqual(self.window.preview_state, 'ready')
        self.assertEqual(self.window.preview_status.text(), 'C20197 WebGL ready')
        self.window.reload_button.click()
        self.wait_for_preview('C20197')
        self.assertEqual(self.window.viewer_page_loads, 2)

    def test_local_renderer_draws_mesh_ignores_old_meshes_and_supports_navigation(self):
        self.window.show()
        self.wait_for_preview('C2040')
        with patch.object(main, 'ROOT', Path(__file__).resolve().parents[1]):
            self.window.reload_button.click()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not self.window.viewer_ready:
            QTest.qWait(20)
        # Red marks the top (+Z), blue the bottom (-Z). Verify their actual
        # framebuffer positions so an inverted initial view cannot pass.
        payload = {'vertices': [[-.35,-.25,1],[.35,-.25,1],[0,.35,1],
                                [-.35,-.25,-1],[.35,-.25,-1],[0,.35,-1]],
                   'faces': [[0,0,1,2],[1,3,4,5]], 'materials': [[.85,.1,.1],[.1,.1,.85]]}
        revision = self.window.web_revision
        self.window.model_preview_loaded('C2040', revision, payload)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and self.window.preview_state != 'ready':
            QTest.qWait(20)
        self.assertEqual(self.window.preview_state, 'ready', self.window.preview_status.text())
        initial = self.evaluate(self.window.web, 'window.getModelPreviewState()')
        self.assertEqual(initial['triangles'], 2)
        self.assertTrue(initial['loaded'])
        landmarks = self.evaluate(self.window.web, '''(() => {
            draw();
            const pixels = new Uint8Array(canvas.width * canvas.height * 4);
            gl.readPixels(0,0,canvas.width,canvas.height,gl.RGBA,gl.UNSIGNED_BYTE,pixels);
            const top = {count:0,y:0}, bottom = {count:0,y:0};
            for (let i=0; i<pixels.length; i+=4) {
                const r=pixels[i], g=pixels[i+1], b=pixels[i+2];
                const mark = r>1.8*g && r>1.8*b ? top : b>1.8*r && b>1.8*g ? bottom : null;
                if (mark) { mark.count++; mark.y+=Math.floor(i/4/canvas.width); }
            }
            return {top:{count:top.count,y:top.y/top.count},
                    bottom:{count:bottom.count,y:bottom.y/bottom.count}};
        })()''')
        self.assertGreater(landmarks['top']['count'], 20)
        self.assertGreater(landmarks['bottom']['count'], 20)
        # WebGL framebuffer Y increases upwards, so +Z must be above -Z.
        self.assertGreater(landmarks['top']['y'], landmarks['bottom']['y'])
        self.assertFalse(self.evaluate(self.window.web, 'window.showMesh(' + json.dumps(payload) + ',"C20197",0)'))
        self.assertEqual(self.evaluate(self.window.web, 'window.getModelPreviewState()'), initial)
        target = self.window.web.focusProxy()
        center = target.rect().center()
        wheel = QWheelEvent(QPointF(center), QPointF(target.mapToGlobal(center)), QPoint(), QPoint(0, 360),
                            Qt.NoButton, Qt.NoModifier, Qt.NoScrollPhase, False)
        self.app.sendEvent(target, wheel)
        QTest.qWait(100)
        self.assertGreater(self.evaluate(self.window.web, 'window.getModelPreviewState().zoom'), initial['zoom'])
        QTest.mousePress(target, Qt.LeftButton, pos=center)
        QTest.mouseMove(target, center + QPoint(30, 20), delay=20)
        QTest.mouseRelease(target, Qt.LeftButton, pos=center + QPoint(30, 20))
        QTest.qWait(100)
        self.assertNotEqual(self.evaluate(self.window.web, 'window.getModelPreviewState().yaw'), initial['yaw'])
        self.window.fit_button.click()
        QTest.qWait(100)
        self.assertEqual(self.evaluate(self.window.web, 'window.getModelPreviewState()'), initial)

    def test_svg_canvases_start_on_demand_and_share_browser_profile(self):
        self.assertIs(self.window.symbol_view.profile, self.window.web_profile)
        self.assertIs(self.window.footprint_view.profile, self.window.web_profile)
        self.assertFalse(self.window.symbol_view.shell_started)
        self.assertFalse(self.window.footprint_view.shell_started)
        self.window.show()
        api = SimpleNamespace(get_svg_data_of_component=lambda part: fixture(part), check_cancelled=lambda: None)
        with patch('main.NetworkApi', return_value=api):
            self.window.preview_mode_buttons['footprint'].click()
            self.wait_for_library()
        self.assertTrue(self.window.footprint_view.shell_started)
        self.assertFalse(self.window.symbol_view.shell_started)

    def wait_for_update_check(self):
        deadline = time.monotonic() + 5
        while self.window.update_dialog.worker is not None and time.monotonic() < deadline:
            QTest.qWait(20)
        self.assertIsNone(self.window.update_dialog.worker)

    def test_check_update_button_displays_latest_version_and_reuses_open_dialog(self):
        self.window.show()
        with patch('update_ui.UpdateClient', return_value=SimpleNamespace(check=lambda version: None)):
            self.window.settings_button.click()
            self.window.settings_dialog.update_button.click()
            dialog = self.window.update_dialog
            self.assertIs(dialog.parent(), self.window.settings_dialog)
            self.assertIs(dialog.controller, self.window)
            self.window.check_updates()
            self.assertIs(self.window.update_dialog, dialog)
            self.wait_for_update_check()
        self.assertIn('最新版本', dialog.status.text())
        dialog.close()
        self.assertTrue(self.window.settings_dialog.isVisible())
        self.window.settings_dialog.cancel_button.click()
        self.window.settings_button.click()
        self.app.sendPostedEvents(None, QEvent.DeferredDelete)
        with patch('update_ui.UpdateClient', return_value=SimpleNamespace(check=lambda version: None)):
            self.window.settings_dialog.update_button.click()
            self.wait_for_update_check()
        self.assertIs(self.window.update_dialog.parent(), self.window.settings_dialog)
        self.window.update_dialog.close()

    def test_check_update_failure_can_retry_and_source_run_offers_release_page(self):
        self.window.show()
        with patch('update_ui.UpdateClient', side_effect=RuntimeError('offline')):
            self.window.settings_button.click()
            self.window.settings_dialog.update_button.click()
            self.wait_for_update_check()
        dialog = self.window.update_dialog
        self.assertIn('offline', dialog.status.text())
        release = Release('2.1.0', 'https://github.com/Travelerrrrrr/LCSC3D/releases/tag/v2.1.0', '', '', 100, '新版说明')
        with patch('update_ui.UpdateClient', return_value=SimpleNamespace(check=lambda version: release)):
            dialog.install.click()
            self.wait_for_update_check()
        self.assertIn('2.1.0', dialog.status.text())
        self.assertEqual(dialog.notes.toPlainText(), '新版说明')
        self.assertFalse(dialog.install.isEnabled())
        self.assertTrue(dialog.page.isEnabled())
        dialog.close()

    def test_local_source_is_visible_and_reaches_the_update_worker(self):
        from updater import LocalUpdateSource
        source = LocalUpdateSource('http://127.0.0.1:8765', '2.1.0')
        self.window.update_source = source
        self.window.show()
        with patch('update_ui.UpdateClient', return_value=SimpleNamespace(check=lambda version: None)) as client:
            self.window.settings_button.click()
            self.window.settings_dialog.update_button.click()
            self.wait_for_update_check()
        dialog = self.window.update_dialog
        self.assertIs(client.call_args.kwargs['source'], source)
        self.assertIn('本地测试', dialog.windowTitle())
        self.assertIs(dialog.source, source)
        dialog.close()

    def test_closing_update_check_cancels_request_and_keeps_main_window_open(self):
        started = threading.Event()
        class Client:
            def __init__(self, cancelled):
                self.cancelled = cancelled
            def check(self, version):
                started.set()
                self.cancelled.wait(3)
                raise main.Cancelled()
        self.window.show()
        with patch('update_ui.UpdateClient', side_effect=Client):
            self.window.settings_button.click()
            self.window.settings_dialog.update_button.click()
            self.assertTrue(started.wait(1))
            self.window.update_dialog.close()
            self.wait_for_update_check()
            QTest.qWait(20)
        self.assertFalse(self.window.update_dialog.isVisible())
        self.assertTrue(self.window.isVisible())

    def test_removed_library_export_settings_migrate_to_step_download(self):
        settings_path = Path(self.directory.name) / 'settings.json'
        settings_path.write_text(json.dumps({'step': False, 'obj': False, 'symbol': True,
                                             'footprint': True}), encoding='utf-8')
        self.window.settings_enabled = True
        with patch.object(main, 'SETTINGS_PATH', settings_path):
            self.window._restore_settings()
            self.window.save_settings()
        self.window.settings_enabled = False
        self.assertTrue(self.window.step_box.isChecked())
        self.assertEqual(set(json.loads(settings_path.read_text(encoding='utf-8'))),
                         {'destination', 'step', 'obj', 'schlib', 'pcblib', 'store_proxy', 'update_proxy', 'log_level',
                          'merge_schlib', 'merge_pcblib', 'schlib_name', 'pcblib_name',
                          'keep_schlib', 'keep_pcblib', 'schlib_target', 'pcblib_target', 'project_path',
                          'library_mode', 'keep_individual', 'import_existing_to_project'})
        self.window.path_input.setText(str(Path(self.directory.name) / 'models'))
        captured = []
        def download(part, options, api, progress):
            captured.append(options.formats)
            return main.Result(part, '成功')
        with patch('main.NetworkApi', return_value=SimpleNamespace()), patch('backend.download_part', side_effect=download):
            self.window.start_batch()
            deadline = time.monotonic() + 5
            while self.window.batch_running and time.monotonic() < deadline:
                QTest.qWait(20)
            self.assertFalse(self.window.batch_running)
        self.assertEqual(captured, [('STEP',)] * 2)

    def test_ad_only_settings_and_real_batch_export(self):
        from altium_inspect import schematic, pcb
        cad = {part: json.loads((Path(__file__).parent / 'fixtures' / (part + '.json')).read_text('utf-8'))
               for part in ('C2040', 'C20197')}
        self.window.step_box.setChecked(False)
        self.window.schlib_box.setChecked(True)
        self.window.pcblib_box.setChecked(True)
        self.assertNotIn('覆盖已有文件', [box.text() for box in self.window.findChildren(main.QCheckBox)])
        settings_path = Path(self.directory.name) / 'settings.json'
        self.window.settings_enabled = True
        with patch.object(main, 'SETTINGS_PATH', settings_path):
            self.window.save_settings()
            self.window._restore_settings()
        self.window.settings_enabled = False
        self.assertFalse(self.window.step_box.isChecked())
        self.assertTrue(self.window.schlib_box.isChecked())
        self.assertTrue(self.window.pcblib_box.isChecked())
        self.window.path_input.setText(str(Path(self.directory.name) / 'AD 导出'))
        api = SimpleNamespace(get_cad_data_of_component=lambda part: cad[part], check_cancelled=lambda: None)
        with patch('main.NetworkApi', return_value=api) as network:
            for attempt in range(2):
                if attempt:
                    for result in self.window.results.values():
                        for file in result.files:
                            Path(file).write_bytes(b'old library')
                self.window.start_batch()
                self.assertFalse(self.window.schlib_box.isEnabled())
                self.assertFalse(self.window.pcblib_box.isEnabled())
                # Native CFB writes, AppData cross-volume moves and Qt delivery
                # share the desktop runner. This is a correctness check, not a
                # five-second performance contract.
                deadline = time.monotonic() + 15
                while self.window.batch_running and time.monotonic() < deadline:
                    QTest.qWait(20)
                self.assertFalse(self.window.batch_running)
                self.assertTrue(self.window.worker.wait(5000))
            self.assertEqual(network.call_count, 2)
            self.assertTrue(all(call.kwargs['use_cache'] is False for call in network.call_args_list))
        self.assertFalse(self.window.batch_running)
        self.assertTrue(self.window.schlib_box.isEnabled())
        for part, count in [('C2040', 57), ('C20197', 8)]:
            result = self.window.results[part]
            self.assertEqual(result.status, '成功', result.message)
            self.assertEqual(len(result.files), 2)
            self.assertEqual(Path(result.folder).name, cad[part]['title'] + '_' + part)
            self.assertEqual({Path(file).name for file in result.files},
                             {cad[part]['title'] + '.SchLib', cad[part]['title'] + '.PcbLib'})
            for file in result.files:
                if file.endswith('.SchLib'):
                    self.assertEqual(len(schematic(Path(file).read_bytes())[2]), count)
                else:
                    self.assertEqual(len(pcb(Path(file).read_bytes())[1]), count)

    def test_out_of_order_results_update_progress_by_completion_count(self):
        self.window.update_result(1, main.Result('C20197', '成功', message='done'))
        self.assertEqual(self.window.progress_bar.value(), 1)
        self.window.update_phase(1, '下载 STEP', '')
        self.assertEqual(self.window.table.item(1, main.RESULT_COLUMN).text(), '成功')
        self.window.update_result(0, main.Result('C2040', '失败', message='missing'))
        self.assertEqual(self.window.progress_bar.value(), 2)

    def test_loading_queries_titles_before_download_including_unchecked_parts(self):
        self.info_patch.stop()
        api = SimpleNamespace(check_cancelled=lambda: None)
        infos = {'C2040': {'title': 'RP2040', 'model': 'LQFN-56'},
                 'C20197': {'title': '4D03WGJ0102T5E', 'model': 'R0603-8P'}}
        self.window.table.item(1, main.DOWNLOAD_COLUMN).setCheckState(Qt.Unchecked)
        with patch('main.NetworkApi', return_value=api), \
                patch('main.get_component_metadata', side_effect=lambda part, _: infos[part]) as query:
            self.window.load_queue()
            self.wait_for_component_info()
        self.assertEqual({call.args[0] for call in query.call_args_list}, set(infos))
        self.assertIsNone(self.window.worker)
        self.assertEqual(self.window.results, {})
        self.assertEqual(self.window.checked_rows(), [0])
        self.assertEqual(self.window.progress_bar.value(), 0)
        for row, part in enumerate(self.window.ids):
            item = self.window.table.item(row, main.MODEL_COLUMN)
            self.assertEqual(item.text(), infos[part]['title'])
            self.assertIn(infos[part]['model'], item.toolTip())
            self.assertEqual(self.window.table.item(row, main.RESULT_COLUMN).text(), '等待下载')

    def test_replacing_queue_ignores_old_metadata_and_queries_latest_parts(self):
        self.info_patch.stop()
        started, release = threading.Event(), threading.Event()

        def query(part, api):
            if part == 'C2040':
                started.set()
                release.wait(3)
                return {'title': 'Old queue', 'model': ''}
            return {'title': part + ' current', 'model': ''}

        with patch('main.NetworkApi', return_value=SimpleNamespace()), \
                patch('main.get_component_metadata', side_effect=query):
            self.window.load_queue()
            old_revision = self.window.info_revision
            try:
                self.assertTrue(started.wait(1))
                self.window.input.setPlainText('C20197\nC163691')
                self.window.load_queue()
                self.window.update_component_info(old_revision, 'C20197', {'title': 'Old response'}, '')
                self.assertEqual(self.window.table.item(0, main.MODEL_COLUMN).text(), '查询中…')
            finally:
                release.set()
            self.wait_for_component_info()
        self.assertEqual(self.window.table.item(0, main.MODEL_COLUMN).text(), 'C20197 current')
        self.assertEqual(self.window.table.item(1, main.MODEL_COLUMN).text(), 'C163691 current')
        self.assertNotIn('C2040', self.window.component_info)

    def test_failed_metadata_query_can_retry_without_affecting_download_selection(self):
        self.info_patch.stop()

        def first_query(part, api):
            if part == 'C2040':
                raise DownloadError('offline')
            return {'title': 'Resistor', 'model': ''}

        with patch('main.NetworkApi', return_value=SimpleNamespace()), \
                patch('main.get_component_metadata', side_effect=first_query):
            self.window.load_queue()
            self.wait_for_component_info()
        item = self.window.table.item(0, main.MODEL_COLUMN)
        self.assertEqual(item.text(), '查询失败')
        self.assertIn('offline', item.toolTip())
        self.assertTrue(self.window.start_button.isEnabled())
        self.assertEqual(self.window.checked_rows(), [0, 1])
        with patch('main.NetworkApi', return_value=SimpleNamespace()), \
                patch('main.get_component_metadata', return_value={'title': 'Recovered', 'model': ''}):
            self.window.load_queue()
            self.wait_for_component_info()
        self.assertEqual(self.window.table.item(0, main.MODEL_COLUMN).text(), 'Recovered')
        self.assertEqual(self.window.results, {})

    def test_late_metadata_keeps_downloaded_title_and_failed_download_keeps_known_title(self):
        revision = self.window.info_revision
        self.window.update_result(0, main.Result('C2040', '成功', title='Downloaded', model='Official model'))
        self.window.update_component_info(revision, 'C2040', {'title': 'Older title', 'model': 'Older model'}, '')
        self.assertEqual(self.window.table.item(0, main.MODEL_COLUMN).text(), 'Downloaded')
        self.assertEqual(self.window.component_info['C2040'], {'title': 'Downloaded', 'model': 'Official model'})
        self.assertIn('Official model', self.window.table.item(0, main.MODEL_COLUMN).toolTip())
        self.window.update_component_info(revision, 'C20197', {'title': 'Known title', 'model': ''}, '')
        self.assertEqual(self.window.progress_bar.value(), 1)
        self.window.update_result(1, main.Result('C20197', '失败', message='offline'))
        self.assertEqual(self.window.table.item(1, main.MODEL_COLUMN).text(), 'Known title')
        self.assertEqual(self.window.table.item(1, main.RESULT_COLUMN).text(), '失败')

    def test_close_cancels_component_query_without_blocking(self):
        self.info_patch.stop()
        started = threading.Event()

        def query(part, api):
            started.set()
            api.cancelled.wait(3)
            raise main.Cancelled()

        self.window.show()
        with patch('main.NetworkApi', side_effect=lambda cancelled: SimpleNamespace(cancelled=cancelled)), \
                patch('main.get_component_metadata', side_effect=query):
            self.window.load_queue()
            self.assertTrue(started.wait(1))
            self.assertFalse(self.window.close())
            self.assertTrue(self.window.close_when_finished)
            self.wait_for_component_info()
        self.assertIsNone(self.window.info_pending)
        self.assertFalse(self.window.isVisible())

    def test_download_checkboxes_all_and_invert_keep_preview_selection(self):
        self.window.show()
        self.assertTrue(QTest.qWaitForWindowExposed(self.window, 5000))
        self.assertEqual(self.window.checked_rows(), [0, 1])
        cell = self.window.table.visualItemRect(self.window.table.item(1, main.DOWNLOAD_COLUMN))
        QTest.mouseClick(self.window.table.viewport(), Qt.LeftButton,
                        pos=QPoint(cell.left() + 12, cell.center().y()))
        self.assertEqual(self.window.checked_rows(), [0])
        cell = self.window.table.visualItemRect(self.window.table.item(1, main.PART_COLUMN))
        QTest.mouseClick(self.window.table.viewport(), Qt.LeftButton, pos=cell.center())
        self.assertEqual(self.window.current_preview, 'C20197')
        self.assertEqual(self.window.selection_summary.text(), '已勾选 1 / 2')
        self.window.invert_selection_button.click()
        self.assertEqual(self.window.checked_rows(), [1])
        self.window.select_all_button.click()
        self.assertEqual(self.window.checked_rows(), [0, 1])
        self.window.invert_selection_button.click()
        self.assertEqual(self.window.checked_rows(), [])
        self.assertEqual(self.window.current_preview, 'C20197')

    def test_reloading_queue_preserves_checks_by_part_and_checks_new_parts(self):
        self.window.table.item(1, main.DOWNLOAD_COLUMN).setCheckState(Qt.Unchecked)
        self.window.input.setPlainText('c20197, C2040, C163691, C20197')
        self.assertTrue(self.window.load_queue())
        self.assertEqual(self.window.ids, ['C20197', 'C2040', 'C163691'])
        self.assertEqual(self.window.checked_rows(), [1, 2])
        self.assertEqual(self.window.selection_summary.text(), '已勾选 2 / 3')

    def test_empty_download_selection_does_not_start_or_create_directory(self):
        destination = Path(self.directory.name) / 'not-created'
        self.window.path_input.setText(str(destination))
        self.window.invert_selection_button.click()
        with patch('main.BatchWorker') as worker:
            self.window.start_batch()
        worker.assert_not_called()
        self.assertIsNone(self.window.worker)
        self.assertFalse(destination.exists())
        self.assertEqual(self.window.run_status.text(), '请至少勾选一个要下载的器件')

    def test_running_locks_download_checks_and_keeps_rows_available_for_preview(self):
        self.window.set_running(True)
        self.assertFalse(self.window.select_all_button.isEnabled())
        self.assertFalse(self.window.invert_selection_button.isEnabled())
        self.assertFalse(self.window.table.item(0, main.DOWNLOAD_COLUMN).flags() & Qt.ItemIsEnabled)
        self.assertTrue(self.window.table.item(0, main.PART_COLUMN).flags() & Qt.ItemIsEnabled)
        self.window.invert_download_selection()
        self.assertFalse(self.window.load_queue())
        self.assertEqual(self.window.checked_rows(), [0, 1])
        self.window.table.selectRow(1)
        self.assertEqual(self.window.current_preview, 'C20197')
        self.window.set_running(False)
        self.assertTrue(self.window.select_all_button.isEnabled())
        self.assertTrue(self.window.invert_selection_button.isEnabled())
        self.assertTrue(self.window.table.item(0, main.DOWNLOAD_COLUMN).flags() & Qt.ItemIsEnabled)

    def test_checked_batch_maps_noncontiguous_rows_and_counts_only_current_results(self):
        self.window.input.setPlainText('C2040\nC20197\nC163691\nC1')
        self.window.load_queue()
        previous = main.Result('C2040', '成功', title='Previous', message='kept')
        self.window.update_result(0, previous)
        self.window.table.item(0, main.DOWNLOAD_COLUMN).setCheckState(Qt.Unchecked)
        self.window.table.item(2, main.DOWNLOAD_COLUMN).setCheckState(Qt.Unchecked)
        self.window.path_input.setText(str(Path(self.directory.name) / 'selected'))
        phases = []
        update_phase = self.window.update_phase

        def capture_phase(row, status, title):
            update_phase(row, status, title)
            phases.append((row, self.window.run_status.text()))

        def download(part, options, api, progress):
            progress('下载 STEP', part + ' model')
            return main.Result(part, '成功', title=part + ' model', message='done')

        def wait_for_batch():
            deadline = time.monotonic() + 5
            while self.window.batch_running and time.monotonic() < deadline:
                QTest.qWait(20)
            self.assertFalse(self.window.batch_running, 'Batch did not complete')
            self.assertTrue(self.window.worker.wait(5000))

        with patch('main.NetworkApi', return_value=SimpleNamespace()), \
                patch('backend.download_part', side_effect=download) as fetch, \
                patch.object(self.window, 'update_phase', side_effect=capture_phase):
            self.window.start_batch()
            self.assertEqual(self.window.worker.ids, ['C20197', 'C1'])
            self.assertEqual(self.window.worker.rows, [1, 3])
            self.assertEqual(self.window.progress_bar.maximum(), 2)
            wait_for_batch()
            self.assertEqual({call.args[0] for call in fetch.call_args_list}, {'C20197', 'C1'})
            self.assertEqual({row for row, _ in phases}, {1, 3})
            self.assertTrue(all(' / 2 ·' in status for _, status in phases))
            self.assertIs(self.window.results['C2040'], previous)
            self.assertNotIn('C163691', self.window.results)
            self.assertEqual(self.window.table.item(0, main.MODEL_COLUMN).text(), 'Previous')
            self.assertEqual(self.window.table.item(2, main.RESULT_COLUMN).text(), '等待下载')
            for row in (1, 3):
                self.assertEqual(self.window.table.item(row, main.MODEL_COLUMN).text(), self.window.ids[row] + ' model')
                self.assertEqual(self.window.table.item(row, main.RESULT_COLUMN).text(), '成功')
            self.assertEqual(self.window.progress_bar.value(), 2)
            self.assertEqual(self.window.summary.text(), '2 / 2 成功')
            self.assertEqual(self.window.checked_rows(), [1, 3])

            # A second batch excludes the earlier results from its progress.
            self.window.invert_selection_button.click()
            self.window.table.item(2, main.DOWNLOAD_COLUMN).setCheckState(Qt.Unchecked)
            fetch.reset_mock()
            phases.clear()
            self.window.start_batch()
            self.assertEqual(self.window.progress_bar.value(), 0)
            self.assertEqual(self.window.worker.ids, ['C2040'])
            wait_for_batch()
            self.assertEqual([call.args[0] for call in fetch.call_args_list], ['C2040'])
            self.assertEqual(len(self.window.results), 3)
            self.assertEqual(self.window.progress_bar.value(), 1)
            self.assertEqual(self.window.progress_bar.maximum(), 1)
            self.assertEqual(self.window.summary.text(), '1 / 1 成功')

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
                patch('backend.model_reference', return_value=model):
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
