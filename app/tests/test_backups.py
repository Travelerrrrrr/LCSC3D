"""Backup round trips, conflict protection, cleanup boundaries and settings UI."""
from dataclasses import replace
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import backend
from altium import MergedLibrary
from altium_project import commit_user_file
from app_settings import Preferences, get_preferences, set_preferences
from app_logging import close_logging
from backups import (backup_directory, create_backup, list_backups, prepare_restore,
                     restore_backup, clear_backups)
from errors import DownloadError, Cancelled


class BackupTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name).resolve()
        env = patch.dict(os.environ, LOCALAPPDATA=str(self.root / 'profile'))
        env.start()
        self.addCleanup(env.stop)
        self.addCleanup(close_logging)
        previous = get_preferences()
        self.addCleanup(set_preferences, previous)
        set_preferences(Preferences())
        self.target = self.root / '中文工程.PrjPcb'
        self.original = b'[Design]\r\nName=old\r\n'
        self.changed = b'[Design]\r\nName=new\r\n'
        self.target.write_bytes(self.original)

    def make_backup(self):
        path = commit_user_file(self.target, self.original, self.changed)
        self.assertEqual(path.read_bytes(), self.original)
        return next(row for row in list_backups() if row.path == path)

    def test_defaults_are_light_brand_blue_and_automatic_backup_is_a_validated_boolean(self):
        self.assertEqual(Preferences().theme_mode, 'light')
        self.assertEqual(Preferences().accent_color, '#087af5')
        for invalid in ('false', 0, 1, None, [], {}):
            self.assertTrue(Preferences.from_mapping({'auto_backup': invalid}).auto_backup)
        self.assertFalse(Preferences.from_mapping({'auto_backup': False}).auto_backup)

    def test_backup_records_original_path_hash_time_and_actual_storage(self):
        row = self.make_backup()
        self.assertEqual(row.original_path, str(self.target))
        self.assertEqual(row.size, len(self.original))
        self.assertGreater(row.stored_size, row.size)
        self.assertEqual(row.stored_size, sum(p.stat().st_size for p in row.path.parent.iterdir()))
        self.assertEqual(self.target.read_bytes(), self.changed)
        self.assertFalse(list(self.root.glob('*.bak')))

    def test_disabling_backups_keeps_optimistic_concurrency_protection(self):
        set_preferences(replace(get_preferences(), auto_backup=False))
        self.assertIsNone(commit_user_file(self.target, self.original, self.changed))
        self.assertEqual(list_backups(), [])
        with self.assertRaises(DownloadError):
            commit_user_file(self.target, self.original, b'stale change')
        self.assertEqual(self.target.read_bytes(), self.changed)

    def test_restoration_can_be_undone_even_when_automatic_backups_are_disabled(self):
        row = self.make_backup()
        set_preferences(replace(get_preferences(), auto_backup=False))
        result = restore_backup(prepare_restore(row.id, self.target))
        self.assertEqual(self.target.read_bytes(), self.original)
        safety = next(item for item in list_backups() if str(item.path) == result['safety_backup'])
        self.assertEqual(safety.path.read_bytes(), self.changed)
        restore_backup(prepare_restore(safety.id, self.target))
        self.assertEqual(self.target.read_bytes(), self.changed)

    def test_restore_to_new_path_and_identical_restore_do_not_change_other_files(self):
        row = self.make_backup()
        destination = self.root / 'new' / 'Recovered.PrjPcb'
        plan = prepare_restore(row.id, destination)
        self.assertIsNone(plan.target_sha256)
        self.assertFalse(destination.exists())
        restore_backup(plan)
        self.assertEqual(destination.read_bytes(), self.original)
        self.assertEqual(self.target.read_bytes(), self.changed)
        before = len(list_backups())
        self.assertFalse(restore_backup(prepare_restore(row.id, destination))['changed'])
        self.assertEqual(len(list_backups()), before)

    def test_corrupt_backup_or_changed_target_never_overwrites_current_contents(self):
        row = self.make_backup()
        plan = prepare_restore(row.id, self.target)
        self.target.write_bytes(b'[Design]\nUserEdit=1\n')
        with self.assertRaises(DownloadError):
            restore_backup(plan)
        self.assertIn(b'UserEdit', self.target.read_bytes())
        row.path.write_bytes(bytes([row.path.read_bytes()[0] ^ 1]) + self.original[1:])
        with self.assertRaises(DownloadError):
            prepare_restore(row.id, self.target)
        self.assertIn(b'UserEdit', self.target.read_bytes())

    def test_restore_checks_missing_target_conflicts_and_rejects_wrong_extensions(self):
        row = self.make_backup()
        destination = self.root / 'Recovered.PrjPcb'
        plan = prepare_restore(row.id, destination)
        destination.write_bytes(b'[Design]\ncreated after confirmation')
        with self.assertRaises(DownloadError):
            restore_backup(plan)
        for target in ('relative.PrjPcb', str(self.root / 'other.txt'), str(row.path)):
            with self.assertRaises(DownloadError):
                prepare_restore(row.id, target)

    def test_cannot_write_backup_prevents_modification_and_incomplete_backup_is_removed(self):
        original_write = backend.atomic_write
        def fail_metadata(path, payload):
            if path.name == 'metadata.json':
                raise PermissionError('locked')
            original_write(path, payload)
        with patch('backend.atomic_write', side_effect=fail_metadata), self.assertRaises(PermissionError):
            commit_user_file(self.target, self.original, self.changed)
        self.assertEqual(self.target.read_bytes(), self.original)
        self.assertEqual(list(backup_directory().iterdir()), [])

    def test_failed_restore_preserves_target_and_safety_backup(self):
        row = self.make_backup()
        plan = prepare_restore(row.id, self.target)
        original_write = backend.atomic_write
        def fail_target(path, payload):
            if path == self.target:
                raise PermissionError('locked')
            original_write(path, payload)
        with patch('backend.atomic_write', side_effect=fail_target), self.assertRaises(PermissionError):
            restore_backup(plan)
        self.assertEqual(self.target.read_bytes(), self.changed)
        self.assertEqual(len(list_backups()), 2)

    def test_clear_is_limited_to_confirmed_backups_and_partial_failure_is_retryable(self):
        first = self.make_backup()
        create_backup(self.target, self.changed)
        extra = backup_directory() / 'keep.txt'
        extra.write_text('not managed')
        unlink = Path.unlink
        def fail_data(path, *args, **kwargs):
            if path == first.path:
                raise PermissionError('locked')
            return unlink(path, *args, **kwargs)
        with patch.object(Path, 'unlink', fail_data):
            result = clear_backups([first.id])
        self.assertEqual(result['failed'], [first.id])
        self.assertEqual(len(list_backups()), 2)
        self.assertEqual(clear_backups([first.id])['cleared'], [first.id])
        self.assertEqual(len(list_backups()), 1)
        self.assertTrue(extra.exists())
        self.assertEqual(self.target.read_bytes(), self.changed)
        clear_backups([row.id for row in list_backups()])
        commit_user_file(self.target, self.changed, self.original)
        self.assertEqual(len(list_backups()), 1)

    def test_cancelled_commit_never_replaces_the_original(self):
        def cancelled():
            raise Cancelled()
        with self.assertRaises(Cancelled):
            commit_user_file(self.target, self.original, self.changed, cancelled)
        self.assertEqual(self.target.read_bytes(), self.original)
        self.assertEqual(list_backups(), [])

    def test_legacy_libraries_and_projects_restore_to_explicit_destinations(self):
        data = json.loads((Path(__file__).parent / 'fixtures/C2040.json').read_text('utf-8'))
        root = backup_directory()
        root.mkdir(parents=True)
        for suffix in ('SchLib', 'PcbLib', 'PrjPcb'):
            if suffix == 'PrjPcb':
                payload = self.original
            else:
                lib = MergedLibrary(suffix.upper(), merge_pcb=True)
                lib.add(data, 'C2040', lambda: None)
                payload = lib.build(['C2040'], lambda: None)
            path = root / (uuid.uuid4().hex + '-Original.' + suffix)
            path.write_bytes(payload)
            row = next(row for row in list_backups() if row.path == path)
            self.assertTrue(row.legacy)
            self.assertFalse(row.original_path)
            target = self.root / ('Recovered.' + suffix)
            restore_backup(prepare_restore(row.id, target))
            self.assertEqual(target.read_bytes(), payload)

    def test_broken_metadata_can_be_cleared_but_not_restored(self):
        row = self.make_backup()
        row.path.with_name('metadata.json').write_text('{invalid')
        self.assertTrue(list_backups()[0].error)
        with self.assertRaises(DownloadError):
            prepare_restore(row.id, self.target)
        self.assertEqual(clear_backups([row.id])['cleared'], [row.id])


class BackupUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import QApplication
        if QApplication.instance() is None:
            QApplication.setAttribute(Qt.AA_ShareOpenGLContexts)
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        import main
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name).resolve()
        env = patch.dict(os.environ, LOCALAPPDATA=str(self.root / 'profile'))
        env.start()
        self.addCleanup(env.stop)
        self.addCleanup(close_logging)
        for method in ('_setup_preview', 'request_component_info', 'show_preview'):
            stub = patch.object(main.MainWindow, method)
            stub.start()
            self.addCleanup(stub.stop)
        self.addCleanup(set_preferences, get_preferences())
        self.window = main.MainWindow(settings_enabled=False)
        self.window.show()
        self.window.open_settings()
        self.settings = self.window.settings_dialog
        self.panel = self.settings.backup_panel
        self.settings.section_buttons[4].click()
        self.app.processEvents()
        self.addCleanup(self.close_window)

    def close_window(self):
        from PySide6.QtCore import QEvent
        self.wait()
        self.window.close()
        self.window.deleteLater()
        self.app.sendPostedEvents(None, QEvent.DeferredDelete)

    def wait(self):
        deadline = time.monotonic() + 5
        while self.settings.worker is not None and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(.01)
        self.app.processEvents()
        self.assertIsNone(self.settings.worker)

    def test_switch_is_saved_and_cancel_keeps_preference(self):
        self.panel.auto_box.setChecked(False)
        self.settings.cancel_button.click()
        self.assertTrue(self.window.preferences.auto_backup)
        self.window.open_settings()
        self.settings = self.window.settings_dialog
        self.settings.backup_panel.auto_box.setChecked(False)
        self.settings.save_button.click()
        self.assertFalse(self.window.preferences.auto_backup)

    def test_restore_and_clear_require_confirmation_and_refresh_space(self):
        target = self.root / 'Example.PrjPcb'
        old, new = b'[Design]\nOld=1', b'[Design]\nNew=2'
        target.write_bytes(old)
        commit_user_file(target, old, new)
        self.panel.refresh()
        self.assertIn('1 ', self.panel.usage.text())
        self.panel.restore_button.click()
        page = self.panel.restore_page
        page.restore_button.click()
        self.wait()
        page.confirmation.action_buttons[0].click()
        self.assertEqual(target.read_bytes(), new)
        page.restore_button.click()
        self.wait()
        page.confirmation.action_buttons[1].click()
        self.wait()
        self.assertEqual(target.read_bytes(), old)
        self.assertEqual(len(self.panel.rows), 2)
        self.panel.clear_button.click()
        self.panel.confirmation.action_buttons[0].click()
        self.assertEqual(len(list_backups()), 2)
        self.panel.clear_button.click()
        self.panel.confirmation.action_buttons[1].click()
        self.wait()
        self.assertEqual(self.panel.rows, [])
        self.assertIn('0 B', self.panel.usage.text())
        self.assertEqual(target.read_bytes(), old)

    def test_backup_actions_are_disabled_during_export(self):
        target = self.root / 'Example.PrjPcb'
        target.write_bytes(b'[Design]')
        create_backup(target, b'[Design]')
        self.panel.refresh()
        self.window.set_running(True)
        self.assertFalse(self.panel.restore_button.isEnabled())
        self.assertFalse(self.panel.clear_button.isEnabled())
        self.window.set_running(False)
        self.assertTrue(self.panel.restore_button.isEnabled())


if __name__ == '__main__':
    unittest.main()
