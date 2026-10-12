"""Resolve the footprint's official 3D library without changing model bytes."""
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lcsc3d.services import backend
from lcsc3d.core.app_logging import close_logging, configure_logging
from lcsc3d.core.errors import Cancelled, DownloadError
from lcsc3d.models.model3d import ModelReference, model_library_reference, model_library_asset
from test_backend import STEP, OBJ

PRIMARY = ModelReference('USB model', 'primary-asset')
COMPONENT = {'title': 'USB connector', 'packageDetail': {'title': 'USB model', 'dataStr': {
    'head': {'uuid_3d': 'library-record', 'c_para': {'3DModel': 'USB model'}},
    'shape': ['SVGNODE~' + json.dumps({'attrs': {'c_etype': 'outline3D', 'uuid': PRIMARY.uuid, 'title': PRIMARY.name}})]}}}
LIBRARY = {'success': True, 'result': {'uuid': 'library-record', 'docType': 16,
    'title': 'USB model', 'dataStr': {'head': {'uuid': 'backup-asset'}}}}


def library_bytes(library=LIBRARY):
    return json.dumps(library).encode()


class StepFallbackTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.folder = Path(self.directory.name)
        environment = patch.dict(os.environ, {'LOCALAPPDATA': str(self.folder / 'profile')})
        environment.start()
        self.addCleanup(environment.stop)
        self.addCleanup(close_logging)
        cache = patch('lcsc3d.services.backend.PAYLOAD_CACHE', backend.PayloadCache())
        cache.start()
        self.addCleanup(cache.stop)
        self.api = backend.NetworkApi(use_cache=False)

    def test_valid_primary_step_does_not_query_library(self):
        with patch.object(self.api, 'fetch', return_value=STEP) as fetch:
            value = backend.component_step_payload(COMPONENT, PRIMARY, self.api)
        self.assertEqual(value, backend.StepPayload(STEP))
        fetch.assert_called_once_with(backend.ENDPOINT_3D_MODEL_STEP.replace('modules.easyeda.com', 'modules.lceda.cn').format(uuid=PRIMARY.uuid), optional=True)

    def test_403_then_missing_mirror_resolves_library_record_and_asset(self):
        with patch.object(self.api, 'fetch', side_effect=[DownloadError('服务器返回 HTTP 403'), None, library_bytes(), STEP]) as fetch:
            value = backend.component_step_payload(COMPONENT, PRIMARY, self.api)
        self.assertEqual(value, backend.StepPayload(STEP, used_library=True))
        self.assertEqual([call.args[0] for call in fetch.call_args_list], [
            'https://modules.lceda.cn/qAxj6KHrDKw4blvCG8QJPs7Y/primary-asset',
            'https://modules.easyeda.com/qAxj6KHrDKw4blvCG8QJPs7Y/primary-asset',
            'https://lceda.cn/api/components/library-record',
            'https://modules.lceda.cn/qAxj6KHrDKw4blvCG8QJPs7Y/backup-asset'])

    def test_invalid_primary_payload_can_recover_via_library(self):
        with patch.object(self.api, 'fetch', side_effect=[b'<html>unavailable</html>', b'not STEP', library_bytes(), STEP]):
            self.assertTrue(backend.component_step_payload(COMPONENT, PRIMARY, self.api).used_library)

    def test_missing_both_primary_mirrors_can_recover(self):
        with patch.object(self.api, 'fetch', side_effect=[None, None, library_bytes(), STEP]):
            self.assertEqual(backend.component_step_payload(COMPONENT, PRIMARY, self.api).data, STEP)

    def test_library_query_can_recover_through_official_mirror(self):
        with patch.object(self.api, 'fetch', side_effect=[None, None, DownloadError('HTTP 403'), library_bytes(), STEP]) as fetch:
            self.assertTrue(backend.component_step_payload(COMPONENT, PRIMARY, self.api).used_library)
        self.assertEqual(fetch.call_args_list[3].args[0], 'https://easyeda.com/api/components/library-record')

    def test_same_resolved_asset_is_not_downloaded_twice(self):
        library = copy.deepcopy(LIBRARY)
        library['result']['dataStr']['head']['uuid'] = PRIMARY.uuid
        with patch.object(self.api, 'fetch', side_effect=[None, None, library_bytes(library)]) as fetch:
            with self.assertRaisesRegex(DownloadError, '相同的模型资源'):
                backend.component_step_payload(COMPONENT, PRIMARY, self.api)
        self.assertEqual(fetch.call_count, 3)

    def test_failure_preserves_both_mirror_diagnostics_and_fallback_reason(self):
        with patch.object(self.api, 'fetch', side_effect=[DownloadError('服务器返回 HTTP 403'), None, None, None]):
            with self.assertRaises(DownloadError) as caught:
                backend.component_step_payload(COMPONENT, PRIMARY, self.api)
        for text in ('主模型 STEP', '国内接口', 'HTTP 403', '国际接口', '文件不存在', '备用关联', '3D 库记录不存在'):
            self.assertIn(text, str(caught.exception))

    def test_missing_library_reference_keeps_primary_error(self):
        with patch.object(self.api, 'get_step_3d_model', side_effect=DownloadError('HTTP 403')), \
                patch.object(self.api, 'get_3d_library_model') as lookup:
            with self.assertRaisesRegex(DownloadError, 'HTTP 403.*没有备用 3D 库关联'):
                backend.component_step_payload({}, PRIMARY, self.api)
        lookup.assert_not_called()

    def test_fallback_failure_never_replaces_existing_step(self):
        with patch.object(self.api, 'get_cad_data_of_component', return_value=COMPONENT), \
                patch.object(self.api, 'get_step_3d_model', return_value=STEP):
            original = backend.download_part('C48800488', backend.Options(self.folder / 'exports', bind_3d_models=False), self.api)
        self.assertEqual(original.status, '成功')
        with patch.object(self.api, 'get_cad_data_of_component', return_value=COMPONENT), \
                patch.object(self.api, 'fetch', return_value=None):
            failed = backend.download_part('C48800488', backend.Options(self.folder / 'exports', bind_3d_models=False), self.api)
        self.assertEqual(failed.status, '失败')
        self.assertEqual(Path(original.files[0]).read_bytes(), STEP)
        self.assertEqual(list(Path(original.folder).iterdir()), [Path(original.files[0])])

    def test_invalid_library_ids_never_connect(self):
        for uuid in ('../secret', 'https://other.test/model', '', None):
            with self.subTest(uuid=uuid), patch.object(self.api, 'fetch') as fetch:
                with self.assertRaises(DownloadError):
                    self.api.get_3d_library_model(uuid)
                fetch.assert_not_called()

    def test_invalid_or_mismatched_library_cannot_supply_a_step_asset(self):
        cases = [b'<html>blocked</html>', b'null', b'{"success":false}',
            library_bytes({'success': True, 'result': {**LIBRARY['result'], 'uuid': 'another-record'}}),
            library_bytes({'success': True, 'result': {**LIBRARY['result'], 'docType': 2}}),
            library_bytes({'success': True, 'result': {**LIBRARY['result'], 'dataStr': {'head': {'uuid': '../model'}}}})]
        for raw in cases:
            with self.subTest(raw=raw), patch.object(self.api, 'fetch', return_value=raw) as fetch:
                with self.assertRaises(DownloadError):
                    self.api.get_3d_library_model('library-record')
                self.assertEqual(fetch.call_count, 2)
                self.assertTrue(all('/api/components/' in call.args[0] for call in fetch.call_args_list))

    def test_library_resolution_accepts_string_documents_and_caches_only_success(self):
        api = backend.NetworkApi(use_cache=True)
        library = copy.deepcopy(LIBRARY)
        library['result']['dataStr'] = json.dumps(library['result']['dataStr'])
        with patch.object(api, 'fetch', return_value=library_bytes(library)) as fetch:
            self.assertEqual(api.get_3d_library_model('library-record'), ModelReference('USB model', 'backup-asset'))
            self.assertEqual(api.get_3d_library_model('library-record').uuid, 'backup-asset')
        fetch.assert_called_once()
        with patch.object(api, 'fetch', return_value=None) as fetch:
            self.assertIsNone(api.get_3d_library_model('missing-record'))
            self.assertIsNone(api.get_3d_library_model('missing-record'))
        self.assertEqual(fetch.call_count, 4)

    def test_cancelled_primary_does_not_start_fallback(self):
        with patch.object(self.api, 'get_step_3d_model', side_effect=Cancelled()), \
                patch.object(self.api, 'get_3d_library_model') as lookup:
            with self.assertRaises(Cancelled):
                backend.component_step_payload(COMPONENT, PRIMARY, self.api)
        lookup.assert_not_called()

    def test_cancellation_after_resolution_does_not_fetch_backup(self):
        def resolve(uuid):
            self.api.cancelled.set()
            return ModelReference('USB model', 'backup-asset')
        with patch.object(self.api, 'get_step_3d_model', return_value=None) as step, \
                patch.object(self.api, 'get_3d_library_model', side_effect=resolve):
            with self.assertRaises(Cancelled):
                backend.component_step_payload(COMPONENT, PRIMARY, self.api)
        step.assert_called_once_with(PRIMARY.uuid)

    def test_download_preserves_official_bytes_and_reports_fallback_as_success(self):
        def step(uuid):
            return STEP if uuid == 'backup-asset' else None
        with patch.object(self.api, 'get_cad_data_of_component', return_value=COMPONENT), \
                patch.object(self.api, 'get_step_3d_model', side_effect=step), \
                patch.object(self.api, 'get_3d_library_model', return_value=ModelReference('USB model', 'backup-asset')), \
                patch.object(self.api, 'get_raw_3d_model_obj', return_value=OBJ) as obj:
            result = backend.download_part('C48800488', backend.Options(self.folder / 'exports', ('STEP', 'OBJ'), bind_3d_models=False), self.api)
        self.assertEqual(result.status, '成功', result.message)
        self.assertFalse(result.export_errors)
        self.assertIn('备用关联', result.message)
        self.assertEqual(next(Path(path) for path in result.files if path.endswith('.step')).read_bytes(), STEP)
        self.assertEqual(next(Path(path) for path in result.files if path.endswith('.obj')).read_bytes(), OBJ.encode())
        obj.assert_called_once_with(PRIMARY.uuid)

    def test_head_only_reference_is_resolved_as_a_library_after_direct_failure(self):
        component = copy.deepcopy(COMPONENT)
        component['packageDetail']['dataStr']['shape'] = []
        reference = backend.model_reference(component)
        self.assertEqual(reference.uuid, 'library-record')
        with patch.object(self.api, 'fetch', side_effect=[None, None, library_bytes(), STEP]):
            self.assertTrue(backend.component_step_payload(component, reference, self.api).used_library)

    def test_helpers_distinguish_library_record_and_model_file(self):
        self.assertEqual(model_library_reference(COMPONENT).uuid, 'library-record')
        self.assertEqual(model_library_asset(LIBRARY['result']).uuid, 'backup-asset')
        with self.assertRaisesRegex(DownloadError, '没有关联模型文件'):
            model_library_asset({'docType': 16, 'dataStr': {}})

    def test_successful_fallback_logs_entire_chain_under_one_operation(self):
        configure_logging(directory=self.folder / 'logs')
        with patch.object(self.api, 'fetch', side_effect=[None, None, library_bytes(), STEP]):
            backend.component_step_payload(COMPONENT, PRIMARY, self.api)
        close_logging()
        entries = [json.loads(line) for line in (self.folder / 'logs/LCSC3D.log').read_text(encoding='utf-8').splitlines()]
        required = {'resources.step_fallback_started', 'resources.step_fallback_resolved', 'resources.step_fallback_succeeded'}
        events = [row for row in entries if row['event'] in required]
        self.assertEqual({row['event'] for row in events}, required)
        self.assertEqual(len({row['root_id'] for row in events}), 1)
        self.assertEqual(events[-1]['bytes'], len(STEP))
        self.assertTrue(all('url' not in row and 'payload' not in row for row in events))

    def test_real_http_statuses_and_fallback_share_part_format_and_log_root(self):
        configure_logging(directory=self.folder / 'logs')
        counts = Counter()

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                counts[self.path] += 1
                if self.path == '/step/primary-asset':
                    status, body = (403, b'AccessDenied') if counts[self.path] == 1 else (404, b'NoSuchKey')
                elif self.path == '/library/library-record':
                    status, body = 200, library_bytes()
                else:
                    status, body = 200, STEP
                self.send_response(status)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base = 'http://127.0.0.1:' + str(server.server_port)
            with patch('lcsc3d.services.backend.ENDPOINT_3D_MODEL_STEP', base + '/step/{uuid}'), \
                    patch('lcsc3d.services.backend.MODEL_LIBRARY_ENDPOINTS', (base + '/library/{uuid}', base + '/mirror-library/{uuid}')), \
                    patch.object(self.api, 'get_cad_data_of_component', return_value=COMPONENT):
                result = backend.download_part('C48800488', backend.Options(self.folder / 'exports', bind_3d_models=False), self.api)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(3)
        self.assertEqual(result.status, '成功', result.message)
        self.assertEqual(counts, {'/step/primary-asset': 2, '/library/library-record': 1, '/step/backup-asset': 1})
        close_logging()
        entries = [json.loads(line) for line in (self.folder / 'logs/LCSC3D.log').read_text('utf-8').splitlines()]
        rejected = [row for row in entries if row['event'] == 'resources.http_rejected']
        missing = [row for row in entries if row['event'] == 'resources.unavailable']
        self.assertEqual([row['status'] for row in rejected], [403])
        self.assertEqual([row['status'] for row in missing], [404])
        succeeded = next(row for row in entries if row['event'] == 'resources.step_fallback_succeeded')
        self.assertEqual(succeeded['part'], 'C48800488')
        self.assertEqual(succeeded['format'], 'STEP')
        self.assertEqual({row['root_id'] for row in rejected + missing + [succeeded]}, {succeeded['root_id']})

    def test_failed_fallback_logs_export_stage_and_retains_operation_identity(self):
        configure_logging(directory=self.folder / 'logs')
        with patch.object(self.api, 'get_cad_data_of_component', return_value=COMPONENT), \
                patch.object(self.api, 'fetch', return_value=None):
            result = backend.download_part('C48800488', backend.Options(self.folder / 'exports', bind_3d_models=False), self.api)
        self.assertEqual(result.status, '失败')
        close_logging()
        entries = [json.loads(line) for line in (self.folder / 'logs/LCSC3D.log').read_text('utf-8').splitlines()]
        fallback = next(row for row in entries if row['event'] == 'resources.step_fallback_failed')
        failed = next(row for row in entries if row['event'] == 'download.format_failed')
        self.assertEqual((failed['part'], failed['format'], failed['stage']), ('C48800488', 'STEP', 'download'))
        self.assertEqual(failed['root_id'], fallback['root_id'])
        self.assertTrue(failed['causes'])
        self.assertFalse(list((self.folder / 'exports').rglob('*.step')))


if __name__ == '__main__':
    unittest.main()
