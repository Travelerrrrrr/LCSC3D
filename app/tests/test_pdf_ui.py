"""PDF selection, settings, missing-data notice and UI geometry."""
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from PySide6.QtCore import Qt, QEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from lcsc3d.ui import main
from lcsc3d.services.pdf_download import DatasheetBatch, MISSING_REPORT
from lcsc3d.core.app_logging import close_logging
from test_pdf_download import pdf_bytes


class PDFWindowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if QApplication.instance() is None:
            QApplication.setAttribute(Qt.AA_ShareOpenGLContexts)
        cls.app=QApplication.instance() or QApplication([])

    def setUp(self):
        temp=tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root=Path(temp.name).resolve()
        env=patch.dict(os.environ,LOCALAPPDATA=str(self.root/'profile')); env.start(); self.addCleanup(env.stop)
        self.addCleanup(close_logging)
        for name in ('_setup_preview','show_preview','request_component_info'):
            mock=patch.object(main.MainWindow,name); mock.start(); self.addCleanup(mock.stop)
        self.window=main.MainWindow(settings_enabled=False)
        self.window.ensure_store(deferred=True)
        self.window.input.setPlainText('C2040\nC20197'); self.window.load_queue()
        self.window.path_input.setText(str(self.root/'exports'))

    def tearDown(self):
        if self.window.worker and self.window.worker.isRunning():
            self.window.worker.cancelled.set(); self.window.worker.wait(5000)
        self.app.processEvents()
        self.window.settings_enabled=False
        self.window.close(); self.window.deleteLater()
        self.app.sendPostedEvents(None,QEvent.DeferredDelete)

    def test_pdf_defaults_off_and_pdf_only_ignores_unselected_library_settings(self):
        w=self.window
        self.assertFalse(w.pdf_box.isChecked())
        w.step_box.setChecked(False);w.pdf_box.setChecked(True)
        w.lib_append_box.setChecked(True)
        self.assertEqual(w.selected_formats(),('PDF',))
        self.assertFalse(w.export_options().append_mode)
        self.assertTrue(w.path_input.isEnabled())
        w.set_running(True)
        self.assertFalse(w.pdf_box.isEnabled())
        w.set_running(False)
        self.assertTrue(w.pdf_box.isEnabled())

    def test_pdf_preference_roundtrips_without_reenabling_step(self):
        w=self.window
        settings=self.root/'profile/LCSC3D/settings.json'
        with patch.object(main,'SETTINGS_PATH',settings),patch.object(main,'LEGACY_SETTINGS_PATH',self.root/'unused.json'):
            w.pdf_box.setChecked(True);w.step_box.setChecked(False);w.settings_enabled=True
            self.assertTrue(w.save_settings())
            self.assertTrue(json.loads(settings.read_text('utf-8'))['pdf'])
            w.pdf_box.setChecked(False);w._restore_settings()
            self.assertEqual(w.selected_formats(),('PDF',))

    def test_missing_pdf_notifies_once_and_keeps_other_part_successful(self):
        w=self.window
        w.step_box.setChecked(False);w.pdf_box.setChecked(True)
        def product(part):
            return dict(title='Model-'+part,manufacturer='Brand',datasheets=[] if part=='C20197' else ['https://example.com/data.pdf'])
        with patch.object(DatasheetBatch,'product',side_effect=product),patch.object(DatasheetBatch,'fetch',return_value=pdf_bytes()):
            w.start_button.click()
            deadline=time.monotonic()+5
            while w.batch_running and time.monotonic()<deadline:QTest.qWait(10)
        self.assertFalse(w.batch_running)
        self.assertEqual([w.results[p].status for p in w.ids],['成功','无数据手册'])
        self.assertTrue((self.root/'exports/PDF'/MISSING_REPORT).is_file())
        entries=[r for r in w.notifications.history.entries if r['title']=='PDF 数据手册缺失']
        self.assertEqual(len(entries),1)
        self.assertTrue(w.start_button.isEnabled())
