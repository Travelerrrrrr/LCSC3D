"""Exercise connection reuse, concurrent downloads, cancellation and cache limits."""
import gzip
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import backend
from test_backend import FakeApi, STEP


class DownloadPerformanceTests(unittest.TestCase):
    def test_step_and_obj_overlap_and_share_one_obj_for_wrl(self):
        barrier = threading.Barrier(2)

        class Api(FakeApi):
            obj_calls = 0

            def get_step_3d_model(self, uuid):
                barrier.wait(timeout=2)
                return super().get_step_3d_model(uuid)

            def get_raw_3d_model_obj(self, uuid):
                self.obj_calls += 1
                barrier.wait(timeout=2)
                return 'v 0 0 0'

        api = Api()
        model = SimpleNamespace(name='QFN', uuid='model-id')
        exporter = SimpleNamespace(output=SimpleNamespace(raw_wrl='#VRML V2.0 utf8'))
        with tempfile.TemporaryDirectory() as folder, \
                patch('backend.Easyeda3dModelImporter', return_value=SimpleNamespace(output=model)), \
                patch('backend.Exporter3dModelKicad', return_value=exporter):
            result = backend.download_part('C2040', backend.Options(Path(folder), ('STEP', 'WRL', 'OBJ')), api)
        self.assertEqual(result.status, '成功')
        self.assertEqual(api.step_calls, 1)
        self.assertEqual(api.obj_calls, 1)

    def test_unavailable_obj_is_not_downloaded_twice(self):
        api = FakeApi()
        model = SimpleNamespace(name='QFN', uuid='model-id')
        with tempfile.TemporaryDirectory() as folder, \
                patch('backend.Easyeda3dModelImporter', return_value=SimpleNamespace(output=model)), \
                patch.object(api, 'get_raw_3d_model_obj', return_value=None) as fetch:
            result = backend.download_part('C2040', backend.Options(Path(folder), ('STEP', 'WRL', 'OBJ')), api)
        self.assertEqual(result.status, '部分完成')
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(len(result.files), 1)

    def test_batch_runs_concurrently_and_preserves_input_order_on_failure(self):
        barrier = threading.Barrier(3)
        second_finished = threading.Event()
        callbacks = []

        def download(part, options, api, progress):
            barrier.wait(timeout=2)
            if part == 'C1':
                self.assertTrue(second_finished.wait(2))
            elif part == 'C2':
                second_finished.set()
            else:
                raise backend.DownloadError('missing')
            return backend.Result(part, '成功')

        with patch('backend.download_part', side_effect=download):
            results = backend.download_batch(['C1', 'C2', 'C3'], None, api=FakeApi(),
                                              on_result=lambda row, result: callbacks.append((row, result.part)))
        self.assertEqual([result.part for result in results], ['C1', 'C2', 'C3'])
        self.assertEqual([result.status for result in results], ['成功', '成功', '失败'])
        self.assertEqual(set(callbacks), {(0, 'C1'), (1, 'C2'), (2, 'C3')})

    def test_cancelled_batch_does_not_start_remaining_downloads(self):
        cancelled = threading.Event()

        def first(part, *args):
            cancelled.set()
            return backend.Result(part, '成功')

        with patch('backend.download_part', side_effect=first) as fetch:
            results = backend.download_batch(['C1', 'C2', 'C3'], None, cancelled, api=FakeApi(), workers=1)
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual([result.status for result in results], ['成功', '已取消', '已取消'])

    def test_overwrite_bypasses_cached_network_payloads(self):
        with patch('backend.NetworkApi', return_value=FakeApi()) as factory, \
                patch('backend.download_part', return_value=backend.Result('C1', '成功')):
            backend.download_batch(['C1'], backend.Options(Path('unused'), overwrite=True))
        self.assertFalse(factory.call_args.kwargs['use_cache'])

    def test_component_and_model_mirrors_recover_invalid_primary_payloads(self):
        api = backend.NetworkApi(use_cache=False)
        data = json.dumps({'success': True, 'result': {'title': 'RP2040'}}).encode()
        with patch.object(api, 'fetch', side_effect=[b'<html>blocked</html>', data]) as fetch:
            self.assertEqual(api.get_cad_data_of_component('C2040')['title'], 'RP2040')
            self.assertEqual(fetch.call_args_list[0].args[0], 'https://lceda.cn/api/products/C2040/components')
            self.assertEqual(fetch.call_args_list[1].args[0], 'https://easyeda.com/api/products/C2040/components')
        with patch.object(api, 'fetch', side_effect=[b'<html>blocked</html>', STEP]) as fetch:
            self.assertEqual(api.get_step_3d_model('model-id'), STEP)
            self.assertIn('modules.lceda.cn', fetch.call_args_list[0].args[0])
            self.assertIn('modules.easyeda.com', fetch.call_args_list[1].args[0])

    def test_payload_cache_is_bounded_expires_and_does_not_cache_failures(self):
        cache = backend.PayloadCache(max_bytes=10)
        with patch('backend.time.monotonic', return_value=0):
            cache.put('old', b'123456', 5)
            cache.put('new', b'abcdef', 5)
            self.assertIsNone(cache.get('old'))
            self.assertEqual(cache.get('new'), b'abcdef')
            cache.put('oversized', b'x' * 11, 5)
            self.assertLessEqual(cache.size, 10)
        with patch('backend.time.monotonic', return_value=6):
            self.assertIsNone(cache.get('new'))
            self.assertEqual(cache.size, 0)
        api = backend.NetworkApi()
        with patch('backend.PAYLOAD_CACHE', cache), patch.object(api, 'fetch', side_effect=backend.DownloadError('offline')):
            with self.assertRaises(backend.DownloadError):
                api.get_step_3d_model('model-id')
        self.assertEqual(cache.size, 0)

    def test_valid_model_cache_avoids_network_and_still_honors_cancellation(self):
        cache = backend.PayloadCache()
        api = backend.NetworkApi()
        with patch('backend.PAYLOAD_CACHE', cache), patch.object(api, 'fetch', return_value=STEP) as fetch:
            self.assertEqual(api.get_step_3d_model('model-id'), STEP)
            self.assertEqual(api.get_step_3d_model('model-id'), STEP)
            self.assertEqual(fetch.call_count, 1)
            api.cancelled.set()
            with self.assertRaises(backend.Cancelled):
                api.get_step_3d_model('model-id')

    def test_http_connections_are_reused_and_gzip_is_decoded(self):
        clients = []
        payload = b'official model bytes' * 100

        class Handler(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'

            def do_GET(self):
                clients.append(self.client_address)
                body = gzip.compress(payload)
                self.send_response(200)
                self.send_header('Content-Length', str(len(body)))
                self.send_header('Content-Encoding', 'gzip')
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            api = backend.NetworkApi(use_cache=False)
            url = f'http://127.0.0.1:{server.server_port}/model'
            self.assertEqual(api.fetch(url), payload)
            self.assertEqual(api.fetch(url), payload)
            self.assertEqual(len(clients), 2)
            self.assertEqual(clients[0], clients[1], 'A new TCP connection was opened')
        finally:
            server.shutdown()
            server.server_close()
            thread.join(2)
