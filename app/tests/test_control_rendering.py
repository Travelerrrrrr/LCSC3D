"""Render controls and measure their visible borders and selection coverage."""
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
from PySide6.QtWidgets import QApplication, QWidget, QVBoxLayout
from lcsc3d.core.app_logging import close_logging
from lcsc3d.core.app_settings import Preferences
from lcsc3d.ui.app_theme import theme_manager
from lcsc3d.ui.catalog_filters import CategoryPanel
from lcsc3d.ui.localized_widgets import QPushButton, QComboBox


class ControlRenderingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if QApplication.instance() is None:
            QApplication.setAttribute(Qt.AA_ShareOpenGLContexts)
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setStyle('Fusion')

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        env = patch.dict(os.environ, LOCALAPPDATA=directory.name)
        env.start()
        self.addCleanup(env.stop)
        self.addCleanup(close_logging)
        self.root = QWidget()
        self.root.setObjectName('canvas')
        self.root.resize(420, 350)
        self.root.show()
        self.addCleanup(self.close)

    def close(self):
        self.root.close()
        self.root.deleteLater()
        self.app.sendPostedEvents(None, QEvent.DeferredDelete)

    def test_button_straight_edges_have_equal_pixel_coverage_in_every_state(self):
        button = QPushButton('', self.root)
        button.setGeometry(17, 19, 161, 45)
        button.setCheckable(True)
        button.show()
        for mode in ('light', 'dark'):
            theme_manager().apply(Preferences(theme_mode=mode))
            for role, checked, enabled in (('', False, True), ('', False, False),
                                            ('navButton', True, True), ('pageButton', True, True)):
                button.setObjectName(role)
                button.setChecked(checked)
                button.setEnabled(enabled)
                button.setFocus()
                self.app.processEvents()
                image = button.grab().toImage()
                band = max(4, round(image.devicePixelRatio() * 4))
                # Same cross-section on all sides; the rounded-up grab may
                # contain one transparent pixel outside the widget at 150%.
                lines = [[image.pixelColor(image.width() // 2, y).getRgb()
                          for y in range(band)],
                         [image.pixelColor(image.width() // 2, image.height() - 1 - y).getRgb()
                          for y in range(band)],
                         [image.pixelColor(x, image.height() // 2).getRgb()
                          for x in range(band)],
                         [image.pixelColor(image.width() - 1 - x, image.height() // 2).getRgb()
                          for x in range(band)]]
                c = theme_manager().tokens
                target = QColor(c['accent_ink' if enabled and (checked or button.hasFocus()) else 'border']).getRgb()
                coverage = [sum(pixel[3] > 220 and max(abs(pixel[channel] - target[channel])
                                                     for channel in range(3)) < 16
                                for pixel in line) for line in lines]
                with self.subTest(mode=mode, role=role, enabled=enabled):
                    self.assertGreater(min(coverage), 0)
                    self.assertEqual(min(coverage), max(coverage), coverage)

    def test_dropdown_selected_row_fills_its_full_width_and_height(self):
        combo = QComboBox(self.root)
        combo.setGeometry(15, 12, 220, 36)
        combo.addItems(['嘉立创风格', 'AD 风格'])
        combo.setCurrentIndex(1)
        combo.show()
        for mode in ('light', 'dark'):
            theme_manager().apply(Preferences(theme_mode=mode))
            combo.showPopup()
            self.app.processEvents()
            view, panel = combo.view(), combo.popup_frame
            rect = view.visualRect(view.model().index(1, 0))
            self.assertEqual(view.visualRect(view.model().index(0, 0)).top(), 0)
            self.assertEqual(rect.bottom(), view.viewport().height() - 1)
            pixels = view.viewport().grab().toImage()
            ratio = pixels.devicePixelRatio()
            target = QColor(theme_manager().tokens['accent'])
            for x, y in ((1, rect.center().y()), (rect.right() - 1, rect.center().y()),
                         (rect.center().x(), rect.top() + 1), (rect.center().x(), rect.bottom() - 1)):
                actual = pixels.pixelColor(round(x * ratio), round(y * ratio))
                self.assertEqual(actual.name(), target.name(), (mode, x, y, actual.name()))
            self.assertEqual(panel.width(), combo.width())
            QTest.keyClick(view, Qt.Key_Home)
            QTest.keyClick(view, Qt.Key_Escape)
            self.assertEqual(combo.currentIndex(), 1)

    def test_category_branch_column_has_only_vector_disclosures(self):
        layout = QVBoxLayout(self.root)
        panel = CategoryPanel()
        layout.addWidget(panel)
        panel.show_categories([dict(id='root', name='分类', children=[
            dict(id='parent', name='单片机', children=[dict(id='child', name='MCU', children=[])]),
            dict(id='other', name='逻辑器件', children=[])])])
        tree = panel.tree
        parent = tree.topLevelItem(0)
        parent.setExpanded(True)
        tree.setCurrentItem(parent.child(0))
        self.app.processEvents()
        image = tree.viewport().grab().toImage()
        ratio = image.devicePixelRatio()
        # No vertical connector image, tinted block or stretched bitmap in an
        # unselected branch gutter below the disclosure row.
        lower = tree.visualItemRect(tree.topLevelItem(1))
        x = max(1, round(tree.indentation() * ratio / 2))
        color = QColor(theme_manager().tokens['surface']).name()
        for y in range(round(lower.top() * ratio), min(image.height(), round(lower.bottom() * ratio))):
            self.assertEqual(image.pixelColor(x, y).name(), color)
        point = tree.visualItemRect(parent).topLeft() - QPoint(tree.indentation() // 2, 0)
        point.setY(tree.visualItemRect(parent).center().y())
        QTest.mouseClick(tree.viewport(), Qt.LeftButton, pos=point)
        self.assertFalse(parent.isExpanded())


if __name__ == '__main__':
    unittest.main()
