"""Inject real failures and assert the logs identify feature, stage and cause."""
from concurrent.futures import ThreadPoolExecutor
import errno
import http.client
import json
import logging
import subprocess
import os
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lcsc3d.core import app_logging as logs
from lcsc3d.services import backend
from lcsc3d.ui import main
from lcsc3d.services import updater
from lcsc3d.core.app_settings import Preferences, get_preferences, set_preferences, read_settings, initial_log_level
from lcsc3d.core.errors import Cancelled, DownloadError
from lcsc3d.ui.favorites import Jobs
from lcsc3d.selftests.favorites_selftest import OfflineStore
from lcsc3d.ui.library_preview import build_library_preview, SvgPreviewPage
from lcsc3d.services.store import StoreClient, MemorySession, CaptchaRequired
from lcsc3d.services.store_session import SessionVault
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

    def test_malformed_run_markers_do_not_block_crash_capture_or_later_logs(self):
        for index, value in enumerate(([], None, 'private marker', {'pid': -1})):
            (self.folder / f'run-{90000 + index}.json').write_text(json.dumps(value), encoding='utf-8')
        (self.folder / 'run-90004.json').write_text('[' * 20000 + '0' + ']' * 20000, encoding='utf-8')
        self.addCleanup(logs.stop_crash_capture)
        logs.start_crash_capture()
        self.assertTrue(logs.diagnostic_status()['crash_capture_active'])
        self.assertTrue(logs.log_event('ERROR', 'test.after_invalid_marker'))
        self.assertTrue(self.entries('test.after_invalid_marker'))
        self.assertEqual(len(self.entries('application.marker_read_failed')), 5)

    def test_unavailable_historic_files_do_not_disable_current_crash_capture(self):
        for index in range(4):
            (self.folder / f'LCSC3D-crash-{90000 + index}.log').write_text('old crash', encoding='utf-8')
        marker = self.folder / 'run-90005.json'
        marker.write_text('{}', encoding='utf-8')
        unlink, stat = Path.unlink, Path.stat
        def locked(path, *args, **kwargs):
            if path.name.startswith('LCSC3D-crash-9000'):
                raise PermissionError('PRIVATE-locked-file')
            return unlink(path, *args, **kwargs)
        def disappeared(path, *args, **kwargs):
            if path == marker:
                raise FileNotFoundError('PRIVATE-removed-marker')
            return stat(path, *args, **kwargs)
        self.addCleanup(logs.stop_crash_capture)
        with patch.object(Path, 'unlink', locked), patch.object(Path, 'stat', disappeared):
            logs.start_crash_capture()
        self.assertTrue(logs.diagnostic_status()['crash_capture_active'])
        self.assertTrue(logs.log_event('ERROR', 'test.after_historic_cleanup_failure'))
        rows = [json.loads(line) for line in (self.folder / 'LCSC3D.log').read_text('utf-8').splitlines()]
        self.assertTrue(any(row['event'] == 'application.crash_cleanup_failed' for row in rows))
        self.assert_private_absent('PRIVATE-locked-file')
        self.assert_private_absent('PRIVATE-removed-marker')

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
        with patch('lcsc3d.services.backend.model_reference', return_value=SimpleNamespace(name='model', uuid='synthetic')), \
                patch('lcsc3d.services.backend.export_pcblib', side_effect=ValueError('PRIVATE-converter-data')):
            results = backend.download_batch(['C2040', 'C20197'],
                backend.Options(self.directory / 'exports', ('STEP', 'PCBLIB'), bind_3d_models=False), api=api)
        self.assertTrue(all(result.status == '部分完成' for result in results))
        rows = self.entries('download.format_failed')
        self.assertEqual({row['part'] for row in rows}, {'C2040','C20197'})
        self.assertTrue(all(row['format'] == 'PCBLIB' and row['stage'] == 'convert' for row in rows))
        self.assertEqual(len({row['root_id'] for row in rows}), 1)
        self.assertEqual(self.entries('download.batch_result')[0]['partial'], 2)
        self.assert_private_absent('PRIVATE-converter-data')

    def test_file_permission_failure_is_distinct_from_conversion_failure(self):
        api = FakeApi(step=STEP)
        with patch('lcsc3d.services.backend.model_reference', return_value=SimpleNamespace(name='model', uuid='synthetic')), \
                patch('lcsc3d.services.backend.replace_file', side_effect=PermissionError(errno.EACCES, 'PRIVATE-directory')):
            result = backend.download_part('C2040', backend.Options(self.directory, ('STEP',), bind_3d_models=False), api)
        self.assertEqual(result.status, '失败')
        row = self.entries('download.format_failed')[0]
        self.assertEqual((row['part'], row['format'], row['stage']), ('C2040','STEP','write'))
        self.assertEqual(self.assert_cause(row, 'PermissionError')['errno'], errno.EACCES)
        self.assertTrue(self.entries('file.write.failed'))
        self.assert_private_absent('PRIVATE-directory')

    def test_real_schlib_and_pcblib_conversion_are_traced(self):
        from lcsc3d.eda.altium import export_schlib, export_pcblib
        data = fixture()
        for event, export in (('export.schlib', export_schlib), ('export.pcblib', export_pcblib)):
            output = export(data, 'C2765186')
            self.assertTrue(output)
            self.assertEqual(self.entries(event + '.finished')[0]['part'], 'C2765186')

    def test_kicad_partial_failure_records_source_primitive_stage_and_batch(self):
        bad=fixture('C31966')
        bad['packageDetail']['dataStr']['shape'].append('FUTURE~PRIVATE-source-record')
        api=backend.NetworkApi()
        api.get_cad_data_of_component=lambda part: bad if part=='C31966' else fixture(part)
        options=backend.Options(self.directory/'exports',('KICAD_SYMBOL','KICAD_FOOTPRINT'),kicad_merge=True, bind_3d_models=False)
        results=backend.download_batch(['C31966','C23922'],options,api=api)
        self.assertEqual([r.status for r in results],['部分完成','成功'])
        row=next(r for r in self.entries('export.kicad_conversion_failed') if r['part']=='C31966')
        cause=self.assert_cause(row,'EdaError','require')
        self.assertEqual((cause['eda_code'],cause['eda_domain'],cause['eda_primitive']),
                         ('unsupported_primitive','footprint','FUTURE'))
        self.assertEqual((row['format'],row['stage']),('KICAD_FOOTPRINT','convert'))
        self.assertEqual(self.entries('download.export_plan')[0]['mode'],'merge')
        done=self.entries('download.format_completed')
        self.assertEqual({(r['part'],r['format']) for r in done},
                         {('C31966','KICAD_SYMBOL'),('C23922','KICAD_SYMBOL'),('C23922','KICAD_FOOTPRINT')})
        failed=self.entries('download.format_failed')
        self.assertEqual([(r['part'],r['format'],r['stage']) for r in failed],[('C31966','KICAD_FOOTPRINT','convert')])
        self.assertEqual(len({r['root_id'] for r in done+failed+[row]}),1)
        self.assert_private_absent('PRIVATE-source-record')

    def test_ad_rejection_and_ir_import_keep_structured_diagnostics(self):
        data=fixture('C31966')
        data['packageDetail']['dataStr']['shape'].append('FUTURE~PRIVATE-source-record')
        api=backend.NetworkApi()
        api.get_cad_data_of_component=lambda part:data
        backend.download_batch(['C31966'],backend.Options(self.directory/'ad',('SCHLIB','PCBLIB'),True,True, bind_3d_models=False),api=api)
        row=self.entries('download.format_failed')[0]
        self.assertEqual(self.assert_cause(row,'EdaError')['eda_primitive'],'FUTURE')
        data['packageDetail']['dataStr']['shape']=['VIA~4000~3000~2~~5~gge123']
        from lcsc3d.eda.easyeda_import import parse_component
        from lcsc3d.eda.eda_ir import dumps,loads,require_domain
        component=loads(dumps(parse_component(data,'C31966')))
        self.assertEqual(self.entries('eda.import_domain_failed')[-1]['domain'],'footprint')
        with self.assertRaises(DownloadError) as caught:
            require_domain(component,'footprint')
        self.assertEqual(caught.exception.eda_code,'invalid_via_dimensions')
        self.assert_private_absent('PRIVATE-source-record')

    def test_kicad_write_failure_and_cancel_log_rollback_and_preserve_previous_files(self):
        from lcsc3d.eda import library_files
        api=backend.NetworkApi()
        api.get_cad_data_of_component=lambda part:fixture(part)
        root=self.directory/'libraries'
        options=backend.Options(root,('KICAD_SYMBOL','KICAD_FOOTPRINT'),kicad_merge=True, bind_3d_models=False)
        backend.download_batch(['C23922'],options,api=api)
        before={p.relative_to(root):p.read_bytes() for p in root.rglob('*') if p.is_file()}
        original=library_files.replace_file
        for cancelled in (False,True):
            start=len(self.entries())
            stop=threading.Event()
            api.cancelled=stop
            def fail(source,target):
                if target.suffix=='.kicad_sym':
                    if cancelled:
                        stop.set()
                        raise Cancelled()
                    raise PermissionError(errno.EACCES,'PRIVATE-path')
                return original(source,target)
            with patch('lcsc3d.eda.library_files.replace_file',side_effect=fail):
                backend.download_batch(['C31966'],options,cancelled=stop,api=api)
            events=self.entries()[start:]
            self.assertTrue(any(r['event']=='library.rollback_completed' for r in events))
            self.assertEqual(before,{p.relative_to(root):p.read_bytes() for p in root.rglob('*') if p.is_file()})
            if cancelled:
                self.assertTrue(any(r['event']=='download.format_cancelled' and r['part']=='C31966' for r in events))
                self.assertFalse([r for r in events if r['level'] in ('ERROR','CRITICAL')])
            else:
                row=next(r for r in events if r['event']=='download.kicad_failed')
                self.assertEqual(row['stage'],'write')
                self.assertEqual(self.assert_cause(row,'PermissionError')['errno'],errno.EACCES)
        self.assert_private_absent('PRIVATE-path')

    def test_shared_footprint_reuse_and_deferred_outputs_keep_batch_diagnostics(self):
        data = {part: fixture(part) for part in ('C23922', 'C8734')}
        api = backend.NetworkApi()
        api.get_cad_data_of_component = lambda part: data[part]
        options = backend.Options(self.directory / 'exports', ('SCHLIB', 'PCBLIB'), True, True,
                                  keep_schlib=True, keep_pcblib=True, bind_3d_models=False)
        rows = backend.download_batch(['C23922', 'C8734'], options, api=api)
        self.assertEqual([row.status for row in rows], ['成功', '成功'])
        reuse = self.entries('library.footprint_reuse')
        self.assertTrue(any(row['count'] == 2 and row['unique'] == 1 and row['reused'] == 1 for row in reuse))
        completed = self.entries('download.individual_library_completed')
        self.assertEqual({(row['part'], row['format']) for row in completed},
                         {(part, fmt) for part in ('C23922', 'C8734') for fmt in ('SCHLIB', 'PCBLIB')})
        self.assertEqual(len({row['root_id'] for row in reuse + completed}), 1)
        self.assertFalse(self.entries('download.format_failed'))

    def test_cancel_is_reported_without_a_false_error(self):
        stop = threading.Event()
        stop.set()
        result = backend.download_batch(['C2040'], backend.Options(self.directory, bind_3d_models=False), cancelled=stop, api=FakeApi())
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

    def test_native_crash_after_clear_is_captured_in_isolated_subprocess(self):
        profile=self.directory/'crash-child'
        code=f'''import sys, os, faulthandler
sys.path.insert(0,{str(Path(__file__).resolve().parents[1])!r})
if sys.platform=='win32':
    import ctypes
    ctypes.windll.kernel32.SetErrorMode(0x8002)
from lcsc3d.core.app_logging import configure_logging,start_crash_capture,log_event
from lcsc3d.services.log_support import clear_logs
configure_logging(version="crash-test")
start_crash_capture()
log_event("ERROR","test.before_clear")
clear_logs()
log_event("ERROR","test.after_clear")
faulthandler._sigsegv()
'''
        result=subprocess.run([sys.executable,'-c',code],cwd=self.directory,
            env={**os.environ,'LOCALAPPDATA':str(profile)},stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=20)
        self.assertNotEqual(result.returncode,0)
        folder=profile/'LCSC3D/logs'
        log=(folder/'LCSC3D.log').read_text('utf-8')
        self.assertIn('test.after_clear',log)
        self.assertNotIn('test.before_clear',log)
        dumps=list(folder.glob('LCSC3D-crash-*.log'))
        self.assertEqual(len(dumps),1)
        self.assertIn(b'<module>',dumps[0].read_bytes())
        self.assertTrue(list(folder.glob('run-*.json')))

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
        with patch('lcsc3d.ui.main.NetworkApi', return_value=FakeApi()):
            worker = main.ModelPreviewWorker('C2040', 3)
            worker.run()
        row = self.entries('preview.model_load_failed')[0]
        self.assertEqual((row['part'], row['revision']), ('C2040',3))
        self.assert_cause(row, 'ValueError', 'run')

    def test_invalid_svg_is_logged_even_when_returned_as_preview_error(self):
        from lcsc3d.ui.library_preview import _read_svg
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
        with patch('lcsc3d.services.updater._wait_for_parent'),patch('lcsc3d.services.updater._spawn',return_value=child), \
                patch('lcsc3d.services.updater._wait_for_ack',return_value=False),patch('lcsc3d.services.updater._show_failure'):
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
        with patch('lcsc3d.core.app_logging.SafeFileHandler',side_effect=PermissionError('PRIVATE-directory')):
            self.assertFalse(logs.configure_logging(directory=self.folder))
            self.assertEqual(logs.logging_health(),'PermissionError')
            @logs.traced('test.write_failed')
            def action():
                return 'usable'
            self.assertEqual(action(),'usable')


if __name__ == '__main__':
    unittest.main()
