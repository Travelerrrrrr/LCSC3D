"""Loopback-only release fixture. Serves only one EXE and its update metadata."""
from contextlib import contextmanager
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import threading
import time


class LocalReleaseServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, executable, version, *, scenario='success', delay_ms=0):
        if not re.fullmatch(r'\d+\.\d+\.\d+', version):
            raise ValueError('Expected a numeric X.Y.Z version')
        if scenario not in ('success', 'bad-checksum', 'no-update'):
            raise ValueError('Unknown test scenario')
        self.executable = Path(executable).resolve(strict=True)
        self.version, self.scenario = version, scenario
        self.size = self.executable.stat().st_size
        with self.executable.open('rb') as stream:
            self.digest = hashlib.file_digest(stream, 'sha256').hexdigest()
        self.delay = max(0, min(delay_ms, 1000)) / 1000
        self.requests = []
        super().__init__(('127.0.0.1', 0), ReleaseHandler)
        self.origin = f'http://127.0.0.1:{self.server_port}'
        self.prefix = f'/releases/download/v{version}/'

    @property
    def published_digest(self):
        return ('0' if self.digest[0] != '0' else '1') + self.digest[1:] if self.scenario == 'bad-checksum' else self.digest

    def release(self):
        return {'tag_name': 'v' + self.version, 'draft': False, 'prerelease': False,
                'body': '本地模拟更新测试。使用测试副本，未发布到 GitHub。\n'
                        '默认更新包与启动程序相同，模拟版本只用于验证更新流程。',
                'assets': [{'name': 'LCSC3D.exe', 'size': self.size,
                            'browser_download_url': self.origin + self.prefix + 'LCSC3D.exe',
                            'digest': 'sha256:' + self.published_digest},
                           {'name': 'SHA256SUMS.txt',
                            'browser_download_url': self.origin + self.prefix + 'SHA256SUMS.txt'}]}


class ReleaseHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        server = self.server
        if self.headers.get('Host') != f'127.0.0.1:{server.server_port}':
            self.send_error(400)
            return
        if self.path == '/latest.json':
            data, kind = json.dumps(server.release(), ensure_ascii=False).encode(), 'application/json; charset=utf-8'
        elif self.path == server.prefix + 'SHA256SUMS.txt':
            data, kind = (server.published_digest + '  LCSC3D.exe\n').encode(), 'text/plain'
        elif self.path == server.prefix + 'LCSC3D.exe':
            data, kind = None, 'application/octet-stream'
        elif self.path in ('/', '/releases/tag/v' + server.version):
            data, kind = ('LCSC3D 本地更新测试\n模拟发行版本：' + server.version).encode(), 'text/plain; charset=utf-8'
        else:
            self.send_error(404)
            return
        server.requests.append(self.path)
        self.send_response(200)
        self.send_header('Content-Type', kind)
        self.send_header('Content-Length', str(server.size if data is None else len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        try:
            if data is not None:
                self.wfile.write(data)
            else:
                with server.executable.open('rb') as stream:
                    while block := stream.read(256 * 1024):
                        self.wfile.write(block)
                        if server.delay:
                            time.sleep(server.delay)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass


@contextmanager
def serve_release(executable, version, **options):
    server = LocalReleaseServer(executable, version, **options)
    thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .05}, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
