"""Diagnostic bundles must be useful, private, bounded and safe to clear live."""
import faulthandler
import json
import stat
from types import SimpleNamespace
import zipfile
from unittest.mock import patch

from test_settings import PreferencesTestCase
import app_logging as logs
import log_support
from app_paths import data_directory
from app_settings import Preferences, set_preferences


class LogSupportTests(PreferencesTestCase):
    def bundle(self):
        result = log_support.package_logs()
        self.assertTrue(result['path'].is_relative_to(data_directory() / 'diagnostics'))
        with zipfile.ZipFile(result['path']) as archive:
            self.assertIsNone(archive.testzip())
            contents = {name: archive.read(name) for name in archive.namelist()}
        return result, contents, json.loads(contents['diagnostics.json'])

    def test_bundle_collects_all_log_types_and_only_safe_settings_and_markers(self):
        secret = 'PRIVATE-session-proxy-download-folder'
        set_preferences(Preferences(store_proxy='direct', update_proxy='system'))
        logs.log_event('ERROR', 'test.failure', password=secret)
        expected = ['LCSC3D.log.1', 'LCSC3D.log.3', 'LCSC3D-update.log',
                    'LCSC3D-update.log.2', 'LCSC3D-crash-1234.log']
        for name in expected:
            (self.log_dir / name).write_text('diagnostic line\n', encoding='utf-8')
        (self.log_dir / 'run-5678.json').write_text(json.dumps(
            {'pid': 5678, 'session_id': 'a' * 32, 'password': secret}), encoding='utf-8')
        for directory in (data_directory(), self.log_dir):
            for name in ('store-session.bin', 'LCSC3D-settings.json', 'private.txt', 'model.obj', 'old.zip'):
                (directory / name).write_text(secret, encoding='utf-8')
        nested = self.log_dir / 'nested'
        nested.mkdir()
        (nested / 'LCSC3D.log').write_text(secret, encoding='utf-8')
        result, contents, metadata = self.bundle()
        self.assertFalse(result['issues'])
        self.assertEqual(result['log_count'], len(expected) + 1)
        self.assertTrue(all('logs/' + name in contents for name in expected))
        self.assertNotIn(secret.encode(), b'\n'.join(contents.values()))
        self.assertEqual(metadata['preferences'], Preferences(store_proxy='direct').to_mapping())
        self.assertEqual(metadata['application']['log_level'], 'DEBUG')
        self.assertTrue(metadata['application']['session_id'])
        self.assertTrue(metadata['environment']['qt'])
        self.assertEqual(json.loads(contents['logs/run-5678.json']), {'pid': 5678, 'session_id': 'a' * 32})

    def test_bundle_takes_snapshot_and_preserves_recent_lines_when_bounded(self):
        self.log_dir.joinpath('LCSC3D-update.log').write_bytes(b'old\n' * 100 + b'newest\n')
        with patch.object(log_support, 'MAX_FILE_BYTES', 64), patch.object(log_support, 'MAX_BUNDLE_BYTES', 128):
            result, contents, metadata = self.bundle()
        self.assertTrue(any(item['reason'] == 'truncated' for item in result['issues']))
        self.assertLessEqual(sum(item['bytes'] for item in metadata['files']), 128)
        self.assertTrue(contents['logs/LCSC3D-update.log'].endswith(b'newest\n'))

    def test_bundles_have_unique_names_and_do_not_include_previous_bundles(self):
        first, _, _ = self.bundle()
        second, contents, _ = self.bundle()
        self.assertNotEqual(first['path'], second['path'])
        self.assertTrue(first['path'].exists())
        self.assertFalse(any(name.endswith('.zip') for name in contents))

    def test_corrupt_marker_is_reported_without_blocking_other_logs(self):
        (self.log_dir / 'run-123.json').write_text('PRIVATE-invalid-json', encoding='utf-8')
        result, contents, _ = self.bundle()
        self.assertTrue(any(item['file'] == 'run-123.json' for item in result['issues']))
        self.assertNotIn('logs/run-123.json', contents)
        self.assertNotIn(b'PRIVATE', b'\n'.join(contents.values()))
        self.assertIn('logs/LCSC3D.log', contents)

    def test_unreadable_file_is_reported_and_other_files_are_still_packaged(self):
        path = self.log_dir / 'LCSC3D-update.log'
        path.write_text('unavailable', encoding='utf-8')
        original = type(path).open
        def open_file(candidate, *args, **kwargs):
            if candidate == path:
                raise PermissionError('PRIVATE-path')
            return original(candidate, *args, **kwargs)
        with patch.object(type(path), 'open', open_file):
            result, contents, _ = self.bundle()
        self.assertIn({'file': path.name, 'reason': 'PermissionError'}, result['issues'])
        self.assertIn('logs/LCSC3D.log', contents)
        self.assertNotIn(b'PRIVATE', b'\n'.join(contents.values()))

    def test_failed_zip_write_removes_partial_archive_and_keeps_logs(self):
        logs.log_event('ERROR', 'test.keep_on_package_failure')
        with patch('log_support.zipfile.ZipFile.writestr', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                log_support.package_logs()
        self.assertEqual(list((data_directory() / 'diagnostics').iterdir()), [])
        self.assertIn('test.keep_on_package_failure', (self.log_dir / 'LCSC3D.log').read_text(encoding='utf-8'))
        self.assertTrue(logs.log_event('ERROR', 'test.still_writable'))

    def test_missing_logs_still_produce_environment_diagnostics(self):
        logs.set_log_level('CRITICAL')
        logs.close_logging()
        (self.log_dir / 'LCSC3D.log').unlink()
        self.log_dir.rmdir()
        result, contents, metadata = self.bundle()
        self.assertEqual(result['log_count'], 0)
        self.assertEqual(metadata['files'], [])
        self.assertIn('反馈说明.txt', contents)

    def test_linked_files_are_neither_collected_nor_cleared(self):
        path = self.log_dir / 'LCSC3D-update.log'
        path.write_text('PRIVATE-link-target', encoding='utf-8')
        original = type(path).lstat
        def linked(candidate, *args, **kwargs):
            if candidate == path:
                return SimpleNamespace(st_mode=stat.S_IFREG, st_file_attributes=0x400)
            return original(candidate, *args, **kwargs)
        with patch.object(type(path), 'lstat', linked):
            result, contents, _ = self.bundle()
            cleared = log_support.clear_logs()
        self.assertNotIn('logs/LCSC3D-update.log', contents)
        self.assertTrue(result['issues'])
        self.assertTrue(cleared['failures'])
        self.assertEqual(path.read_text(encoding='utf-8'), 'PRIVATE-link-target')

    def test_clear_removes_historical_logs_keeps_bundle_and_resumes_current_logging(self):
        logs.log_event('ERROR', 'test.before_clear')
        for name in ('LCSC3D.log.1', 'LCSC3D-update.log', 'LCSC3D-crash-123.log'):
            (self.log_dir / name).write_text('old log', encoding='utf-8')
        private = self.log_dir / 'store-session.bin'
        private.write_bytes(b'PRIVATE-session')
        result, _, _ = self.bundle()
        cleared = log_support.clear_logs()
        self.assertFalse(cleared['failures'])
        self.assertEqual(len(cleared['cleared']), 4)
        self.assertTrue(result['path'].exists())
        self.assertEqual(private.read_bytes(), b'PRIVATE-session')
        self.assertTrue(logs.log_event('ERROR', 'test.after_clear'))
        events = [row['event'] for row in self.log_entries()]
        self.assertNotIn('test.before_clear', events)
        self.assertIn('test.after_clear', events)
        self.assertFalse((self.log_dir / 'LCSC3D.log.1').exists())

    def test_clear_preserves_current_crash_handle_and_run_marker(self):
        logs.start_crash_capture()
        self.addCleanup(logs.stop_crash_capture)
        crash_path = type(self.log_dir)(logs._crash_stream.name)
        marker = logs._run_marker
        logs._crash_stream.write(b'OLD CRASH DATA\n' * 20)
        logs._crash_stream.flush()
        result = log_support.clear_logs()
        self.assertFalse(result['failures'])
        self.assertTrue(marker.exists())
        self.assertEqual(crash_path.read_bytes(), b'')
        self.assertTrue(faulthandler.is_enabled())
        faulthandler.dump_traceback(file=logs._crash_stream)
        data = crash_path.read_bytes()
        self.assertIn(b'test_clear_preserves_current_crash_handle', data)
        self.assertNotIn(b'\x00', data)
        self.assertNotIn(b'OLD CRASH DATA', data)

    def test_clear_reports_in_use_files_and_preserves_other_sessions(self):
        locked = self.log_dir / 'LCSC3D-update.log'
        locked.write_text('keep', encoding='utf-8')
        crash = self.log_dir / 'LCSC3D-crash-999.log'
        crash.write_text('other session', encoding='utf-8')
        marker = self.log_dir / 'run-999.json'
        marker.write_text('{}', encoding='utf-8')
        original = type(locked).unlink
        def unlink_file(candidate, *args, **kwargs):
            if candidate == locked:
                raise PermissionError('in use')
            return original(candidate, *args, **kwargs)
        with patch.object(type(locked), 'unlink', unlink_file):
            result = log_support.clear_logs()
        self.assertEqual({item['file'] for item in result['failures']}, {locked.name, crash.name})
        self.assertTrue(locked.exists() and crash.exists() and marker.exists())
        self.assertTrue(logs.log_event('ERROR', 'test.write_after_partial_clear'))
