"""Real HTTP regressions for large batches and cancellable resource retries."""
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from email.utils import formatdate
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import gzip
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lcsc3d.services import backend
import urllib3
from lcsc3d.core.app_logging import close_logging, configure_logging
from test_backend import STEP, OBJ


class ResourceNetworkTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.folder = Path(temp.name)
        self.patch(patch.dict(os.environ, {'LOCALAPPDATA': str(self.folder)}))
        configure_logging('DEBUG', version='test')
        self.addCleanup(close_logging)
        self.patch(patch('lcsc3d.services.backend.proxy_for_url', return_value=None))
        self.patch(patch('lcsc3d.services.backend.RESOURCE_COOLDOWNS', backend.ResourceCooldowns()))
        # A single connection exposes a response held open during backoff.
        self.pool = urllib3.PoolManager(maxsize=1, block=True)
        self.addCleanup(self.pool.clear)
        self.patch(patch('lcsc3d.services.backend.connection_pool', return_value=self.pool))

    def patch(self, patcher):
        value = patcher.start()
        self.addCleanup(patcher.stop)
        return value

    def serve(self, respond):
        requests = []
        lock = threading.Lock()

        class Handler(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'

            def handle(self):
                try:
                    super().handle()
                except (ConnectionResetError, ConnectionAbortedError):
                    pass  # Client discards unread error bodies and pooled connections.

            def do_GET(self):
                with lock:
                    requests.append((self.path, time.monotonic()))
                    status, headers, body = respond(self.path, len(requests))
                self.send_response(status)
                self.send_header('Content-Length', str(len(body)))
                for key, value in headers.items():
                    self.send_header(key, value)
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass  # Error responses may be closed without reading the body.

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
        thread.start()

        def close():
            server.shutdown()
            server.server_close()
            thread.join(2)

        self.addCleanup(close)
        return f'http://127.0.0.1:{server.server_port}', requests

    def route_models(self, base):
        self.patch(patch('lcsc3d.services.backend.COMPONENT_ENDPOINTS', (base + '/components/{part}',)))
        self.patch(patch('lcsc3d.services.backend.ENDPOINT_3D_MODEL_STEP', base + '/step/{uuid}'))
        self.patch(patch('lcsc3d.services.backend.ENDPOINT_3D_MODEL', base + '/obj/{uuid}'))

    def test_oversized_plain_and_gzip_resources_stop_and_release_the_pool(self):
        self.patch(patch('lcsc3d.services.backend.MAX_RESOURCE_BYTES', 1024, create=True))
        bodies = {'/plain': b'x' * 4096, '/gzip': gzip.compress(b'x' * 4096), '/small': b'valid'}
        base, requests = self.serve(lambda path, count: (200, {}, bodies[path]))
        api = backend.NetworkApi()
        for path in ('/plain', '/gzip'):
            with self.subTest(path=path), self.assertRaisesRegex(backend.DownloadError, '数据过大'):
                api.fetch(base + path)
            self.assertEqual(api.fetch(base + '/small'), b'valid')
        self.assertEqual(len(requests), 4)

    def test_truncated_headerless_gzip_is_a_recoverable_download_error(self):
        base, requests = self.serve(lambda path, count: (200, {}, gzip.compress(b'component data')[:-5]))
        with self.assertRaises(backend.DownloadError):
            backend.NetworkApi().fetch(base + '/gzip')
        self.assertLessEqual(len(requests), 2)

    def test_limit_accepts_exact_size_and_concatenated_gzip_without_caching_failure(self):
        self.patch(patch('lcsc3d.services.backend.MAX_RESOURCE_BYTES', 1024))
        body = b'x' * 1024
        compressed = gzip.compress(body[:512]) + gzip.compress(body[512:])
        base, requests = self.serve(lambda path, count: (200, {'Content-Encoding': 'gzip'} if path == '/encoded' else {},
                                                      compressed if path != '/plain' else body))
        api = backend.NetworkApi()
        for path in ('/plain', '/gzip', '/encoded'):
            self.assertEqual(api.fetch(base + path), body)
        self.assertEqual(len(requests), 3)

    def test_cancellation_at_response_eof_prevents_a_successful_resource_result(self):
        stop = threading.Event()
        response = Mock(status=200, headers={})
        def read(*args, **kwargs):
            stop.set()
            return b''
        response.read.side_effect = read
        self.patch(patch('lcsc3d.services.backend.connection_pool', return_value=SimpleNamespace(request=lambda *a, **kw: response)))
        with self.assertRaises(backend.Cancelled):
            backend.NetworkApi(stop).fetch('http://127.0.0.1/empty')
        response.release_conn.assert_called()

    def test_batches_cross_29_items_and_recover_each_rate_limit_window(self):
        fixture = json.loads((Path(__file__).parent / 'fixtures/C2040.json').read_text(encoding='utf-8'))
        component = json.dumps({'success': True, 'result': fixture}).encode()
        for count, formats, workers in ((29, ('STEP',), 1), (30, ('STEP', 'OBJ'), 1),
                                         (31, ('SCHLIB', 'PCBLIB'), 1),
                                         (60, ('STEP', 'OBJ', 'SCHLIB', 'PCBLIB'), 3)):
            with self.subTest(count=count, formats=formats):
                state = {'accepted': 0, 'boundary': 29, 'until': None, 'limited': 0}

                def respond(path, _):
                    if path.startswith('/step/'):
                        return 200, {}, STEP
                    if path.startswith('/obj/'):
                        return 200, {}, OBJ.encode()
                    if state['accepted'] == state['boundary']:
                        if state['until'] is None:
                            state['until'] = time.monotonic() + 1
                        if time.monotonic() < state['until']:
                            state['limited'] += 1
                            return 429, {'Retry-After': '1'}, b'too many requests'
                        state['boundary'] += 29
                        state['until'] = None
                    state['accepted'] += 1
                    return 200, {}, component

                base, requests = self.serve(respond)
                self.route_models(base)
                parts = [f'C{index}' for index in range(1, count + 1)]
                callbacks = []
                results = backend.download_batch(parts, backend.Options(self.folder / str(count), formats, bind_3d_models=False),
                    api=backend.NetworkApi(use_cache=False), workers=workers,
                    on_result=lambda index, result: callbacks.append((index, result.part)))
                self.assertEqual([result.status for result in results], ['成功'] * count,
                                 [(result.part, result.message) for result in results if result.status != '成功'])
                self.assertEqual([result.part for result in results], parts)
                self.assertCountEqual(callbacks, list(enumerate(parts)))
                self.assertEqual(state['accepted'], count)
                self.assertEqual(state['limited'] > 0, count > 29)
                for result in results:
                    self.assertEqual(len(result.files), len(formats))
                    for filename in result.files:
                        data = Path(filename).read_bytes()
                        if filename.endswith('.step'):
                            self.assertEqual(data, STEP)
                        elif filename.endswith('.obj'):
                            self.assertEqual(data, OBJ.encode())
                        else:
                            self.assertTrue(data.startswith(bytes.fromhex('D0CF11E0A1B11AE1')))
                self.assertFalse(list((self.folder / str(count)).rglob('*.tmp')))

    def test_retry_after_seconds_is_honored_and_connection_is_released(self):
        base, requests = self.serve(lambda path, count:
            (429, {'Retry-After': '1'}, b'busy') if count == 1 else (200, {}, STEP))
        self.assertEqual(backend.NetworkApi().fetch(base + '/model'), STEP)
        self.assertEqual(len(requests), 2)
        self.assertGreaterEqual(requests[1][1] - requests[0][1], 1)

    def test_service_unavailable_retry_after_http_date(self):
        due = int(time.time()) + 2
        base, requests = self.serve(lambda path, count:
            (503, {'Retry-After': formatdate(due, usegmt=True)}, b'busy') if count == 1 else (200, {}, STEP))
        self.assertEqual(backend.NetworkApi().fetch(base + '/model'), STEP)
        self.assertEqual(len(requests), 2)
        self.assertGreaterEqual(time.time(), due)

    def test_missing_and_invalid_retry_after_recover(self):
        self.patch(patch('lcsc3d.services.backend.RESOURCE_RETRY_BACKOFF', 0.02))
        for header in (None, 'invalid date', '-3', '0'):
            with self.subTest(header=header):
                headers = {} if header is None else {'Retry-After': header}
                base, requests = self.serve(lambda path, count:
                    (429, headers, b'busy') if count == 1 else (200, {}, STEP))
                self.assertEqual(backend.NetworkApi().fetch(base + '/model'), STEP)
                self.assertEqual(len(requests), 2)
                self.assertGreater(requests[1][1] - requests[0][1], 0)

    def test_permanent_client_errors_and_optional_missing_models_are_not_retried(self):
        for status in (400, 401, 403, 404, 410):
            with self.subTest(status=status):
                base, requests = self.serve(lambda path, count: (status, {}, b'error'))
                api = backend.NetworkApi()
                if status in (404, 410):
                    self.assertIsNone(api.fetch(base + '/model', optional=True))
                else:
                    with self.assertRaisesRegex(backend.DownloadError, str(status)):
                        api.fetch(base + '/model')
                self.assertEqual(len(requests), 1)

    def test_cancel_during_retry_after_does_not_wait_or_retry(self):
        limited = threading.Event()

        def respond(path, count):
            limited.set()
            return 429, {'Retry-After': '30'}, b'busy'

        base, requests = self.serve(respond)
        cancelled = threading.Event()
        api = backend.NetworkApi(cancelled)
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(api.fetch, base + '/model')
            self.assertTrue(limited.wait(2))
            cancelled.set()
            with self.assertRaises(backend.Cancelled):
                pending.result(timeout=2)
        self.assertEqual(len(requests), 1)

    def test_cooldown_is_shared_by_api_instances_but_not_other_origins(self):
        base, requests = self.serve(lambda path, count:
            (429, {'Retry-After': '1'}, b'busy') if count == 1 else (200, {}, STEP))
        other, other_requests = self.serve(lambda path, count: (200, {}, OBJ.encode()))
        deferred = threading.Event()
        defer = backend.RESOURCE_COOLDOWNS.defer

        def observe_defer(url, delay):
            defer(url, delay)
            deferred.set()

        self.patch(patch.object(backend.RESOURCE_COOLDOWNS, 'defer', side_effect=observe_defer))
        with ThreadPoolExecutor(max_workers=3) as pool:
            first = pool.submit(backend.NetworkApi().fetch, base + '/download')
            self.assertTrue(deferred.wait(2))
            metadata = pool.submit(backend.NetworkApi().fetch, base + '/metadata')
            unrelated = pool.submit(backend.NetworkApi().fetch, other + '/preview')
            self.assertEqual(unrelated.result(timeout=2), OBJ.encode())
            self.assertFalse(metadata.done())
            self.assertEqual(first.result(timeout=3), STEP)
            self.assertEqual(metadata.result(timeout=3), STEP)
        self.assertLess(other_requests[0][1] - requests[0][1], 1)
        self.assertEqual(len(requests), 3)
        self.assertTrue(all(at - requests[0][1] >= 1 for _, at in requests[1:]))

    def test_persistent_rate_limit_has_bounded_exponential_retries(self):
        class ClockEvent:
            now = 0.0

            def __init__(self):
                self.waits = []

            def is_set(self):
                return False

            def wait(self, seconds):
                self.waits.append(seconds)
                self.now += seconds
                return False

        event = ClockEvent()
        request = Mock(side_effect=lambda *a, **kw:
            urllib3.response.HTTPResponse(status=429, body=b'busy', preload_content=False))
        self.patch(patch('lcsc3d.services.backend.connection_pool', return_value=SimpleNamespace(request=request)))
        self.patch(patch('lcsc3d.services.backend.time.monotonic', side_effect=lambda: event.now))
        with self.assertRaisesRegex(backend.DownloadError, 'HTTP 429'):
            backend.NetworkApi(event).fetch('http://127.0.0.1/model')
        self.assertEqual(request.call_count, 5)
        self.assertEqual(event.waits, [5, 10, 20, 40])

    def test_excessive_retry_after_fails_without_early_retries_or_long_waits(self):
        base, requests = self.serve(lambda path, count: (429, {'Retry-After': '3600'}, b'busy'))
        started = time.monotonic()
        with self.assertRaisesRegex(backend.DownloadError, '稍后'):
            backend.NetworkApi().fetch(base + '/model')
        with self.assertRaises(backend.DownloadError):
            backend.NetworkApi().fetch(base + '/metadata')
        self.assertEqual(len(requests), 1)
        self.assertLess(time.monotonic() - started, 2)

    def test_step_obj_and_svg_recover_without_mirror_or_placeholder(self):
        self.patch(patch('lcsc3d.services.backend.RESOURCE_RETRY_BACKOFF', 0.02))
        counts = Counter()
        svg = b'{"success":true,"result":[]}'

        def respond(path, count):
            counts[path] += 1
            if counts[path] == 1:
                return 429, {}, b'busy'
            return 200, {}, STEP if path.startswith('/step/') else OBJ.encode() if path.startswith('/obj/') else svg

        base, requests = self.serve(respond)
        self.route_models(base)
        self.patch(patch('lcsc3d.services.backend.SVG_ENDPOINTS', (base + '/svg/{part}',)))
        api = backend.NetworkApi(use_cache=False)
        self.assertEqual(api.get_step_3d_model('test-model'), STEP)
        self.assertEqual(api.get_raw_3d_model_obj('test-model'), OBJ)
        self.assertEqual(api.get_svg_data_of_component('C2040')['result'], [])
        self.assertEqual(len(requests), 6)
        self.assertEqual(list(counts.values()), [2, 2, 2])

    def test_cancelled_large_batch_preserves_completed_and_existing_files(self):
        fixture = json.loads((Path(__file__).parent / 'fixtures/C2040.json').read_text(encoding='utf-8'))
        component = json.dumps({'success': True, 'result': fixture}).encode()
        limited = threading.Event()

        def respond(path, count):
            if path == '/components/C30':
                limited.set()
                return 429, {'Retry-After': '30'}, b'busy'
            return 200, {}, STEP if path.startswith('/step/') else component

        base, requests = self.serve(respond)
        self.route_models(base)
        parts = [f'C{index}' for index in range(1, 41)]
        existing = self.folder / 'exports/RP2040_C30/previous.step'
        existing.parent.mkdir(parents=True)
        existing.write_bytes(b'previous complete file')
        cancelled = threading.Event()
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(backend.download_batch, parts, backend.Options(self.folder / 'exports', bind_3d_models=False),
                                  cancelled, api=backend.NetworkApi(cancelled, use_cache=False), workers=1)
            self.assertTrue(limited.wait(5))
            cancelled.set()
            results = pending.result(timeout=2)
        self.assertEqual([result.status for result in results], ['成功'] * 29 + ['已取消'] * 11)
        self.assertTrue(all(Path(result.files[0]).read_bytes() == STEP for result in results[:29]))
        self.assertEqual(existing.read_bytes(), b'previous complete file')
        self.assertFalse(any(path == '/components/C31' for path, _ in requests))

    def test_transient_server_errors_still_retry_only_once(self):
        for recovers in (False, True):
            with self.subTest(recovers=recovers):
                base, requests = self.serve(lambda path, count:
                    (200, {}, STEP) if recovers and count > 1 else (500, {}, b'error'))
                if recovers:
                    self.assertEqual(backend.NetworkApi().fetch(base + '/model'), STEP)
                else:
                    with self.assertRaisesRegex(backend.DownloadError, 'HTTP 500'):
                        backend.NetworkApi().fetch(base + '/model')
                self.assertEqual(len(requests), 2)


if __name__ == '__main__':
    unittest.main()
