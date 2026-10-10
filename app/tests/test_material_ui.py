"""Material integration boundaries and usable navigation at supported sizes."""
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PySide6.QtCore import Qt, QEvent
from PySide6.QtWidgets import QApplication
from app_settings import Preferences
from app_theme import theme_manager
from app_paths import data_directory
from app_logging import close_logging
import main


class MaterialUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if QApplication.instance() is None:
            QApplication.setAttribute(Qt.AA_ShareOpenGLContexts)
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='Material UI 中文 ')
        self.addCleanup(self.directory.cleanup)
        self.env = patch.dict(os.environ, LOCALAPPDATA=self.directory.name)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.addCleanup(close_logging)
        theme_manager().applied = False
        self.window = main.MainWindow(settings_enabled=False)
        self.window.show()
        self.app.processEvents()
        self.addCleanup(self.close_window)

    def close_window(self):
        self.window.close()
        self.window.deleteLater()
        self.app.sendPostedEvents(None, QEvent.DeferredDelete)

    def test_upstream_theme_and_icons_are_cached_only_in_app_data_and_reused(self):
        manager = theme_manager()
        self.assertIn('Qt-Material', self.app.styleSheet())
        self.assertNotIn('icon:/', self.app.styleSheet())
        cache = data_directory() / 'cache' / 'qt-material-2.17'
        icons = list(cache.glob('*/icons/primary/downarrow.svg'))
        self.assertEqual(len(icons), 1)
        first = {path: path.stat().st_mtime_ns for path in cache.rglob('*') if path.is_file()}
        manager.apply(Preferences(theme_mode='dark'))
        manager.apply(Preferences(theme_mode='light'))
        for path, timestamp in first.items():
            self.assertEqual(path.stat().st_mtime_ns, timestamp)
        self.assertFalse(list(cache.glob('build-*')))
        self.assertEqual({p.name for p in Path(self.directory.name).iterdir()}, {'LCSC3D'})

    def test_export_action_remains_reachable_in_small_window_and_large_fonts(self):
        w = self.window
        for language, size in (('zh_CN', 13), ('en_US', 24)):
            w.apply_preferences(Preferences(language=language, font_size=size))
            w.resize(1060, 740)
            w.schlib_box.setChecked(True)
            w.lib_merge_box.setChecked(True)
            for _ in range(5):
                self.app.processEvents()
            w.content_scroll.ensureWidgetVisible(w.start_button)
            self.app.processEvents()
            viewport = w.content_scroll.viewport()
            point = w.start_button.mapTo(viewport, w.start_button.rect().center())
            self.assertTrue(viewport.rect().contains(point))
            self.assertGreater(w.table.height(), 80)
            self.assertLess(w.table.geometry().bottom(), w.progress_bar.geometry().top())
            self.assertLessEqual(w.width(), 1060)
            self.assertLessEqual(w.height(), 740)

    def test_settings_categories_preserve_unsaved_edits_and_cancel(self):
        w = self.window
        w.settings_button.click()
        d = w.settings_dialog
        d.font_size_spin.setValue(18)
        d.theme_mode_combo.setCurrentIndex(d.theme_mode_combo.findData('dark'))
        d.section_buttons[1].click()
        self.assertTrue(d.store_proxy_combo.isVisible())
        d.section_buttons[2].click()
        self.assertTrue(d.package_log_button.isVisible())
        d.section_buttons[3].click()
        self.assertTrue(d.update_button.isVisible())
        d.section_buttons[0].click()
        self.assertEqual(d.font_size_spin.value(), 18)
        self.assertTrue(d.font_combo.isVisible())
        d.cancel_button.click()
        self.assertEqual(w.preferences, Preferences())


if __name__ == '__main__':
    unittest.main()
