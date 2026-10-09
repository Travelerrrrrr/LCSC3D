"""Embedded public product pictures and visible storefront import feedback."""
import threading
import time
from unittest.mock import patch

from test_settings import PreferencesTestCase
import main
from favorites import FavoritesDialog
from store import StoreError
from product_preview import ProductPreview
from PySide6.QtCore import Qt, QBuffer, QIODevice, QEvent
from PySide6.QtGui import QImage, QColor
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication


def picture(color):
    image = QImage(80, 60, QImage.Format_RGB32)
    image.fill(QColor(color))
    buffer = QBuffer()
    buffer.open(QIODevice.WriteOnly)
    image.save(buffer, 'PNG')
    return bytes(buffer.data())


class FakePhotos:
    def product(self, part, stop):
        return {'part': part, 'images': ['red', 'blue']}

    def image(self, url, stop):
        return picture(url)


class ProductPreviewTests(PreferencesTestCase):
    @classmethod
    def setUpClass(cls):
        if QApplication.instance() is None:
            QApplication.setAttribute(Qt.AA_ShareOpenGLContexts)
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        super().setUp()
        self.widget = ProductPreview(client_factory=FakePhotos)
        self.widget.resize(400, 380)
        self.widget.show()

    def tearDown(self):
        self.widget.jobs.cancel_all()
        self.wait(lambda: not self.widget.jobs.workers)
        self.widget.close()
        self.widget.deleteLater()
        self.app.sendPostedEvents(None, QEvent.DeferredDelete)

    def wait(self, condition):
        deadline = time.monotonic() + 4
        while not condition() and time.monotonic() < deadline:
            QTest.qWait(10)
        self.assertTrue(condition())

    def test_photo_buttons_show_real_image_pixels_and_support_zoom(self):
        self.widget.select('C2040')
        self.wait(lambda: self.widget.load_state[0] == 'ready')
        self.assertEqual(self.widget.position.text(), '1 / 2')
        self.assertEqual(self.widget.view.photo.pixmap().toImage().pixelColor(0, 0), QColor('red'))
        self.widget.next.click()
        self.wait(lambda: self.widget.load_state[0] == 'ready')
        self.assertEqual(self.widget.position.text(), '2 / 2')
        self.assertEqual(self.widget.view.photo.pixmap().toImage().pixelColor(0, 0), QColor('blue'))
        self.widget.view.set_zoom(2)
        self.assertEqual(self.widget.view.zoom, 2)
        self.widget.view.fit_photo()
        self.assertTrue(self.widget.view.fitted)

    def test_late_response_cannot_replace_the_current_product(self):
        started, release = threading.Event(), threading.Event()
        class Slow(FakePhotos):
            def product(self, part, stop):
                if part == 'C1':
                    started.set()
                    release.wait(2)
                return {'part': part, 'images': ['red' if part == 'C1' else 'blue']}
        self.widget.factory = Slow
        self.widget.select('C1')
        self.assertTrue(started.wait(1))
        self.widget.select('C2')
        self.wait(lambda: self.widget.load_state[0] == 'ready')
        release.set()
        self.wait(lambda: not self.widget.jobs.workers)
        self.assertEqual(self.widget.part, 'C2')
        self.assertEqual(self.widget.view.photo.pixmap().toImage().pixelColor(0, 0), QColor('blue'))

    def test_empty_failed_and_corrupt_photos_can_be_reloaded(self):
        for result in ({'part': 'C1', 'images': []}, {'part': 'C2', 'images': ['red']}):
            with patch.object(FakePhotos, 'product', return_value=result):
                self.widget.select('C1', refresh=True)
                self.wait(lambda: self.widget.load_state[0] == 'error')
                self.assertIsNone(self.widget.view.photo)
        with patch.object(FakePhotos, 'image', return_value=b'not a photo'):
            self.widget.select('C1', refresh=True)
            self.wait(lambda: self.widget.load_state[0] == 'error')
        self.widget.select('C1', refresh=True)
        self.wait(lambda: self.widget.load_state[0] == 'ready')
        self.widget.clear()
        self.assertIsNone(self.widget.view.photo)
        self.assertEqual(self.widget.part, '')


class ImportFeedbackTests(PreferencesTestCase):
    @classmethod
    def setUpClass(cls):
        if QApplication.instance() is None:
            QApplication.setAttribute(Qt.AA_ShareOpenGLContexts)
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        super().setUp()
        for name in ('_setup_preview', 'request_component_info', 'show_preview'):
            stub = patch.object(main.MainWindow, name)
            stub.start()
            self.addCleanup(stub.stop)
        self.window = main.MainWindow(settings_enabled=False)
        self.dialog = self.window.bind_store(FavoritesDialog(self.window, vault=False))
        self.window.show()
        self.dialog.show()

    def tearDown(self):
        self.window.close()
        self.window.deleteLater()
        self.app.sendPostedEvents(None, QEvent.DeferredDelete)

    def test_store_import_popup_reports_added_and_duplicate_counts(self):
        self.dialog.import_requested.emit([{'part': 'C2040', 'title': 'RP2040'}])
        notice = self.dialog.import_notice
        self.assertTrue(notice.isVisible())
        self.assertIn('成功', notice.windowTitle())
        self.assertIn('新增 1', notice.text())
        notice.accept()
        self.dialog.import_requested.emit([{'part': 'C2040', 'title': 'RP2040'}])
        self.assertIn('新增 0', self.dialog.import_notice.text())
        self.assertIn('跳过 1', self.dialog.import_notice.text())
        self.assertEqual(self.window.ids, ['C2040'])

    def test_failed_import_popup_keeps_invalid_input_and_queue(self):
        self.window.input.setPlainText('bad input')
        self.dialog.import_requested.emit([{'part': 'C2040'}])
        self.assertIn('失败', self.dialog.import_notice.windowTitle())
        self.assertIn('无效', self.dialog.import_notice.text())
        self.assertEqual(self.window.ids, [])
        self.assertEqual(self.window.input.toPlainText(), 'bad input')

    def test_keep_individual_options_follow_each_merge_toggle_and_download_state(self):
        self.assertFalse(self.window.keep_schlib_box.isEnabled())
        self.window.schlib_box.setChecked(True)
        self.window.merge_schlib_box.setChecked(True)
        self.assertTrue(self.window.keep_schlib_box.isEnabled())
        self.assertFalse(self.window.keep_pcblib_box.isEnabled())
        self.window.keep_schlib_box.setChecked(True)
        self.window.set_running(True)
        self.assertFalse(self.window.keep_schlib_box.isEnabled())
        self.assertFalse(self.window.library_options_button.isEnabled())
        self.window.set_running(False)
        self.assertTrue(self.window.keep_schlib_box.isChecked())
        self.assertTrue(self.window.keep_schlib_box.isEnabled())
