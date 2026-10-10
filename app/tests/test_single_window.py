"""In-app navigation, notifications and visual regressions from user screenshots."""
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PySide6.QtCore import QEvent, QPoint, Qt
from PySide6.QtGui import QColor
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QCheckBox
from app_logging import close_logging
from app_settings import Preferences
from shell_ui import notify
from ui_components import ColumnSplitter
import main


class SingleWindowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if QApplication.instance() is None:
            QApplication.setAttribute(Qt.AA_ShareOpenGLContexts)
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        env = patch.dict(os.environ, LOCALAPPDATA=self.folder.name)
        env.start()
        self.addCleanup(env.stop)
        self.addCleanup(close_logging)
        for target in ('main.MainWindow._setup_preview', 'main.MainWindow.request_component_info',
                       'main.MainWindow.show_preview', 'favorites.LoginDialog.begin'):
            stub = patch(target)
            stub.start()
            self.addCleanup(stub.stop)
        self.window = main.MainWindow(settings_enabled=False)
        self.window.show()
        self.settle()
        self.addCleanup(self.close)

    def close(self):
        self.window.close()
        self.window.deleteLater()
        self.app.sendPostedEvents(None, QEvent.DeferredDelete)

    def settle(self):
        for _ in range(5):
            self.app.processEvents()

    def assert_one_window(self):
        self.settle()
        self.assertIsNone(self.app.activeModalWidget())
        visible = [widget for widget in self.app.topLevelWidgets() if widget.isVisible()
                   and widget.windowType() != Qt.ToolTip]
        self.assertEqual(visible, [self.window])

    def test_navigation_keeps_queue_and_unsaved_settings_in_one_native_window(self):
        w = self.window
        w.input.setPlainText('C2040\nC20197')
        w.load_queue()
        w.table.item(1, main.DOWNLOAD_COLUMN).setCheckState(Qt.Unchecked)
        w.open_favorites()
        store = w.favorites_dialog
        store.search_input.setText('RP2040')
        w.open_settings()
        settings = w.settings_dialog
        settings.font_size_spin.setValue(18)
        w.open_favorites()
        self.assertEqual(store.search_input.text(), 'RP2040')
        w.open_settings()
        self.assertIs(w.settings_dialog, settings)
        self.assertEqual(settings.font_size_spin.value(), 18)
        self.assertEqual(w.preferences.font_size, 13)
        w.workspace_button.click()
        self.assertEqual(w.ids, ['C2040', 'C20197'])
        self.assertEqual(w.table.item(1, main.DOWNLOAD_COLUMN).checkState(), Qt.Unchecked)
        self.assert_one_window()

    def test_color_file_login_sponsor_export_and_help_are_embedded_pages(self):
        w = self.window
        w.open_settings()
        settings = w.settings_dialog
        settings.color_button.click()
        self.assertIs(w._page_host.current_page(), settings.color_page)
        self.assert_one_window()
        settings.color_page.setCurrentColor(QColor('#334455'))
        settings.color_page.accept()
        self.assertEqual(settings.accent_combo.currentData(), '#334455')
        self.assertIs(w._page_host.current_page(), settings)
        settings.sponsor_button.click()
        self.assert_one_window()
        w.back_button.click()
        self.assertIs(w._page_host.current_page(), settings)
        w.open_account()
        self.assertIs(w._page_host.current_page(), w.favorites_dialog.login_dialog)
        self.assert_one_window()
        w.back_button.click()
        w.configure_export_targets()
        self.assert_one_window()
        w.export_dialog.reject()
        w.choose_folder()
        self.assert_one_window()
        w.folder_page.selectFile(self.folder.name)
        w.folder_page.accept()
        self.assertEqual(Path(w.path_input.text()).resolve(), Path(self.folder.name).resolve())
        w.show_help()
        self.assert_one_window()
        self.assertIs(w._page_host.current_page(), w.help_page)

    def test_notification_slides_from_right_and_confirmation_requires_a_click(self):
        calls = []
        toast = notify(self.window, 'Title', 'Message', persistent=True,
                       actions=(('Cancel', None), ('Confirm', lambda: calls.append(True))))
        start = toast.x()
        QTest.qWait(300)
        self.assertLess(toast.x(), start)
        self.assertEqual(toast.geometry().right(), toast.parentWidget().width() - 17)
        self.assertFalse(calls)
        self.assert_one_window()
        toast.action_buttons[1].click()
        self.assertEqual(calls, [True])

    def test_every_combo_opens_below_its_field_with_same_width_and_escape_keeps_value(self):
        w = self.window
        for mode in ('light', 'dark'):
            w.apply_preferences(Preferences(theme_mode=mode, font_family='Cascadia Code', font_size=12))
            w.open_settings()
            settings = w.settings_dialog
            for combo in (settings.language_combo, settings.text_color_combo, settings.font_combo):
                self.settle()
                original = combo.currentIndex()
                combo.showPopup()
                self.settle()
                popup = combo.popup_frame
                expected = combo.mapTo(popup.parentWidget(), QPoint(0, combo.height() + 4))
                self.assertEqual(popup.pos(), expected)
                self.assertEqual(popup.width(), combo.width())
                self.assert_one_window()
                QTest.keyClick(combo.view(), Qt.Key_End)
                QTest.keyClick(combo.view(), Qt.Key_Escape)
                self.assertEqual(combo.currentIndex(), original)
                self.assertFalse(popup.isVisible())
            settings.reject()

    def test_account_rows_follow_anchor_width_even_after_the_caption_changes(self):
        w = self.window
        store = w.ensure_store()
        for name in ('A', 'Account with a much longer name'):
            store.client.account = {'name': name}
            w.refresh_store_account()
            w.open_account()
            self.settle()
            panel = w.account_menu
            self.assertEqual(panel.width(), w.account_button.width())
            self.assertEqual(len({button.width() for button in panel.buttons}), 1)
            self.assert_one_window()
            panel.close()

    def test_file_chooser_dropdowns_stay_inside_the_same_window(self):
        self.window.choose_folder()
        page = self.window.folder_page
        self.settle()
        for dropdown in page.dropdowns:
            if not dropdown.combo.isVisible() or not dropdown.combo.count():
                continue
            QTest.mouseClick(dropdown.combo, Qt.LeftButton)
            self.settle()
            self.assertTrue(dropdown.frame.isVisible())
            self.assertEqual(dropdown.frame.width(), dropdown.combo.width())
            self.assert_one_window()
            QTest.keyClick(dropdown.view, Qt.Key_Escape)
            self.assertFalse(dropdown.frame.isVisible())
        page.reject()

    def test_store_uses_shared_splitter_has_no_native_tab_base_and_support_text_is_restored(self):
        w = self.window
        w.open_favorites()
        store = w.favorites_dialog
        self.assertIsInstance(store.product_splitter, ColumnSplitter)
        self.assertEqual(store.product_splitter.handleWidth(), w.workspace_splitter.handleWidth())
        self.assertFalse(store.tabs.tabBar().drawBase())
        w.open_settings()
        d = w.settings_dialog
        self.assertEqual(d.star_button.text(), '⭐点个Star⭐')
        self.assertEqual(d.sponsor_button.text(), '🍔赞助作者🍔')

    def test_purple_checkbox_has_no_black_outer_frame(self):
        self.window.apply_preferences(Preferences(theme_mode='light', accent_color='#7c3aed'))
        box = QCheckBox('', self.window)
        box.setChecked(True)
        box.resize(28, 28)
        image = box.grab().toImage()
        black = sum(image.pixelColor(x, y).alpha() > 0 and image.pixelColor(x, y).name() == '#000000'
                    for y in range(image.height()) for x in range(image.width()))
        self.assertEqual(black, 0)
        purple = sum(image.pixelColor(x, y).name() == '#7c3aed'
                     for y in range(image.height()) for x in range(image.width()))
        self.assertGreater(purple, 25)


if __name__ == '__main__':
    unittest.main()
