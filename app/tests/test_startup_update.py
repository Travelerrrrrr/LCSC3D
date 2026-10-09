"""Startup update checks notify only for a release and never delay shutdown."""
import sys
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock, patch

from test_settings import PreferencesTestCase
import main
from updater import Release
from PySide6.QtCore import QEvent, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication


class StartupUpdateTests(PreferencesTestCase):
    @classmethod
    def setUpClass(cls):
        if QApplication.instance() is None:
            QApplication.setAttribute(Qt.AA_ShareOpenGLContexts)
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        super().setUp()
        for method in ('_setup_preview', 'request_component_info', 'show_preview'):
            stub = patch.object(main.MainWindow, method)
            stub.start()
            self.addCleanup(stub.stop)
        self.window = main.MainWindow(settings_enabled=False)
        self.window.show()
        self.app.processEvents()
        self.threads = []
        self.release = Release('2.1.2', 'https://github.com/Travelerrrrrr/LCSC3D/releases/tag/v2.1.2', '', '', 100, '新版说明')

    def tearDown(self):
        self.window.close()
        for thread in self.threads:
            thread.join(timeout=3)
            self.assertFalse(thread.is_alive())
        self.window.deleteLater()
        self.app.sendPostedEvents(None, QEvent.DeferredDelete)

    def start(self):
        self.window.check_startup_update()
        self.threads.append(self.window.startup_update_check.thread)

    def wait_for(self, predicate):
        deadline = time.monotonic() + 3
        while not predicate() and time.monotonic() < deadline:
            QTest.qWait(10)
        self.assertTrue(predicate())

    def test_new_version_opens_ready_dialog_without_checking_twice_or_downloading(self):
        client = SimpleNamespace(check=Mock(return_value=self.release), download=Mock())
        with patch('update_ui.UpdateClient', return_value=client), patch.object(sys, 'frozen', True, create=True):
            self.start()
            self.wait_for(lambda: self.window.startup_update_outcome is not None)
        dialog = self.window.update_dialog
        self.assertTrue(dialog.isVisible())
        self.assertEqual(dialog.release, self.release)
        self.assertIn('2.1.2', dialog.status.text())
        self.assertTrue(dialog.install.isEnabled())
        self.assertIsNone(dialog.worker)
        client.check.assert_called_once_with(main.VERSION)
        client.download.assert_not_called()
        related = [row for row in self.log_entries() if row['event'] in
                   ('update.startup_check_started', 'update.startup_notification_shown')]
        self.assertEqual(len(related), 2)
        self.assertEqual(related[0]['root_id'], related[1]['root_id'])
        dialog.close()

    def test_latest_version_finishes_silently(self):
        status = self.window.run_status.text()
        with patch('update_ui.UpdateClient', return_value=SimpleNamespace(check=lambda version: None)), \
                patch('main.UpdateDialog') as dialog:
            self.start()
            self.wait_for(lambda: self.window.startup_update_outcome is not None)
        self.assertEqual(self.window.startup_update_outcome, 'latest')
        dialog.assert_not_called()
        self.assertEqual(self.window.run_status.text(), status)
        self.assertTrue(self.window.isEnabled())

    def test_network_failure_is_logged_without_creating_any_update_dialog(self):
        client = SimpleNamespace(check=Mock(side_effect=OSError('offline')))
        with patch('update_ui.UpdateClient', return_value=client), patch('main.UpdateDialog') as dialog:
            self.start()
            self.wait_for(lambda: self.window.startup_update_outcome is not None)
        self.assertEqual(self.window.startup_update_outcome, 'failed')
        dialog.assert_not_called()
        self.assertTrue(self.window.isVisible() and self.window.isEnabled())
        self.assertTrue(any(row['event'] == 'update.startup_check_failed' for row in self.log_entries()))

    def test_startup_check_runs_once_and_manual_check_still_reports_latest(self):
        client = SimpleNamespace(check=Mock(return_value=None))
        with patch('update_ui.UpdateClient', return_value=client):
            self.start()
            self.wait_for(lambda: self.window.startup_update_outcome is not None)
            self.window.check_startup_update()
            self.window.schedule_startup_update_check()
            self.assertFalse(self.window.startup_update_timer.isActive())
            self.assertEqual(client.check.call_count, 1)
            self.window.update_button.click()
            self.wait_for(lambda: self.window.update_dialog.worker is None)
        self.assertEqual(client.check.call_count, 2)
        self.assertIn('最新版本', self.window.update_dialog.status.text())
        self.window.update_dialog.close()

    def test_manual_check_cancels_pending_startup_notification(self):
        started, release = threading.Event(), threading.Event()
        def slow_check(version):
            started.set()
            release.wait(timeout=2)
            return self.release
        clients = [SimpleNamespace(check=slow_check), SimpleNamespace(check=lambda version: None)]
        try:
            with patch('update_ui.UpdateClient', side_effect=clients):
                self.start()
                self.assertTrue(started.wait(1))
                self.window.input.setPlainText('C2040')
                self.assertEqual(self.window.input.toPlainText(), 'C2040')
                self.window.update_button.click()
                dialog = self.window.update_dialog
                self.wait_for(lambda: dialog.worker is None)
                release.set()
                self.threads[0].join(timeout=2)
                QTest.qWait(100)
            self.assertIs(self.window.update_dialog, dialog)
            self.assertIsNone(dialog.release)
            self.assertIn('最新版本', dialog.status.text())
            dialog.close()
        finally:
            release.set()

    def test_closing_before_scheduled_check_cancels_the_request(self):
        with patch('update_ui.UpdateClient') as client:
            self.window.schedule_startup_update_check()
            self.assertTrue(self.window.startup_update_timer.isActive())
            self.window.close()
            self.assertFalse(self.window.startup_update_timer.isActive())
            self.window.check_startup_update()
        client.assert_not_called()
        self.assertFalse(self.window.isVisible())

    def test_closing_during_network_request_does_not_wait_or_show_late_popup(self):
        started, release = threading.Event(), threading.Event()
        def slow_check(version):
            started.set()
            release.wait(timeout=2)
            return self.release
        try:
            with patch('update_ui.UpdateClient', return_value=SimpleNamespace(check=slow_check)), \
                    patch('main.UpdateDialog') as dialog:
                self.start()
                self.assertTrue(started.wait(1))
                began = time.monotonic()
                self.window.close()
                self.assertLess(time.monotonic() - began, .5)
                self.assertFalse(self.window.isVisible())
                self.assertTrue(self.threads[0].is_alive() and self.threads[0].daemon)
                release.set()
                self.threads[0].join(timeout=2)
                QTest.qWait(100)
            dialog.assert_not_called()
        finally:
            release.set()
