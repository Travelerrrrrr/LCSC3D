"""Saved settings control real request routing and log output independently."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import logging
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import main
import backend
import updater
import app_logging
from app_settings import Preferences, get_preferences, set_preferences, proxy_for_url, write_settings, read_settings
from app_paths import data_directory, temporary_directory, replace_file
from store_session import SessionVault
from store import MemorySession
from PySide6.QtCore import QEvent, Qt
from PySide6.QtWidgets import QApplication, QMessageBox


class PreferencesTestCase(unittest.TestCase):
    def setUp(self):
        original = get_preferences()
        self.addCleanup(set_preferences, original)
        level = app_logging.LOGGER.level
        self.addCleanup(app_logging.set_log_level, logging.getLevelName(level))
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        environment = patch.dict(os.environ, {'LOCALAPPDATA': str(self.folder)})
        environment.start()
        self.addCleanup(environment.stop)
        set_preferences(Preferences())
        self.log_dir = self.folder / 'LCSC3D' / 'logs'
        app_logging.configure_logging(version=main.VERSION, directory=self.log_dir)
        self.addCleanup(app_logging.close_logging)

    def log_entries(self):
        return [json.loads(line) for line in (self.log_dir / 'LCSC3D.log').read_text(encoding='utf-8').splitlines()]


class ProxyRoutingTests(PreferencesTestCase):
    def server(self, kind):
        requests = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                requests.append({'path': self.path, 'cookie': self.headers.get('Cookie', '')})
                data = json.dumps({'route': kind}).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(data)))
                self.send_header('Set-Cookie', 'synthetic_session=1; Path=/; HttpOnly')
                self.end_headers()
                self.wfile.write(data)

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
        thread.start()

        def close():
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        self.addCleanup(close)
        return f'http://127.0.0.1:{server.server_port}', requests

    def test_store_session_changes_route_immediately_and_retains_cookies(self):
        origin, origin_requests = self.server('direct')
        proxy, proxy_requests = self.server('proxy')
        with patch('urllib.request.getproxies', return_value={'http': proxy}), \
                patch('urllib.request.proxy_bypass', return_value=False):
            session = MemorySession()
            body, _ = session.request(origin + '/first')
            self.assertEqual(json.loads(body)['route'], 'proxy')
            cookies = session.cookies
            set_preferences(Preferences(store_proxy='direct'))
            body, _ = session.request(origin + '/second')
            self.assertEqual(json.loads(body)['route'], 'direct')
            self.assertIs(session.cookies, cookies)
            set_preferences(Preferences(store_proxy='system'))
            body, _ = session.request(origin + '/third')
            self.assertEqual(json.loads(body)['route'], 'proxy')
        self.assertEqual(len(origin_requests), 1)
        self.assertEqual(len(proxy_requests), 2)
        self.assertIn('synthetic_session=1', origin_requests[0]['cookie'])
        self.assertIn('synthetic_session=1', proxy_requests[-1]['cookie'])

    def test_direct_store_requests_ignore_environment_and_system_proxy_settings(self):
        origin, origin_requests = self.server('direct')
        set_preferences(Preferences(store_proxy='direct', update_proxy='system'))
        with patch.dict(os.environ, {'HTTP_PROXY': 'http://127.0.0.1:1', 'HTTPS_PROXY': 'http://127.0.0.1:1'}), \
                patch('urllib.request.getproxies', side_effect=AssertionError('System proxy should not be read')):
            body, _ = MemorySession().request(origin + '/direct')
            self.assertEqual(json.loads(body)['route'], 'direct')
        self.assertEqual(len(origin_requests), 1)

    def test_resource_and_update_clients_follow_independent_live_preferences(self):
        proxy = 'http://127.0.0.1:12345'

        class Response(io.BytesIO):
            status = 200
            headers = {}

            def read(self, size=-1, **kwargs):
                return super().read(size)

            def release_conn(self):
                pass

        api = backend.NetworkApi()
        update = updater.UpdateClient()
        routes = []

        def pool(route):
            routes.append(route)
            result = MagicMock()
            result.request.side_effect = lambda *args, **kwargs: Response(b'public resource')
            return result

        with patch('urllib.request.getproxies', return_value={'https': proxy}), \
                patch('urllib.request.proxy_bypass', return_value=False), \
                patch('backend.connection_pool', side_effect=pool), \
                patch('updater.connection_pool', side_effect=pool):
            set_preferences(Preferences(store_proxy='direct', update_proxy='system'))
            self.assertEqual(api.fetch('https://lceda.cn/api/public'), b'public resource')
            update._open(updater.LATEST_API).close()
            set_preferences(Preferences(store_proxy='system', update_proxy='direct'))
            self.assertEqual(api.fetch('https://lceda.cn/api/public'), b'public resource')
            update._open(updater.LATEST_API).close()
        self.assertEqual(routes, [None, proxy, proxy, None])

    def test_system_proxy_bypass_is_honored(self):
        with patch('urllib.request.proxy_bypass', return_value=True), \
                patch('urllib.request.getproxies') as read:
            self.assertIsNone(proxy_for_url('https://passport.jlc.com/api/public', 'store'))
            self.assertIsNone(proxy_for_url(updater.LATEST_API, 'update'))
        read.assert_not_called()

    def test_request_logs_hide_query_strings_and_proxy_credentials(self):
        origin, _ = self.server('direct')
        proxy, _ = self.server('proxy')
        private = 'PRIVATE-auth-cookie-password'
        proxy = proxy.replace('http://', 'http://user:' + private + '@')
        with patch('urllib.request.getproxies', return_value={'http': proxy}), \
                patch('urllib.request.proxy_bypass', return_value=False):
            MemorySession().request(origin + '/login?code=' + private)
        contents = (self.log_dir / 'LCSC3D.log').read_text(encoding='utf-8')
        self.assertNotIn(private, contents)
        self.assertNotIn(origin, contents)
        self.assertNotIn(proxy, contents)
        self.assertTrue(any(entry['event'] == 'store.request_completed' for entry in self.log_entries()))


class LogOutputTests(PreferencesTestCase):
    def test_debug_is_the_default_and_levels_filter_real_output(self):
        levels = ('DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL')
        self.assertEqual(Preferences().log_level, 'DEBUG')
        for selected in levels:
            app_logging.set_log_level(selected)
            for level in levels:
                app_logging.log_event(level, 'test.level', selected=selected)
        entries = self.log_entries()
        for selected in levels:
            actual = [entry['level'] for entry in entries if entry['selected'] == selected]
            self.assertEqual(actual, list(levels[levels.index(selected):]))
        self.assertTrue(all(entry['version'] == main.VERSION for entry in entries))

    def test_third_party_debug_messages_are_excluded(self):
        third_party = logging.getLogger('urllib3.connectionpool')
        previous = third_party.level
        self.addCleanup(third_party.setLevel, previous)
        third_party.setLevel(logging.DEBUG)
        third_party.debug('PRIVATE-Cookie-Authorization')
        app_logging.log_event('DEBUG', 'test.public_event')
        contents = (self.log_dir / 'LCSC3D.log').read_text(encoding='utf-8')
        self.assertNotIn('PRIVATE', contents)
        self.assertIn('test.public_event', contents)

    def test_rotation_limits_backups_and_retains_recent_entries(self):
        with patch('app_logging.MAX_LOG_BYTES', 500), patch('app_logging.LOG_BACKUPS', 2):
            app_logging.configure_logging(directory=self.log_dir)
            for index in range(30):
                app_logging.log_event('DEBUG', 'test.rotation', index=index)
        files = list(self.log_dir.glob('LCSC3D.log*'))
        self.assertLessEqual(len(files), 3)
        self.assertTrue(all(path.stat().st_size <= 500 for path in files))
        self.assertEqual(self.log_entries()[-1]['index'], 29)


class SettingsPersistenceTests(PreferencesTestCase):
    def test_existing_and_invalid_preferences_get_compatible_defaults(self):
        self.assertEqual(Preferences.from_mapping({'destination': 'existing'}), Preferences())
        invalid = {'store_proxy': {'invalid': 1}, 'update_proxy': [], 'log_level': False}
        self.assertEqual(Preferences.from_mapping(invalid), Preferences())

    def test_interrupted_save_preserves_previous_file_and_removes_temporary_file(self):
        path = self.folder / 'LCSC3D-settings.json'
        path.write_text('{"destination":"existing"}', encoding='utf-8')
        previous = path.read_bytes()
        with patch('app_settings.os.replace', side_effect=PermissionError()), self.assertRaises(OSError):
            write_settings(path, {'store_proxy': 'direct'})
        self.assertEqual(path.read_bytes(), previous)
        self.assertFalse(list(self.folder.glob('.LCSC3D-settings-*')))

    def test_legacy_configuration_migrates_once_to_app_data_and_removes_old_file(self):
        legacy = self.folder / '软件目录' / 'LCSC3D-settings.json'
        legacy.parent.mkdir()
        settings = {'destination': '已有下载目录', 'step': True, 'store_proxy': 'direct', 'log_level': 'INFO'}
        legacy.write_text(json.dumps(settings), encoding='utf-8')
        target = data_directory() / 'LCSC3D-settings.json'
        self.assertEqual(read_settings(target, legacy), settings)
        self.assertFalse(legacy.exists())
        self.assertEqual(json.loads(target.read_text(encoding='utf-8')), settings)

    def test_existing_app_data_configuration_takes_priority_over_legacy_file(self):
        target = data_directory() / 'LCSC3D-settings.json'
        write_settings(target, {'destination': '新配置'})
        legacy = self.folder / 'legacy.json'
        legacy.write_text('{"destination":"old"}', encoding='utf-8')
        self.assertEqual(read_settings(target, legacy), {'destination': '新配置'})
        self.assertTrue(legacy.exists())

    def test_failed_migration_preserves_old_configuration(self):
        legacy = self.folder / 'legacy.json'
        legacy.write_text('{"store_proxy":"direct"}', encoding='utf-8')
        with patch('app_settings.write_settings', side_effect=PermissionError()):
            self.assertEqual(read_settings(data_directory() / 'LCSC3D-settings.json', legacy), {'store_proxy': 'direct'})
        self.assertTrue(legacy.exists())

    def test_default_paths_and_partial_downloads_stay_in_app_data(self):
        base = data_directory()
        self.assertEqual(base, self.folder / 'LCSC3D')
        self.assertEqual(app_logging.default_log_directory().parent, base)
        self.assertEqual(SessionVault().path.parent, base)
        output = self.folder / '导出目录' / 'model.step'
        paths = []
        real_replace = backend.replace_file

        def inspect(source, destination):
            paths.append(Path(source))
            real_replace(source, destination)

        with patch('backend.replace_file', side_effect=inspect):
            backend.atomic_write(output, b'public model')
        self.assertEqual(output.read_bytes(), b'public model')
        self.assertEqual(paths[0].parent, temporary_directory())
        self.assertEqual(list(output.parent.iterdir()), [output])
        self.assertFalse(list(temporary_directory().iterdir()))

    @unittest.skipUnless(sys.platform == 'win32', 'Windows cross-volume replacement')
    def test_cross_volume_replacement_fallback_moves_without_a_sidecar(self):
        import errno
        source = temporary_directory() / 'model.tmp'
        source.write_bytes(b'complete model')
        target = self.folder / 'exports' / 'model.step'
        target.parent.mkdir()
        with patch('app_paths.os.replace', side_effect=OSError(errno.EXDEV, 'different volumes')):
            replace_file(source, target)
        self.assertEqual(target.read_bytes(), b'complete model')
        self.assertFalse(source.exists())
        self.assertEqual(list(target.parent.iterdir()), [target])


@unittest.skipUnless(sys.platform == 'win32', 'Native Qt settings window')
class SettingsWindowTests(PreferencesTestCase):
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
        self.settings_path = self.folder / 'LCSC3D-settings.json'
        path = patch.object(main, 'SETTINGS_PATH', self.settings_path)
        path.start()
        self.addCleanup(path.stop)
        self.window = main.MainWindow(settings_enabled=False)
        self.window.settings_enabled = True
        self.window.show()
        self.app.processEvents()

    def tearDown(self):
        self.window.close()
        self.window.deleteLater()
        self.app.sendPostedEvents(None, QEvent.DeferredDelete)

    def test_header_opens_the_settings_window_with_requested_defaults(self):
        self.window.settings_button.click()
        dialog = self.window.settings_dialog
        self.app.processEvents()
        self.assertTrue(dialog.isVisible())
        self.assertEqual(dialog.windowTitle(), '设置')
        self.assertEqual([dialog.store_proxy_combo.itemText(i) for i in range(2)],
                         ['使用系统代理', '不使用系统代理'])
        self.assertEqual(dialog.store_proxy_combo.currentData(), 'system')
        self.assertEqual(dialog.update_proxy_combo.currentData(), 'system')
        self.assertEqual(dialog.log_level_combo.currentText(), 'Debug')
        self.window.open_settings()
        self.assertIs(self.window.settings_dialog, dialog)

    def test_saved_preferences_are_live_and_restore_with_existing_download_settings(self):
        self.window.path_input.setText(str(self.folder / '元件目录'))
        self.window.obj_box.setChecked(True)
        self.window.settings_button.click()
        dialog = self.window.settings_dialog
        dialog.store_proxy_combo.setCurrentIndex(1)
        dialog.log_level_combo.setCurrentIndex(dialog.log_level_combo.findData('WARNING'))
        dialog.save_button.click()
        self.assertFalse(dialog.isVisible())
        expected = Preferences(store_proxy='direct', update_proxy='system', log_level='WARNING')
        self.assertEqual(get_preferences(), expected)
        self.assertEqual(app_logging.LOGGER.level, logging.WARNING)
        saved = json.loads(self.settings_path.read_text(encoding='utf-8'))
        self.assertTrue(saved['obj'])
        self.assertEqual(saved['destination'], str(self.folder / '元件目录'))
        self.assertEqual(Preferences.from_mapping(saved), expected)
        set_preferences(Preferences())
        self.window._restore_settings()
        self.assertEqual(get_preferences(), expected)
        self.assertTrue(self.window.obj_box.isChecked())
        self.window.settings_button.click()
        reopened = self.window.settings_dialog
        self.assertEqual(reopened.store_proxy_combo.currentData(), 'direct')
        self.assertEqual(reopened.log_level_combo.currentData(), 'WARNING')

    def test_cancelling_does_not_change_proxy_or_log_level(self):
        self.window.settings_button.click()
        dialog = self.window.settings_dialog
        dialog.store_proxy_combo.setCurrentIndex(1)
        dialog.update_proxy_combo.setCurrentIndex(1)
        dialog.log_level_combo.setCurrentIndex(4)
        dialog.cancel_button.click()
        self.assertEqual(get_preferences(), Preferences())
        self.assertEqual(app_logging.LOGGER.level, logging.DEBUG)
        self.assertFalse(self.settings_path.exists())

    def test_merge_preferences_persist_independently_and_lock_while_downloading(self):
        window = self.window
        self.assertFalse(window.merge_schlib_box.isChecked())
        self.assertFalse(window.merge_pcblib_box.isChecked())
        self.assertFalse(window.schlib_name_input.isEnabled())
        window.schlib_box.setChecked(True)
        window.merge_schlib_box.setChecked(True)
        window.schlib_name_input.setText('我的符号')
        window.pcblib_name_input.setText('我的封装.PcbLib')
        window.save_settings()
        window.merge_schlib_box.setChecked(False)
        window.schlib_name_input.clear()
        window._restore_settings()
        self.assertTrue(window.merge_schlib_box.isChecked())
        self.assertFalse(window.merge_pcblib_box.isChecked())
        self.assertEqual(window.schlib_name_input.text(), '我的符号')
        self.assertEqual(window.pcblib_name_input.text(), '我的封装.PcbLib')
        self.assertTrue(window.schlib_name_input.isEnabled())
        self.assertFalse(window.pcblib_name_input.isEnabled())
        window.set_running(True)
        self.assertFalse(window.merge_schlib_box.isEnabled())
        self.assertFalse(window.schlib_name_input.isEnabled())
        window.set_running(False)
        self.assertTrue(window.merge_schlib_box.isEnabled())
        self.assertTrue(window.schlib_name_input.isEnabled())
        window.schlib_box.setChecked(False)
        self.assertFalse(window.merge_schlib_box.isEnabled())
        self.assertFalse(window.schlib_name_input.isEnabled())

    def test_save_error_leaves_the_dialog_open_and_runtime_preferences_unchanged(self):
        self.window.settings_button.click()
        dialog = self.window.settings_dialog
        dialog.store_proxy_combo.setCurrentIndex(1)
        with patch('main.write_settings', side_effect=PermissionError()):
            dialog.save_button.click()
        self.assertTrue(dialog.isVisible())
        self.assertIn('无法保存设置', dialog.status.text())
        self.assertEqual(get_preferences(), Preferences())
        self.assertFalse(self.settings_path.exists())

    def test_open_log_button_opens_the_log_directory(self):
        self.window.settings_button.click()
        with patch('settings_ui.QDesktopServices.openUrl', return_value=True) as opened:
            self.window.settings_dialog.open_log_button.click()
        self.assertEqual(Path(opened.call_args.args[0].toLocalFile()), self.log_dir)
        self.assertTrue(self.log_dir.is_dir())

    def test_package_button_creates_zip_and_opens_its_directory(self):
        self.window.settings_button.click()
        dialog = self.window.settings_dialog
        with patch('settings_ui.QDesktopServices.openUrl', return_value=True) as opened:
            dialog.package_log_button.click()
            self.assertFalse(dialog.clear_log_button.isEnabled())
            deadline = time.monotonic() + 5
            while dialog.worker is not None and time.monotonic() < deadline:
                self.app.processEvents()
                time.sleep(.01)
        self.assertIsNone(dialog.worker)
        bundles = list((data_directory() / 'diagnostics').glob('*.zip'))
        self.assertEqual(len(bundles), 1)
        self.assertEqual(Path(opened.call_args.args[0].toLocalFile()), bundles[0].parent)
        self.assertIn('日志已打包', dialog.status.text())
        self.assertTrue(dialog.clear_log_button.isEnabled())

    def test_clear_requires_confirmation_and_logging_continues(self):
        self.window.settings_button.click()
        dialog = self.window.settings_dialog
        app_logging.log_event('ERROR', 'test.before_clear')
        with patch('settings_ui.QMessageBox.question', return_value=QMessageBox.No):
            dialog.clear_log_button.click()
        self.assertIn('test.before_clear', [row['event'] for row in self.log_entries()])
        with patch('settings_ui.QMessageBox.question', return_value=QMessageBox.Yes):
            dialog.clear_log_button.click()
        self.assertNotIn('test.before_clear', [row['event'] for row in self.log_entries()])
        self.assertTrue(app_logging.log_event('ERROR', 'test.after_clear'))
        self.assertIn('日志已清除', dialog.status.text())

    def test_packaging_failure_restores_controls_and_allows_retry(self):
        self.window.settings_button.click()
        dialog = self.window.settings_dialog
        with patch('settings_ui.package_logs', side_effect=PermissionError()):
            dialog.package_log_button.click()
            deadline = time.monotonic() + 5
            while dialog.worker is not None and time.monotonic() < deadline:
                self.app.processEvents()
                time.sleep(.01)
        self.assertIsNone(dialog.worker)
        self.assertIn('打包失败', dialog.status.text())
        self.assertTrue(dialog.package_log_button.isEnabled())
        self.assertTrue(dialog.save_button.isEnabled())

    def test_closing_during_packaging_waits_for_the_worker(self):
        self.window.settings_button.click()
        dialog = self.window.settings_dialog
        release = threading.Event()
        import settings_ui
        original = settings_ui.package_logs
        def delayed():
            release.wait(timeout=3)
            return original()
        with patch('settings_ui.package_logs', side_effect=delayed), \
                patch('settings_ui.QDesktopServices.openUrl', return_value=True):
            dialog.package_log_button.click()
            dialog.reject()
            self.assertTrue(dialog.isVisible())
            self.window.close()
            self.assertTrue(self.window.isVisible())
            release.set()
            deadline = time.monotonic() + 5
            while self.window.isVisible() and time.monotonic() < deadline:
                self.app.processEvents()
                time.sleep(.01)
        self.assertIsNone(dialog.worker)
        self.assertFalse(self.window.isVisible())

    def test_normal_close_writes_only_to_app_data_and_keeps_exe_directory_clean(self):
        exe_directory = self.folder / '软件目录'
        exe_directory.mkdir()
        exe = exe_directory / 'LCSC3D.exe'
        exe.write_bytes(b'synthetic executable')
        settings = data_directory() / 'LCSC3D-settings.json'
        with patch.object(main, 'SETTINGS_PATH', settings), \
                patch.object(main, 'LEGACY_SETTINGS_PATH', exe_directory / 'LCSC3D-settings.json'):
            self.window._restore_settings()
            self.assertEqual(self.window.path_input.text(), str(data_directory() / 'downloads'))
            self.window.close()
        self.assertTrue(settings.is_file())
        self.assertEqual(list(exe_directory.iterdir()), [exe])


if __name__ == '__main__':
    unittest.main()
