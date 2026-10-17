"""Material integration boundaries and usable navigation at supported sizes."""
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PySide6.QtCore import Qt, QEvent, QPoint, QPointF, QObject
from PySide6.QtGui import QWheelEvent
from PySide6.QtTest import QTest, QSignalSpy
from PySide6.QtWidgets import QApplication
from lcsc3d.core.app_settings import Preferences
from lcsc3d.ui.app_theme import theme_manager
from lcsc3d.core.app_paths import data_directory
from lcsc3d.core.app_logging import close_logging
from lcsc3d.ui import main


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
            self.assertTrue(w.export_card.isAncestorOf(w.progress_bar))
            self.assertTrue(w.export_card.isAncestorOf(w.run_status))
            self.assertLessEqual(w.width(), 1060)
            self.assertLessEqual(w.height(), 740)

    def test_download_queue_shows_five_rows_and_scrolls_without_moving_workspace(self):
        w = self.window
        with patch.object(w, 'request_component_info'), patch.object(w, 'show_preview'):
            w.input.setPlainText('\n'.join('C' + str(n) for n in range(1, 41)))
            w.load_queue()
        w.resize(1380, 880)
        QTest.qWait(100)
        self.assertGreaterEqual(w.table.viewport().height(), 5 * w.table.rowHeight(0))
        self.assertEqual(w.content_scroll.verticalScrollBar().maximum(), 0)
        bar = w.table.verticalScrollBar()
        self.assertGreaterEqual(bar.width(), 14)
        point = w.table.viewport().rect().center()
        wheel = QWheelEvent(QPointF(point), QPointF(w.table.viewport().mapToGlobal(point)), QPoint(), QPoint(0, -120),
                            Qt.NoButton, Qt.NoModifier, Qt.NoScrollPhase, False)
        self.app.sendEvent(w.table.viewport(), wheel)
        QTest.qWait(50)
        self.assertGreater(bar.value(), 0)
        self.assertEqual(w.content_scroll.verticalScrollBar().value(), 0)
        bar.setFocus()
        QTest.keyClick(bar, Qt.Key_End)
        self.assertEqual(bar.value(), bar.maximum())
        self.assertTrue(w.table.viewport().rect().intersects(w.table.visualItemRect(w.table.item(39, 1))))
        self.assertTrue(w.export_card.isAncestorOf(w.progress_bar))
        self.assertTrue(w.export_card.isAncestorOf(w.run_status))

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

    def test_result_column_follows_the_table_right_edge_after_window_and_splitter_resize(self):
        w = self.window
        header = w.table.horizontalHeader()
        columns = [w.table.columnWidth(col) for col in range(main.RESULT_COLUMN)]
        # Stretching must preserve the requested result width when a small
        # screen/splitter leaves less room than the columns need in total.
        result_width = 100
        minimum_width = sum(columns) + result_width

        def assert_filled_or_scrollable():
            self.assertEqual([w.table.columnWidth(col) for col in range(main.RESULT_COLUMN)], columns)
            self.assertEqual(header.length(), max(minimum_width, w.table.viewport().width()))
            bar = w.table.horizontalScrollBar()
            bar.setValue(bar.maximum())
            edge = header.sectionViewportPosition(main.RESULT_COLUMN) + header.sectionSize(main.RESULT_COLUMN)
            self.assertEqual(edge, w.table.viewport().width())
            bar.setValue(0)

        for width, sizes in ((1380, [620, 420]), (1600, [820, 420]), (1200, [580, 350])):
            w.resize(width, 900)
            w.workspace_splitter.setSizes(sizes)
            QTest.qWait(60)
            assert_filled_or_scrollable()
        # Exercise overflow even on a large local display, both without and
        # with the vertical scrollbar taking space from the viewport.
        w.table.setFixedWidth(minimum_width - 10)
        QTest.qWait(60)
        self.assertTrue(w.table.horizontalScrollBar().isVisible())
        assert_filled_or_scrollable()
        with patch.object(w, 'request_component_info'), patch.object(w, 'show_preview'):
            w.input.setPlainText('\n'.join('C' + str(n) for n in range(1, 41)))
            w.load_queue()
        QTest.qWait(60)
        self.assertTrue(w.table.verticalScrollBar().isVisible())
        assert_filled_or_scrollable()
        w.table.setColumnWidth(main.MODEL_COLUMN, 240)
        w.apply_preferences(Preferences(theme_mode='dark'))
        QTest.qWait(50)
        self.assertEqual(w.table.columnWidth(main.MODEL_COLUMN), 240)

    def test_wrapped_result_details_and_export_controls_never_overlap(self):
        w = self.window
        with patch.object(w, 'request_component_info'), patch.object(w, 'show_preview'):
            w.input.setPlainText('\n'.join(f'C{i}' for i in range(12)))
            w.load_queue()
        message = 'C5879483 · 已合并 PCBLIB → lcsc3d_v2_3_0_hb.PcbLib（12 个元件），已复用 2 个相同 footprint；已合并 SCHLIB → lcsc3d_v2_3_0_hb.SchLib（12 个元件）'
        def top(widget):
            return widget.mapTo(w, QPoint(0,0)).y()
        for language, font, width, height in (('zh_CN',13,1380,880),('zh_CN',18,1060,740),('en_US',24,1060,740)):
            w.apply_preferences(Preferences(language=language,font_size=font))
            w.resize(width,height)
            w.schlib_box.setChecked(True)
            w.pcblib_box.setChecked(True)
            w.lib_merge_box.setChecked(True)
            for text, sizes in ((message,[560,590]), (message*3,[350,800]), ('Ready',[700,450])):
                w.detail.setText(text)
                w.workspace_splitter.setSizes(sizes)
                QTest.qWait(80)
                self.assertGreaterEqual(top(w.detail),top(w.table)+w.table.height()+8)
                self.assertGreaterEqual(w.detail.height(),w.detail.heightForWidth(w.detail.width()))
                self.assertGreaterEqual(top(w.export_card),top(w.workspace_splitter)+w.workspace_splitter.height()+12)
                self.assertGreaterEqual(w.workspace_splitter.widget(0).height(),w.workspace_splitter.widget(0).layout().minimumHeightForWidth(w.workspace_splitter.widget(0).width()))
            w.lib_merge_box.setChecked(False)
            QTest.qWait(80)
            self.assertGreaterEqual(top(w.export_card),top(w.workspace_splitter)+w.workspace_splitter.height()+12)

    def test_merge_toggle_paints_only_final_card_geometry(self):
        w=self.window
        frames=[]
        class Observer(QObject):
            def eventFilter(self,widget,event):
                if event.type() == QEvent.Paint:
                    frames.append((widget.geometry().getRect(),widget.mapTo(w,QPoint()).y()))
                return False
        observer=Observer(w)
        w.export_card.installEventFilter(observer)
        try:
            for language,size,width,height in (('zh_CN',13,1380,880),('en_US',24,1060,740)):
                w.apply_preferences(Preferences(language=language,font_size=size))
                w.resize(width,height)
                for target in ('ad','kicad'):
                    w.export_target_box.setCurrentIndex(w.export_target_box.findData(target))
                    w.schlib_box.setChecked(True);w.pcblib_box.setChecked(True)
                    w.kicad_symbol_box.setChecked(True);w.kicad_footprint_box.setChecked(True)
                    box=w.lib_merge_box if target=='ad' else w.kicad_lib_merge_box
                    # Discard the normal mouse-down paint of the previous state.
                    box.toggled.connect(frames.clear)
                    for _ in range(4):
                        w.content_scroll.ensureWidgetVisible(box)
                        QTest.qWait(60)
                        before=box.isChecked()
                        QTest.mouseClick(box,Qt.LeftButton)
                        QTest.qWait(120)
                        self.assertNotEqual(box.isChecked(),before,(language,size,target))
                        self.assertTrue(frames)
                        self.assertEqual(set(frames),{(w.export_card.geometry().getRect(),w.export_card.mapTo(w,QPoint()).y())},
                            (language,size,target,box.isChecked(),frames))
                    box.toggled.disconnect(frames.clear)
            idle=QSignalSpy(w.export_layout_timer.timeout)
            QTest.qWait(100)
            self.assertEqual(idle.count(),0,'Layout must stop scheduling work after it settles')
        finally:
            w.export_card.removeEventFilter(observer)


if __name__ == '__main__':
    unittest.main()
