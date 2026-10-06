"""Exercise the real Windows preview compositor without remote services."""
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import main
from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication


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


if __name__ == '__main__':
    unittest.main()
