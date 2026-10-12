"""The embedded Material picker keeps visible icons and Qt file selection."""
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PySide6.QtCore import QEvent, Qt, QUrl
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QFileDialog, QComboBox, QLineEdit, QListView
from lcsc3d.core.app_logging import close_logging
from lcsc3d.core.app_settings import Preferences
from lcsc3d.ui.app_theme import theme_manager
from lcsc3d.ui.shell_ui import choose_path
from lcsc3d.ui import main


class FilePickerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if QApplication.instance() is None:
            QApplication.setAttribute(Qt.AA_ShareOpenGLContexts)
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.files = self.root / '工程文件'
        self.files.mkdir()
        (self.files / '模型').mkdir()
        (self.files / '符号.SchLib').write_bytes(b'picker fixture')
        (self.files / '封装.PcbLib').write_bytes(b'picker fixture')
        env = patch.dict(os.environ, LOCALAPPDATA=str(self.root / 'profile'))
        env.start()
        self.addCleanup(env.stop)
        self.addCleanup(close_logging)
        for name in ('_setup_preview', 'show_preview', 'request_component_info'):
            stub = patch.object(main.MainWindow, name)
            stub.start()
            self.addCleanup(stub.stop)
        self.window = main.MainWindow(settings_enabled=False)
        self.window.resize(1200, 800)
        self.window.show()
        self.addCleanup(self.close)
        self.selected = []

    def close(self):
        self.window.close()
        self.window.deleteLater()
        self.app.sendPostedEvents(None, QEvent.DeferredDelete)

    def settle(self):
        for _ in range(5):
            self.app.processEvents()

    def picker(self, directory=True, parent=None):
        page = choose_path(parent or self.window, '选择文件', str(self.files), self.selected.append,
                           directory=directory, filter='' if directory else 'AD (*.SchLib)')
        page.setSidebarUrls([QUrl.fromLocalFile(str(self.files))])
        self.settle()
        return page

    def test_icons_are_painted_inside_buttons_in_light_dark_and_custom_themes(self):
        page = self.picker()
        for mode, accent in (('light', '#087af5'), ('dark', '#087af5'),
                             ('light', '#ffffff'), ('dark', '#000000')):
            with self.subTest(mode=mode, accent=accent):
                self.window.apply_preferences(Preferences(theme_mode=mode, accent_color=accent))
                self.settle()
                fingerprints = set()
                for button in page.toolbar_buttons.values():
                    self.assertTrue(button.isVisible())
                    capture = button.grab().toImage()
                    ratio = capture.devicePixelRatio()
                    half = round(button.iconSize().width() * ratio / 2)
                    cx, cy = capture.width() // 2, capture.height() // 2
                    expected = theme_manager().tokens['accent_ink' if button.isEnabled() else 'disabled']
                    pixels = [capture.pixelColor(x, y).name() for y in range(cy - half, cy + half)
                              for x in range(cx - half, cx + half)]
                    # Test the rendered glyph, not just QIcon.isNull(): the old
                    # icons existed but their content rect was eaten by borders.
                    ink = sum(max(abs(int(color[i:i + 2], 16) - int(expected[i:i + 2], 16))
                                  for i in (1, 3, 5)) < 30 for color in pixels)
                    # Compare logical area across display scales. At 100%,
                    # the thin parent arrow has only 14 full-color pixels;
                    # antialiased edge pixels do not pass the color cutoff.
                    self.assertGreater(ink / ratio ** 2, 8, button.objectName())
                    self.assertLess(ink, len(pixels) // 2, button.objectName())
                    fingerprints.add(tuple(pixels))
                self.assertEqual(len(fingerprints), 5)

    def test_navigation_view_switching_and_directory_accept_keep_native_behavior(self):
        page = self.picker()
        page.toolbar_buttons['toParentButton'].click()
        self.settle()
        self.assertEqual(Path(page.directory().absolutePath()).resolve(), self.root)
        self.assertTrue(page.toolbar_buttons['backButton'].isEnabled())
        page.toolbar_buttons['backButton'].click()
        self.settle()
        self.assertEqual(Path(page.directory().absolutePath()).resolve(), self.files)
        self.assertTrue(page.toolbar_buttons['forwardButton'].isEnabled())
        page.toolbar_buttons['forwardButton'].click()
        self.settle()
        self.assertEqual(Path(page.directory().absolutePath()).resolve(), self.root)
        sidebar = page.findChild(QListView, 'sidebar')
        item = sidebar.model().index(0, 0)
        QTest.mouseClick(sidebar.viewport(), Qt.LeftButton, pos=sidebar.visualRect(item).center())
        self.settle()
        self.assertEqual(Path(page.directory().absolutePath()).resolve(), self.files)
        for name, mode in (('listModeButton', QFileDialog.List), ('detailModeButton', QFileDialog.Detail)):
            page.toolbar_buttons[name].click()
            self.assertEqual(page.viewMode(), mode)
            self.assertTrue(page.toolbar_buttons[name].isChecked())
            self.assertEqual(sum(button.isChecked() for button in page.toolbar_buttons.values()), 1)
        self.assertEqual(page.labelText(QFileDialog.Accept), '选择')
        self.assertFalse(page.findChild(QComboBox, 'fileTypeCombo').isVisible())
        selected = self.files / '模型'
        page.selectFile(str(selected))
        page.accept()
        self.assertEqual([Path(value).resolve() for value in self.selected], [selected])
        self.assertIs(self.window._page_host.current_page(), self.window._page_host.workspace)

    def test_file_filter_selection_cancel_and_nested_return_preserve_settings(self):
        self.window.configure_export_targets()
        owner = self.window.export_dialog
        page = self.picker(directory=False, parent=owner)
        self.assertEqual(page.fileMode(), QFileDialog.ExistingFile)
        self.assertEqual(page.nameFilters(), ['AD (*.SchLib)'])
        self.assertTrue(page.findChild(QComboBox, 'fileTypeCombo').isVisible())
        selected = self.files / '符号.SchLib'
        page.selectFile(str(selected))
        page.accept()
        self.assertEqual([Path(value).resolve() for value in self.selected], [selected])
        self.assertIs(self.window._page_host.current_page(), owner)
        page = self.picker(parent=owner)
        page.selectFile(str(self.files / '不存在的目录'))
        page.accept()
        self.assertFalse(page._page_finished)
        page.reject()
        self.assertEqual(len(self.selected), 1)
        self.assertIs(self.window._page_host.current_page(), owner)
        self.assertEqual(owner.inputs['project_path'].text(), '')

    def test_panels_and_toolbar_remain_reachable_at_large_fonts_and_language_changes(self):
        page = self.picker(directory=False)
        filename = page.findChild(QLineEdit, 'fileNameEdit')
        filename.setText('符号.SchLib')
        for language, size in (('zh_CN', 13), ('en_US', 24), ('zh_CN', 13)):
            with self.subTest(language=language, size=size):
                self.window.apply_preferences(Preferences(language=language, font_size=size))
                self.window.resize(1060, 740)
                self.settle()
                self.assertEqual(Path(page.directory().absolutePath()).resolve(), self.files)
                self.assertEqual(filename.text(), '符号.SchLib')
                self.assertEqual(page.nameFilters(), ['AD (*.SchLib)'])
                self.assertEqual(page.locations_title.text(), 'Places' if language == 'en_US' else '常用位置')
                self.assertEqual(page.toolbar_buttons['backButton'].toolTip(), 'Back' if language == 'en_US' else '后退')
                self.assertLess(page.locations_panel.geometry().right(), page.listing_panel.geometry().left())
                page.file_splitter.setSizes([240, 500])
                self.assertGreaterEqual(page.file_splitter.sizes()[0], 170)
                frame = self.window._page_host.frames[page]
                for button in page.toolbar_buttons.values():
                    frame.ensureWidgetVisible(button)
                    self.settle()
                    self.assertTrue(frame.viewport().rect().contains(
                        button.mapTo(frame.viewport(), button.rect().center())))
                self.assertLessEqual(self.window.width(), 1060)
                self.assertIsNone(self.app.activeModalWidget())


if __name__ == '__main__':
    unittest.main()
