"""Removing checked queue entries must preserve files and unrelated UI state."""
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import main
from PySide6.QtCore import QEvent, Qt
from PySide6.QtWidgets import QApplication


@unittest.skipUnless(sys.platform == 'win32', 'Native Windows queue regression')
class QueueDeletionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        QApplication.setAttribute(Qt.AA_ShareOpenGLContexts)
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        for name in ('_setup_preview', 'request_component_info'):
            mock = patch.object(main.MainWindow, name)
            mock.start()
            self.addCleanup(mock.stop)
        preview = patch.object(main.MainWindow, 'show_preview', autospec=True,
                               side_effect=lambda window, part: setattr(window, 'current_preview', part))
        self.preview = preview.start()
        self.addCleanup(preview.stop)
        self.window = main.MainWindow(settings_enabled=False)
        self.window.input.setPlainText('C2040\nC20197\nC163691')
        self.window.load_queue()

    def tearDown(self):
        self.window.info_worker = None
        self.window.close()
        self.window.deleteLater()
        self.app.sendPostedEvents(None, QEvent.DeferredDelete)

    def check(self, rows):
        for row in range(len(self.window.ids)):
            self.window.table.item(row, main.DOWNLOAD_COLUMN).setCheckState(Qt.Checked if row in rows else Qt.Unchecked)

    def test_deleting_checked_rows_preserves_unchecked_results_preview_and_downloaded_files(self):
        with tempfile.TemporaryDirectory() as directory:
            files = [Path(directory) / (part + '.step') for part in self.window.ids]
            for file in files:
                file.write_text('existing downloaded model', encoding='utf-8')
            kept = SimpleNamespace(part='C20197', title='Resistor', model='', folder=directory, message='已保存', status='成功')
            self.window.results['C20197'] = kept
            self.window.table.item(1, main.RESULT_COLUMN).setText('成功')
            self.window.table.selectRow(1)
            self.check({0, 2})
            self.window.remove_checked_button.click()
            self.assertEqual(self.window.ids, ['C20197'])
            self.assertEqual(self.window.input.toPlainText(), 'C20197')
            self.assertIs(self.window.results['C20197'], kept)
            self.assertEqual(self.window.table.item(0, main.RESULT_COLUMN).text(), '成功')
            self.assertEqual(self.window.table.item(0, main.DOWNLOAD_COLUMN).checkState(), Qt.Unchecked)
            self.assertEqual(self.window.current_preview, 'C20197')
            self.assertTrue(all(file.read_text(encoding='utf-8') == 'existing downloaded model' for file in files))

    def test_delete_is_unavailable_without_checks_and_during_downloads(self):
        self.check(set())
        self.assertFalse(self.window.remove_checked_button.isEnabled())
        self.assertEqual(self.window.remove_checked_downloads(), 0)
        self.check({0})
        self.window.set_running(True)
        self.assertFalse(self.window.remove_checked_button.isEnabled())
        self.assertEqual(self.window.remove_checked_downloads(), 0)
        self.assertEqual(len(self.window.ids), 3)
        self.window.set_running(False)
        self.assertTrue(self.window.remove_checked_button.isEnabled())

    def test_deleting_the_current_preview_selects_the_next_surviving_component(self):
        self.check({0})
        self.window.remove_checked_button.click()
        self.assertEqual(self.window.ids, ['C20197', 'C163691'])
        self.assertEqual(self.window.current_preview, 'C20197')
        self.assertEqual(self.window.selected_part(), 'C20197')

    def test_deleted_final_preview_is_cleared_and_late_model_data_is_ignored(self):
        part, revision = self.window.current_preview, self.window.web_revision
        self.window.current_3d = part
        self.window.remove_checked_button.click()
        self.assertFalse(self.window.ids)
        self.assertEqual(self.window.input.toPlainText(), '')
        self.assertEqual(self.window.current_preview, '')
        self.assertEqual(self.window.preview_state, 'empty')
        self.assertIs(self.window.preview_stack.currentWidget(), self.window.preview_empty)
        self.window.model_preview_loaded(part, revision, {'stale': True})
        self.window.on_web_preview_state(revision, 'ready', 'stale')
        self.assertIsNone(self.window.web_model)
        self.assertEqual(self.window.preview_state, 'empty')

    def test_delete_keeps_pending_manual_input_and_does_not_hide_invalid_tokens(self):
        self.window.input.setPlainText('c2040 | (C4); INVALID; C20197; C163691')
        self.check({0})
        self.window.remove_checked_button.click()
        self.assertEqual(self.window.input.toPlainText().splitlines(), ['C20197', 'C163691', 'C4', 'INVALID'])

    def test_late_metadata_cannot_restore_a_deleted_row_or_write_to_the_wrong_row(self):
        old_revision = self.window.info_revision
        self.window.info_worker = SimpleNamespace(cancelled=threading.Event())
        self.check({0})
        self.window.remove_checked_button.click()
        self.assertTrue(self.window.info_worker.cancelled.is_set())
        self.window.update_component_info(old_revision, 'C2040', {'title': 'stale deleted'}, '')
        self.window.update_component_info(old_revision, 'C20197', {'title': 'stale kept'}, '')
        self.assertEqual(self.window.info_rows, {'C20197': 0, 'C163691': 1})
        self.assertNotIn('C2040', self.window.component_info)
        self.assertNotEqual(self.window.table.item(0, main.MODEL_COLUMN).text(), 'stale kept')

    def test_an_unrelated_marketplace_preview_is_preserved(self):
        self.window.current_preview = 'C499531'
        self.check({0})
        self.window.remove_checked_button.click()
        self.assertEqual(self.window.current_preview, 'C499531')
