"""Window controls and actual Win32 client geometry for the integrated title bar."""
import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PySide6.QtCore import QEvent, QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from app_logging import close_logging
from app_settings import Preferences
from shell_ui import notify
import main


class WindowChromeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if QApplication.instance() is None:
            QApplication.setAttribute(Qt.AA_ShareOpenGLContexts)
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        env = patch.dict(os.environ, LOCALAPPDATA=folder.name)
        env.start()
        self.addCleanup(env.stop)
        self.addCleanup(close_logging)
        for target in ('main.MainWindow._setup_preview', 'main.MainWindow.request_component_info'):
            stub = patch(target)
            stub.start()
            self.addCleanup(stub.stop)
        self.window = main.MainWindow(settings_enabled=False)
        # Keep the restored size within the monitor work area, also when this
        # suite is run at 200% scaling on a 2560x1600 desktop.
        self.window.resize(1060, 740)
        self.window.show()
        self.settle()
        self.addCleanup(self.close_window)

    def close_window(self):
        self.window.close()
        self.window.deleteLater()
        self.app.sendPostedEvents(None, QEvent.DeferredDelete)

    def settle(self):
        for _ in range(5):
            self.app.processEvents()

    def test_controls_maximize_restore_and_minimize_without_recreating_window(self):
        w, bar = self.window, self.window.title_bar
        hwnd, geometry = int(w.winId()), w.geometry()
        QTest.mouseClick(bar.maximize_button, Qt.LeftButton)
        self.settle()
        self.assertTrue(w.isMaximized())
        self.assertEqual(bar.maximize_button.accessibleName(), '还原')
        QTest.mouseClick(bar.maximize_button, Qt.LeftButton)
        self.settle()
        self.assertFalse(w.isMaximized())
        self.assertEqual(w.geometry(), geometry)
        self.assertEqual(bar.maximize_button.accessibleName(), '最大化')
        QTest.mouseClick(bar.minimize_button, Qt.LeftButton)
        self.settle()
        self.assertTrue(w.isMinimized())
        w.showNormal()
        self.settle()
        self.assertEqual(int(w.winId()), hwnd)
        self.assertEqual(w.geometry(), geometry)

    def test_close_button_uses_close_event_and_respects_pending_work(self):
        w = self.window
        with patch.object(w, 'closeEvent', side_effect=lambda event: event.ignore()) as handler:
            QTest.mouseClick(w.title_bar.close_button, Qt.LeftButton)
            self.assertTrue(handler.called)
            self.assertTrue(w.isVisible())
        QTest.mouseClick(w.title_bar.close_button, Qt.LeftButton)
        self.assertFalse(w.isVisible())

    def test_controls_remain_visible_on_pages_large_fonts_and_both_themes(self):
        w = self.window
        for mode, language, size in (('light', 'zh_CN', 13), ('dark', 'en_US', 24)):
            w.apply_preferences(Preferences(theme_mode=mode, language=language, font_size=size))
            w.resize(1060, 740)
            w.open_settings()
            self.settle()
            bar = w.title_bar
            for button in (bar.minimize_button, bar.maximize_button, bar.close_button):
                self.assertTrue(button.isVisible())
                self.assertTrue(w.rect().contains(button.mapTo(w, button.rect().center())))
            self.assertLessEqual(w.account_button.mapTo(w, w.account_button.rect().topRight()).x(),
                                 bar.minimize_button.mapTo(w, QPoint()).x())
            self.assertLessEqual(w.width(), 1060)
            self.assertEqual(bar.maximize_button.toolTip(), 'Maximize' if language == 'en_US' else '最大化')
            w.settings_dialog.reject()

    def test_notifications_leave_window_controls_accessible(self):
        w = self.window
        toast = notify(w, 'Title', 'Message', persistent=True)
        toast.animation.setCurrentTime(toast.animation.duration())
        self.settle()
        bar_bottom = w.title_bar.mapTo(toast.parentWidget(), QPoint(0, w.title_bar.height())).y()
        self.assertGreater(toast.y(), bar_bottom)
        for button in (w.title_bar.maximize_button, w.title_bar.close_button):
            self.assertIs(w.childAt(button.mapTo(w, button.rect().center())), button)
        # Leave it open: destroying the window must also close the notification
        # even if Qt has already destroyed the title bar used for its inset.

    @unittest.skipUnless(sys.platform == 'win32', 'Native Windows frame')
    def test_native_client_has_no_titlebar_and_maximizes_inside_work_area(self):
        w = self.window
        user = w._native_frame.user
        user.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        user.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]

        def client_rect():
            rect, point = wintypes.RECT(), wintypes.POINT()
            self.assertTrue(user.GetClientRect(int(w.winId()), ctypes.byref(rect)))
            self.assertTrue(user.ClientToScreen(int(w.winId()), ctypes.byref(point)))
            return point.x, point.y, point.x + rect.right, point.y + rect.bottom

        outer = wintypes.RECT()
        self.assertTrue(user.GetWindowRect(int(w.winId()), ctypes.byref(outer)))
        self.assertEqual(client_rect(), (outer.left, outer.top, outer.right, outer.bottom))
        w.showMaximized()
        self.settle()
        frame = w._native_frame
        info = frame.MonitorInfo()
        info.cbSize = ctypes.sizeof(info)
        self.assertTrue(user.GetMonitorInfoW(user.MonitorFromWindow(int(w.winId()), 2), ctypes.byref(info)))
        self.assertEqual(client_rect(), (info.rcWork.left, info.rcWork.top, info.rcWork.right, info.rcWork.bottom))
        w.showNormal()
        self.settle()

    @unittest.skipUnless(sys.platform == 'win32', 'Native Windows frame')
    def test_native_hit_testing_all_edges_caption_and_interactive_controls(self):
        w = self.window
        user = w._native_frame.user
        user.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        user.SendMessageW.restype = ctypes.c_ssize_t
        rect = wintypes.RECT()
        self.assertTrue(user.GetWindowRect(int(w.winId()), ctypes.byref(rect)))

        def hit(x, y):
            return user.SendMessageW(int(w.winId()), 0x84, 0, (x & 0xffff) | ((y & 0xffff) << 16))

        x, y = (rect.left + rect.right) // 2, (rect.top + rect.bottom) // 2
        for point, expected in (((rect.left + 1, y), 10), ((rect.right - 2, y), 11),
                                ((x, rect.top + 1), 12), ((x, rect.bottom - 2), 15),
                                ((rect.left + 1, rect.top + 1), 13), ((rect.right - 2, rect.top + 1), 14),
                                ((rect.left + 1, rect.bottom - 2), 16), ((rect.right - 2, rect.bottom - 2), 17)):
            self.assertEqual(hit(*point), expected)
        ratio = w.devicePixelRatioF()

        def widget_hit(widget):
            point = widget.mapTo(w, widget.rect().center())
            return hit(rect.left + round(point.x() * ratio), rect.top + round(point.y() * ratio))

        self.assertEqual(widget_hit(w.page_caption), 2)
        for widget in (w.account_button, w.title_bar.maximize_button, w.title_bar.close_button, w.input):
            self.assertEqual(widget_hit(widget), 1)
        # A long page title or a live theme change must not turn buttons into a drag region.
        w.open_settings()
        self.settle()
        self.assertEqual(widget_hit(w.back_button), 1)
        self.assertEqual(widget_hit(w.page_caption), 2)


if __name__ == '__main__':
    unittest.main()
