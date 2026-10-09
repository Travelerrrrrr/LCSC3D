"""Login failures retain useful diagnostics without leaking session data."""
import gzip
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import socket
import ssl
import sys
import tempfile
import threading
import unittest
import urllib.error
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from errors import Cancelled
from favorites import RequestWorker
from store import MemorySession, PASSPORT, StoreClient, StoreError
from store_diagnostics import MAX_LOG_BYTES, record_request_error
from app_logging import configure_logging, close_logging


class IsolatedDiagnostics(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        environment = patch.dict(os.environ, {'LOCALAPPDATA': folder.name})
        environment.start()
        self.addCleanup(environment.stop)
        self.log = self.folder / 'LCSC3D' / 'logs' / 'LCSC3D.log'
        configure_logging('ERROR', directory=self.log.parent)
        self.addCleanup(close_logging)


class StoreNetworkTests(IsolatedDiagnostics):
    def start_broken_server(self, mode):
        requests = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                self.rfile.read(int(self.headers.get('Content-Length', 0)))
                requests.append(self.path)
                self.send_response(200)
                if mode == 'chunked':
                    self.send_header('Transfer-Encoding', 'chunked')
                    self.end_headers()
                    self.wfile.write(b'10\r\n{"code":200}')
                else:
                    body = gzip.compress(b'{"code":200,"data":null}')[:-5]
                    self.send_header('Content-Encoding', 'gzip')
                    self.send_header('Content-Length', str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                self.wfile.flush()
                self.close_connection = True

            do_GET = do_POST

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
        thread.start()

        def close():
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        self.addCleanup(close)
        base = f'http://127.0.0.1:{server.server_port}'

        class LoopbackSession(MemorySession):
            def request(self, url, **kwargs):
                from urllib.parse import urlsplit
                path = urlsplit(url).path
                body, _ = super().request(base + path, **kwargs)
                return body, url

        return LoopbackSession, requests

    def test_incomplete_http_response_is_reported_for_qr_and_sms_without_retry(self):
        session_type, requests = self.start_broken_server('chunked')
        for action in ('qr', 'sms'):
            client = StoreClient(session_type())
            with self.subTest(action=action), self.assertRaisesRegex(StoreError, '网络响应异常或不完整'):
                if action == 'qr':
                    client.start_qr()
                else:
                    client.send_sms('13800000000')
            self.assertIsNone(client.account)
        self.assertEqual(requests, ['/api/cas/login/get-official-qrcode', '/api/cas/config/get-public-key'])
        entries = [json.loads(line) for line in self.log.read_text(encoding='utf-8').splitlines()]
        self.assertEqual([entry['error'] for entry in entries if entry['event'] == 'request.failed'],
                         ['IncompleteRead', 'IncompleteRead'])
        self.assertTrue(any(entry['event'] == 'login.sms_send.failed' for entry in entries))
        self.assertTrue(any(entry['event'] == 'login.qr_create.failed' for entry in entries))

    def test_truncated_gzip_reply_has_a_specific_message(self):
        session_type, requests = self.start_broken_server('gzip')
        with self.assertRaisesRegex(StoreError, '压缩响应损坏或不完整'):
            StoreClient(session_type()).start_qr()
        self.assertEqual(len(requests), 1)

    def test_network_failures_identify_the_login_stage_and_hide_private_details(self):
        sensitive = 'PRIVATE-AUTH-CODE-password-cookie'
        cases = [(urllib.error.URLError(socket.gaierror(-2, sensitive)), '域名解析失败'),
                 (urllib.error.URLError(ssl.SSLCertVerificationError(1, sensitive)), '安全证书校验失败'),
                 (urllib.error.URLError(ssl.SSLError(1, sensitive)), '安全连接建立失败'),
                 (urllib.error.URLError(TimeoutError(sensitive)), '连接超时'),
                 (http.client.BadStatusLine(sensitive), '网络响应异常或不完整'),
                 (http.client.InvalidURL(sensitive), '代理配置无效'),
                 (ValueError(sensitive), '代理配置无效')]
        for error, expected in cases:
            session = MemorySession()
            with self.subTest(error=type(error).__name__), \
                    patch.object(session.opener, 'open', side_effect=error) as opened, \
                    self.assertRaises(StoreError) as caught:
                session.request(PASSPORT + '/api/cas/login/get-official-qrcode?code=' + sensitive,
                                method='POST', data=b'{}')
            self.assertIn('获取登录二维码失败', str(caught.exception))
            self.assertIn(expected, str(caught.exception))
            self.assertNotIn(sensitive, str(caught.exception))
            self.assertEqual(opened.call_count, 1)
        self.assertNotIn(sensitive, self.log.read_text(encoding='utf-8'))

    def test_cancellation_and_response_size_limit_remain_distinct(self):
        session = MemorySession()
        stop = threading.Event()
        stop.set()
        with patch.object(session.opener, 'open') as opened, self.assertRaises(Cancelled):
            session.request(PASSPORT, cancelled=stop)
        opened.assert_not_called()
        response = MagicMock()
        response.headers = {}
        response.read.side_effect = [b'too large', b'']
        response.__enter__.return_value = response
        with patch.object(session.opener, 'open', return_value=response), \
                self.assertRaisesRegex(StoreError, '数据过大'):
            session.request(PASSPORT, limit=2)
        entries = [json.loads(line) for line in self.log.read_text(encoding='utf-8').splitlines()]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]['event'], 'store.http.failed')
        self.assertEqual(entries[0]['error'], 'StoreError')


class StoreDiagnosticsTests(IsolatedDiagnostics):
    def test_unexpected_worker_error_has_a_code_location_but_no_secret_or_locals(self):
        sensitive = 'PRIVATE-password-cookie-authCode'

        def operation(stop, progress):
            password = sensitive
            raise ValueError('https://passport.jlc.com/login?code=' + password)

        results = []
        worker = RequestWorker('qr', 1, operation, None)
        worker.loaded.connect(lambda value, error: results.append((value, error)))
        worker.run()
        self.assertEqual(len(results), 1)
        self.assertIn('ValueError', str(results[0][1]))
        self.assertIn('设置', str(results[0][1]))
        self.assertNotIn(sensitive, str(results[0][1]))
        content = self.log.read_text(encoding='utf-8')
        self.assertNotIn(sensitive, content)
        self.assertNotIn('https://', content)
        self.assertNotIn(str(self.folder), content)
        entry = json.loads(content)
        self.assertEqual(entry['operation'], 'qr')
        self.assertEqual(entry['frames'][-1]['function'], 'operation')
        self.assertEqual(entry['frames'][-1]['file'], Path(__file__).name)

    def test_diagnostic_log_is_bounded_and_keeps_the_latest_error(self):
        self.log.parent.mkdir(parents=True, exist_ok=True)
        close_logging()
        self.log.write_bytes(b'x' * MAX_LOG_BYTES)
        configure_logging('ERROR', directory=self.log.parent)
        self.assertTrue(record_request_error(ValueError('private value'), 'qr'))
        self.assertLess(self.log.stat().st_size, MAX_LOG_BYTES)
        self.assertEqual(self.log.with_suffix('.log.1').stat().st_size, MAX_LOG_BYTES)
        self.assertEqual(json.loads(self.log.read_text(encoding='utf-8'))['error'], 'ValueError')

    def test_read_only_profile_does_not_mask_the_request_error(self):
        close_logging()
        with patch('app_logging.SafeFileHandler', side_effect=PermissionError('private profile path')):
            self.assertFalse(record_request_error(ValueError('private input'), 'qr'))

    def test_cancelled_worker_produces_no_result_or_diagnostics(self):
        results = []
        worker = RequestWorker('qr', 1, lambda stop, progress: (_ for _ in ()).throw(ValueError('private')), None)
        worker.cancelled.set()
        worker.loaded.connect(lambda value, error: results.append((value, error)))
        worker.run()
        self.assertEqual(results, [])
        self.assertEqual(self.log.read_text(encoding='utf-8'), '')


if __name__ == '__main__':
    unittest.main()
