"""Notice order, permanent history, single-window navigation and safe diagnostics."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from PySide6.QtCore import Qt, QEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from lcsc3d.ui import main
from lcsc3d.core import app_logging as logs
from lcsc3d.core.app_settings import Preferences
from lcsc3d.services.notification_history import NotificationHistory
from lcsc3d.ui.shell_ui import notify


class NotificationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if QApplication.instance() is None:
            QApplication.setAttribute(Qt.AA_ShareOpenGLContexts)
        cls.app=QApplication.instance() or QApplication([])

    def setUp(self):
        directory=tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.folder=Path(directory.name)
        env=patch.dict(os.environ,LOCALAPPDATA=directory.name)
        env.start();self.addCleanup(env.stop)
        logs.configure_logging(version=main.VERSION,directory=self.folder/'LCSC3D/logs')
        self.addCleanup(logs.close_logging)
        with patch.object(main.MainWindow,'_setup_preview'), patch.object(main.MainWindow,'request_component_info'), \
                patch.object(main.MainWindow,'show_preview'):
            self.window=main.MainWindow(settings_enabled=False)
        self.window.show();self.app.processEvents()
        self.addCleanup(self.close)

    def close(self):
        self.window.close();self.window.deleteLater()
        self.app.sendPostedEvents(None,QEvent.DeferredDelete)

    def entries(self):
        return [json.loads(line) for line in (self.folder/'LCSC3D/logs/LCSC3D.log').read_text('utf-8').splitlines()]

    def test_newest_notice_stays_top_right_and_older_notices_slide_down(self):
        first=notify(self.window,'First','one',persistent=True)
        QTest.qWait(280)
        initial=first.y()
        second=notify(self.window,'Second','two',persistent=True)
        QTest.qWait(80)
        self.assertGreater(first.y(),initial)
        third=notify(self.window,'Third','three',persistent=True)
        QTest.qWait(280)
        self.assertEqual(self.window.notifications.toasts,[third,second,first])
        self.assertLess(third.y(),second.y());self.assertLess(second.y(),first.y())
        for toast in (first,second,third):
            self.assertEqual(toast.geometry().right(),toast.parentWidget().width()-17)
            self.assertGreaterEqual(toast.y(),self.window.title_bar.geometry().bottom())
        self.window.resize(1060,740);QTest.qWait(60)
        for toast in self.window.notifications.toasts:
            self.assertEqual(toast.geometry().right(),toast.parentWidget().width()-17)
            self.assertLess(toast.geometry().bottom(),toast.parentWidget().height())

    def test_overflow_and_timeout_dismiss_notices_without_losing_their_records(self):
        for i in range(4):notify(self.window,f'Title {i}',f'Message {i}',persistent=True)
        self.assertEqual(len(self.window.notifications.toasts),3)
        history=self.window.notifications.history
        self.assertEqual([e['title'] for e in history.entries],['Title 3','Title 2','Title 1','Title 0'])
        latest=self.window.notifications.toasts[0]
        latest.timer.start(20);QTest.qWait(50)
        self.assertEqual(len(self.window.notifications.toasts),2)
        self.assertEqual(len(history.entries),4)
        reopened=NotificationHistory()
        self.assertEqual(reopened.entries,history.entries)
        self.assertTrue(history.path.is_relative_to(self.folder/'LCSC3D/messages'))

    def test_messages_page_navigation_filters_copy_and_live_updates(self):
        notify(self.window,'Warning','C2765186 summary',severity='warning',details='Type-C full details')
        notify(self.window,'Success','Other part',severity='success')
        self.window.messages_button.click()
        page=self.window.messages_page
        self.assertIs(self.window._page_host.current_page(),page)
        self.assertTrue(self.window.messages_button.isChecked())
        self.assertFalse(self.window.market_button.isChecked())
        self.assertEqual(page.table.rowCount(),2)
        page.level.setCurrentIndex(page.level.findData('warning'))
        page.search.setText('type-c')
        self.assertEqual(page.table.rowCount(),1)
        page.copy_button.click()
        self.assertIn('Type-C full details',QApplication.clipboard().text())
        notify(self.window,'New warning','type-c latest',severity='warning')
        self.assertEqual(page.table.rowCount(),2)
        page.search.clear();page.level.setCurrentIndex(0)
        self.window.apply_preferences(Preferences(language='en_US'))
        self.assertEqual(self.window.messages_button.text(),'Messages')
        self.assertEqual(page.windowTitle(),'Messages')
        self.assertEqual(page.table.rowCount(),3)
        self.assertEqual([w for w in self.app.topLevelWidgets() if w.isVisible()],[self.window])
        self.window.back_button.click()
        self.assertIs(self.window._page_host.current_page(),self.window.content_scroll)
        self.window.messages_button.click()
        self.assertIs(self.window.messages_page,page)
        page.clear_button.click()
        self.assertFalse(page.history.entries)
        self.assertEqual(page.table.rowCount(),0)
        self.assertFalse(page.copy_button.isEnabled())
        self.assertEqual(NotificationHistory().entries,[])

    def test_history_count_and_bytes_are_bounded_and_corrupt_storage_is_reported(self):
        history=self.window.notifications.history
        with patch('lcsc3d.services.notification_history.MAX_MESSAGES',3):
            for i in range(5):history.add(str(i),'text','info')
        self.assertEqual([e['title'] for e in history.entries],['4','3','2'])
        with patch('lcsc3d.services.notification_history.MAX_HISTORY_BYTES',2048):
            for i in range(5):history.add(str(i),'中'*500,'info')
            self.assertLessEqual(history.path.stat().st_size,2048)
        history.path.write_text('invalid private data','utf-8')
        broken=NotificationHistory()
        self.assertTrue(broken.save_failed)
        self.assertEqual(broken.entries,[])
        self.assertIn('notification.history_load_failed',[e['event'] for e in self.entries()])

    def test_failed_atomic_history_write_keeps_old_file_and_notices_still_work(self):
        history=self.window.notifications.history
        history.add('Old','saved','info')
        before=history.path.read_bytes()
        with patch('lcsc3d.services.notification_history.QSaveFile.commit',return_value=False):
            toast=notify(self.window,'New','unsaved',persistent=True)
        self.assertTrue(toast.isVisible())
        self.assertTrue(history.save_failed)
        self.assertEqual(history.path.read_bytes(),before)
        self.assertEqual(history.entries[0]['title'],'New')
        self.assertTrue(logs.log_event('ERROR','test.logging_after_history_failure'))
        self.assertIn('notification.history_save_failed',[e['event'] for e in self.entries()])

    def test_action_failure_is_logged_without_message_content_and_notices_remain_available(self):
        secret='PRIVATE-notice-text-token'
        def fail():raise ValueError(secret)
        toast=notify(self.window,'Warning',secret,actions=(('Run',fail),))
        toast.action_buttons[0].click()
        events=self.entries()
        self.assertIn('notification.action_failed',[e['event'] for e in events])
        self.assertIn('notification.created',[e['event'] for e in events])
        self.assertNotIn(secret,json.dumps(events))
        self.assertEqual(self.window.notifications.history.entries[0]['title'],'操作失败')
        self.assertTrue(logs.log_event('INFO','test.logging_after_notification'))
