"""Inject real failures and assert the logs identify feature, stage and cause."""
from concurrent.futures import ThreadPoolExecutor
import errno
import http.client
import json
import logging
import os
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app_logging as logs
import backend
import main
import updater
from app_settings import Preferences, get_preferences, set_preferences, read_settings, initial_log_level
from errors import Cancelled, DownloadError
from favorites import Jobs
from favorites_selftest import OfflineStore
from library_preview import build_library_preview, SvgPreviewPage
from store import StoreClient, MemorySession, CaptchaRequired
from store_session import SessionVault
from test_backend import FakeApi, STEP, OBJ
from test_altium import fixture
from test_updater import Response, NEW, OLD, SHA, release_data


class DiagnosticTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        env = patch.dict(os.environ, {'LOCALAPPDATA': directory.name})
        env.start()
        self.addCleanup(env.stop)
        preferences = get_preferences()
        set_preferences(Preferences())
        self.addCleanup(set_preferences, preferences)
        self.folder = self.directory / 'LCSC3D' / 'logs'
        logs.configure_logging(version=main.VERSION, directory=self.folder)
        self.addCleanup(logs.close_logging)

    def entries(self, event=None):
        rows = []
        for path in self.folder.glob('LCSC3D*.log'):
            rows.extend(json.loads(line) for line in path.read_text(encoding='utf-8').splitlines())
        return [row for row in rows if event is None or row['event'] == event]

    def assert_cause(self, row, error_type, function=None):
        self.assertTrue(row['error_id'])
        cause = next(item for item in row['causes'] if item['error'] == error_type)
        if function:
            self.assertTrue(any(frame['function'] == function and frame['line'] > 0 for frame in cause['frames']), cause)
        return cause

    def assert_private_absent(self, secret):
        self.assertNotIn(secret, '\n'.join(path.read_text(encoding='utf-8') for path in self.folder.glob('*.log')))

    def test_nested_thread_operations_keep_root_and_isolate_parts(self):
        barrier = threading.Barrier(2)
        @logs.traced('test.part', lambda part: {'part': part})
        def part_operation(part):
            barrier.wait(timeout=2)
            logs.log_event('INFO', 'test.part_result')
        with logs.operation('test.batch'):
            root_id = logs.current_context()['root_id']
            with ThreadPoolExecutor(max_workers=2) as pool:
                tasks = [logs.submit_logged(pool, part_operation, part) for part in ('C2040', 'C20197')]
                for task in tasks:
                    task.result()
        rows = self.entries('test.part_result')
        self.assertEqual({row['part'] for row in rows}, {'C2040', 'C20197'})
        self.assertEqual({row['root_id'] for row in rows}, {root_id})
        self.assertEqual(len({row['operation_id'] for row in rows}), 2)
        self.assertEqual(logs.current_context(), {})
        self.assertTrue(all(row['source']['line'] > 0 and row['session_id'] and row['pid'] for row in rows))

    def test_exception_chain_preserves_system_code_without_error_text(self):
        secret = 'PRIVATE-authCode-13800000000'
        try:
            try:
                raise PermissionError(errno.EACCES, secret)
            except PermissionError as cause:
                raise DownloadError('https://passport.jlc.com/?code=' + secret) from cause
        except DownloadError as exc:
            logs.record_error(exc, 'test.failed', part='C2040', stage='write')
        row = self.entries('test.failed')[0]
        self.assertEqual(self.assert_cause(row, 'PermissionError')['errno'], errno.EACCES)
        self.assertEqual(row['stage'], 'write')
        self.assert_private_absent(secret)

    def test_oversized_secret_metadata_is_rejected(self):
        secret = 'PRIVATE-credentials'
        logs.log_event('INFO', 'test.metadata', password=secret, headers={'Authorization':secret},
                       payload=secret, location='C:\\Users\\private\\output', request='https://x/?code='+secret)
        self.assert_private_absent(secret)
        self.assert_private_absent('C:\\Users')
        self.assertEqual(self.entries()[0]['password'], '<redacted>')

    def test_download_partial_failure_identifies_part_format_stage_and_root(self):
        api = FakeApi(step=STEP, obj=OBJ)
        with patch('backend.model_reference', return_value=SimpleNamespace(name='model', uuid='synthetic')), \
                patch('backend.export_pcblib', side_effect=ValueError('PRIVATE-converter-data')):
            results = backend.download_batch(['C2040', 'C20197'],
                backend.Options(self.directory / 'exports', ('STEP', 'PCBLIB')), api=api)
        self.assertTrue(all(result.status == '部分完成' for result in results))
        rows = self.entries('download.format_failed')
        self.assertEqual({row['part'] for row in rows}, {'C2040','C20197'})
        self.assertTrue(all(row['format'] == 'PCBLIB' and row['stage'] == 'convert' for row in rows))
        self.assertEqual(len({row['root_id'] for row in rows}), 1)
        self.assertEqual(self.entries('download.batch_result')[0]['partial'], 2)
        self.assert_private_absent('PRIVATE-converter-data')

    def test_file_permission_failure_is_distinct_from_conversion_failure(self):
        api = FakeApi(step=STEP)
        with patch('backend.model_reference', return_value=SimpleNamespace(name='model', uuid='synthetic')), \
                patch('backend.replace_file', side_effect=PermissionError(errno.EACCES, 'PRIVATE-directory')):
            result = backend.download_part('C2040', backend.Options(self.directory, ('STEP',)), api)
        self.assertEqual(result.status, '失败')
        row = self.entries('download.format_failed')[0]
        self.assertEqual((row['part'], row['format'], row['stage']), ('C2040','STEP','write'))
        self.assertEqual(self.assert_cause(row, 'PermissionError')['errno'], errno.EACCES)
        self.assertTrue(self.entries('file.write.failed'))
        self.assert_private_absent('PRIVATE-directory')

    def test_real_schlib_and_pcblib_conversion_are_traced(self):
        from altium import export_schlib, export_pcblib
        data = fixture()
        for event, export in (('export.schlib', export_schlib), ('export.pcblib', export_pcblib)):
            output = export(data, 'C2765186')
            self.assertTrue(output)
            self.assertEqual(self.entries(event + '.finished')[0]['part'], 'C2765186')

    def test_cancel_is_reported_without_a_false_error(self):
        stop = threading.Event()
        stop.set()
        result = backend.download_batch(['C2040'], backend.Options(self.directory), cancelled=stop, api=FakeApi())
        self.assertEqual(result[0].status, '已取消')
        self.assertEqual(self.entries('download.batch_result')[0]['cancelled'], 1)
        self.assertFalse([row for row in self.entries() if row['level'] in ('ERROR','CRITICAL')])

    def test_login_catalog_and_favorites_flow_has_named_operations(self):
        service = OfflineStore()
        self.addCleanup(service.close)
        client = service.client()
        qr = client.start_qr()
        client.scan_status(qr['token'])
        client.finish_login(qr['token'])
        client.search_results_page('C2040')
        product = client.product('C2040')
        client.favorites()
        client.add_favorite(product)
        client.remove_favorite(product)
        client.image('https://mp.weixin.qq.com/offline.png')
        client.login_password('OFFLINE', 'fixture-password')
        client.send_sms('13800000000')
        client.login_sms('13800000000', '123456')
        client.clear()
        events = {row['event'] for row in self.entries()}
        for name in ('login.qr_create','login.qr_poll','login.qr_confirm','login.password','login.sms_send',
                     'login.sms_verify','catalog.search_page','catalog.detail','favorites.read','favorites.add',
                     'favorites.remove','image.fetch','session.logout'):
            self.assertIn(name+'.finished', events)
        for secret in ('fixture-password','13800000000','123456','offline-token','offline-auth'):
            self.assert_private_absent(secret)

    def test_expected_captcha_is_not_logged_as_login_error(self):
        service = OfflineStore()
        self.addCleanup(service.close)
        service.risk_required = True
        with self.assertRaises(CaptchaRequired):
            service.client().send_sms('13800000000')
        self.assertTrue(self.entries('login.sms_send.challenge_required'))
        self.assertFalse(self.entries('login.sms_send.failed'))

    def test_error_level_preserves_service_rejection_code(self):
        service = OfflineStore()
        self.addCleanup(service.close)
        service.credential_code = 10212
        logs.set_log_level('ERROR')
        with self.assertRaises(Exception):
            service.client().login_password('OFFLINE', 'fixture-password')
        row = self.entries('login.password.failed')[0]
        self.assertEqual(self.assert_cause(row, 'StoreError')['service_code'], 10212)
        self.assert_private_absent('fixture-password')

    def test_saved_threshold_applies_before_startup_events(self):
        path = self.directory / 'settings.json'
        path.write_text('{"log_level":"CRITICAL"}', encoding='utf-8')
        logs.configure_logging(initial_log_level(path), directory=self.folder)
        logs.log_runtime()
        self.assertEqual(self.entries(), [])

    def test_crash_marker_and_empty_dump_are_removed_on_clean_exit(self):
        logs.start_crash_capture()
        self.addCleanup(logs.stop_crash_capture)
        self.assertTrue(list(self.folder.glob('run-*.json')))
        self.assertTrue(list(self.folder.glob('LCSC3D-crash-*.log')))
        logs.stop_crash_capture()
        self.assertFalse(list(self.folder.glob('run-*.json')))
        self.assertFalse(list(self.folder.glob('LCSC3D-crash-*.log')))

    def test_broken_network_records_http_stage_and_original_cause(self):
        client = StoreClient()
        with patch.object(client.session.opener, 'open', side_effect=http.client.IncompleteRead(b'PRIVATE', 90)), \
                self.assertRaises(Exception):
            client.start_qr()
        row = self.entries('login.qr_create.failed')[0]
        cause = self.assert_cause(row, 'IncompleteRead')
        self.assertEqual((cause['received_bytes'], cause['expected']), (7,90))
        self.assertTrue(any(row.get('operation') == '获取登录二维码' for row in self.entries()))
        self.assert_private_absent('PRIVATE')

    def test_model_parser_failure_keeps_worker_part_and_code_location(self):
        with patch('main.NetworkApi', return_value=FakeApi()):
            worker = main.ModelPreviewWorker('C2040', 3)
            worker.run()
        row = self.entries('preview.model_load_failed')[0]
        self.assertEqual((row['part'], row['revision']), ('C2040',3))
        self.assert_cause(row, 'ValueError', 'run')

    def test_invalid_svg_is_logged_even_when_returned_as_preview_error(self):
        from library_preview import _read_svg
        result = _read_svg({'svg':'<svg PRIVATE'}, 'symbol')
        self.assertTrue(result.error)
        row = self.entries('preview.svg_invalid')[0]
        self.assertEqual(row['viewer'], 'symbol')
        self.assert_cause(row, 'ParseError', '_read_svg')
        self.assert_private_absent('PRIVATE')

    def test_render_failures_keep_fixed_reason_without_page_content(self):
        page = SimpleNamespace(state=SimpleNamespace(emit=lambda *args: None), diagnostic_context=logs.new_context(part='C2040'))
        value = json.dumps({'token': 2, 'status':'error','message':'PRIVATE-SVG-data','diagnostic':'shader_compile'})
        main.PreviewPage.javaScriptConsoleMessage(page, SimpleNamespace(value=0), 'LCSC3D_STATE:'+value, 12, 'PRIVATE-source')
        row = self.entries('preview.render_failed')[0]
        self.assertEqual((row['reason'],row['part']), ('shader_compile','C2040'))
        logs.log_script_error(SimpleNamespace(value=2),'TypeError PRIVATE-content',83,'svg')
        self.assertEqual(self.entries('preview.script_error')[0]['script_line'],83)
        self.assert_private_absent('PRIVATE')

    def test_renderer_process_exit_is_reported(self):
        window = SimpleNamespace(current_3d='C2040', web_revision=5, on_web_preview_state=lambda *a: None)
        main.MainWindow.on_viewer_terminated(window, SimpleNamespace(value=2), 127)
        row = self.entries('preview.renderer_terminated')[0]
        self.assertEqual((row['exit_code'],row['revision'],row['part']), (127,5,'C2040'))

    def test_async_ui_callback_failure_has_original_operation(self):
        worker = SimpleNamespace(log_context=logs.new_context(operation='detail'))
        def failed(value):
            raise ValueError('PRIVATE-response')
        # SimpleNamespace is unhashable; use an ordinary worker identity.
        worker = type('Worker', (), {'log_context': worker.log_context})()
        jobs = SimpleNamespace(sender=lambda:worker, current=lambda w:True, workers={worker:(failed,failed,None)})
        with self.assertRaises(ValueError):
            Jobs._loaded(jobs, {}, None)
        row = self.entries('store.ui_callback_failed')[0]
        self.assertEqual(row['operation_id'],worker.log_context['operation_id'])
        self.assert_cause(row,'ValueError','failed')

    def test_corrupt_settings_and_session_are_recorded(self):
        settings = self.directory/'settings.json'
        settings.write_text('{PRIVATE', encoding='utf-8')
        self.assertEqual(read_settings(settings), {})
        row = self.entries('settings.read_failed')[0]
        self.assertEqual(self.assert_cause(row,'JSONDecodeError')['lineno'],1)
        vault = SessionVault(self.directory/'session.bin')
        vault.path.write_bytes(b'PRIVATE-INVALID')
        self.assertFalse(vault.load_into(StoreClient()))
        self.assertTrue(self.entries('session.saved_state_invalid'))
        self.assertFalse(vault.path.exists())
        self.assert_private_absent('PRIVATE')

    def stage_update(self):
        target = self.directory/'LCSC3D.exe'
        target.write_bytes(OLD)
        client = updater.UpdateClient()
        release = updater.parse_release(release_data(),'2.0.0')
        def opened(url):
            return Response((SHA+'  LCSC3D.exe\n').encode() if url.endswith('.txt') else NEW)
        with patch.object(client,'_open',side_effect=opened):
            manifest = client.download(release,target)
        return manifest,target

    def test_update_helper_logs_phases_and_rollback_with_parent_correlation(self):
        manifest,target = self.stage_update()
        original = self.entries('update.verified')[0]
        child = SimpleNamespace(poll=lambda:1)
        with patch('updater._wait_for_parent'),patch('updater._spawn',return_value=child), \
                patch('updater._wait_for_ack',return_value=False),patch('updater._show_failure'):
            self.assertEqual(updater.apply_update(manifest),1)
        self.assertEqual(target.read_bytes(),OLD)
        row = self.entries('update.install_failed')[0]
        self.assertEqual(row['root_id'],original['root_id'])
        self.assertEqual(row['stage'],'confirm_startup')
        self.assertTrue(self.entries('update.rollback_completed'))
        result = json.loads((manifest.parent/'result.json').read_text(encoding='utf-8'))
        self.assertEqual(result['error_id'],row['error_id'])
        self.assertTrue((self.folder/'LCSC3D-update.log').exists())

    def test_update_checksum_failure_identifies_integrity_problem(self):
        client = updater.UpdateClient()
        target = self.directory/'LCSC3D.exe'
        target.write_bytes(OLD)
        def opened(url):
            return Response((SHA+'  LCSC3D.exe\n').encode() if url.endswith('.txt') else NEW[:-1])
        with patch.object(client,'_open',side_effect=opened), self.assertRaises(updater.UpdateError):
            client.download(updater.parse_release(release_data(),'2.0.0'),target)
        row = self.entries('update.integrity_failed')[0]
        self.assertFalse(row['size_matches'])
        self.assertFalse(row['digest_matches'])
        self.assertEqual(target.read_bytes(),OLD)

    def test_unhandled_python_and_thread_hooks_preserve_stacks(self):
        original, thread_original = sys.excepthook, threading.excepthook
        self.addCleanup(setattr,sys,'excepthook',original)
        self.addCleanup(setattr,threading,'excepthook',thread_original)
        logs.install_exception_hooks()
        try:
            raise RuntimeError('PRIVATE-unhandled')
        except RuntimeError as exc:
            sys.excepthook(type(exc),exc,exc.__traceback__)
            threading.excepthook(SimpleNamespace(exc_value=exc,exc_traceback=exc.__traceback__))
        self.assertEqual(self.entries('application.uncaught')[0]['level'],'CRITICAL')
        self.assert_cause(self.entries('thread.uncaught')[0],'RuntimeError')
        self.assert_private_absent('PRIVATE-unhandled')

    def test_logging_write_failure_is_visible_and_does_not_break_operations(self):
        logs.close_logging()
        with patch('app_logging.SafeFileHandler',side_effect=PermissionError('PRIVATE-directory')):
            self.assertFalse(logs.configure_logging(directory=self.folder))
            self.assertEqual(logs.logging_health(),'PermissionError')
            @logs.traced('test.write_failed')
            def action():
                return 'usable'
            self.assertEqual(action(),'usable')


if __name__ == '__main__':
    unittest.main()
