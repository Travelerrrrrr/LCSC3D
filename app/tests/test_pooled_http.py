"""Persistent sockets must not share account cookies or replay requests."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import unittest
import socket
import ssl
from unittest.mock import Mock, patch
import urllib3

from test_store_network import IsolatedDiagnostics
from lcsc3d.core.app_settings import Preferences, get_preferences, set_preferences
from lcsc3d.services.store import MemorySession, StoreError, SessionExpired


class PooledStoreTests(IsolatedDiagnostics):
    def test_pool_errors_preserve_dns_tls_and_timeout_diagnostics(self):
        for error, message in (
            (urllib3.exceptions.NameResolutionError('invalid', None, socket.gaierror('unavailable')), '域名解析失败'),
            (urllib3.exceptions.SSLError(ssl.SSLCertVerificationError('untrusted')), '安全证书校验失败'),
            (urllib3.exceptions.ConnectTimeoutError('timeout'), '连接超时')):
            pool = Mock()
            pool.request.side_effect = error
            with self.subTest(error=type(error).__name__), \
                    patch('lcsc3d.services.pooled_http.connection_pool', return_value=pool), \
                    self.assertRaisesRegex(StoreError, message):
                MemorySession().request(self.origin + '/diagnostic')
    def setUp(self):
        super().setUp()
        self.addCleanup(set_preferences, get_preferences())
        set_preferences(Preferences(store_proxy='direct'))
        self.requests = []
        rows = self.requests
        class Handler(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'
            def do_GET(self):
                rows.append((self.client_address[1], self.path, self.headers.get('Cookie', '')))
                if self.path == '/redirect':
                    self.send_response(302)
                    self.send_header('Location', '/complete')
                    self.send_header('Set-Cookie', 'redirected=yes; Path=/')
                    body = b''
                else:
                    self.send_response(401 if self.path == '/unauthorized' else 503 if self.path == '/busy' else 200)
                    self.send_header('Set-Cookie', 'session=private-fixture; Path=/')
                    self.send_header('Set-Cookie', 'second=preserved; Path=/')
                    body = json.dumps({'path': self.path}).encode()
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            def do_POST(self):
                self.rfile.read(int(self.headers.get('Content-Length', '0')))
                self.do_GET()
            def log_message(self, *args):
                pass
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.worker = threading.Thread(target=lambda: self.server.serve_forever(poll_interval=.01), daemon=True)
        self.worker.start()
        self.origin = f'http://127.0.0.1:{self.server.server_port}'
        self.addCleanup(self.stop)

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.worker.join(2)

    def test_reuses_connection_retains_duplicate_cookie_headers_and_isolates_sessions(self):
        first, second = MemorySession(), MemorySession()
        for path in ('/one', '/two', '/three'):
            first.request(self.origin + path)
        second.request(self.origin + '/independent')
        self.assertEqual(len({row[0] for row in self.requests}), 1)
        self.assertEqual(self.requests[0][2], '')
        self.assertIn('session=private-fixture', self.requests[2][2])
        self.assertIn('second=preserved', self.requests[2][2])
        self.assertEqual(self.requests[3][2], '')
        first.clear()
        first.request(self.origin + '/after-logout')
        self.assertEqual(self.requests[-1][2], '')

    def test_redirect_cookies_and_url_are_preserved_and_http_errors_never_replay_posts(self):
        session = MemorySession()
        body, url = session.request(self.origin + '/redirect')
        self.assertEqual(url, self.origin + '/complete')
        self.assertEqual(json.loads(body)['path'], '/complete')
        self.assertIn('redirected=yes', self.requests[-1][2])
        self.assertEqual(len({row[0] for row in self.requests}), 1)
        with self.assertRaises(SessionExpired):
            session.request(self.origin + '/unauthorized')
        with self.assertRaises(StoreError):
            session.request(self.origin + '/busy', method='POST', data=b'one-attempt')
        self.assertEqual(sum(path == '/busy' for _, path, _ in self.requests), 1)
