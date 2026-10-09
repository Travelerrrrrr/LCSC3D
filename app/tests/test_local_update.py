"""Real loopback HTTP updates remain isolated from production URL validation."""
from contextlib import ExitStack
import io
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'app'))
sys.path.insert(0, str(ROOT / 'scripts'))
import updater
from app_logging import configure_logging, close_logging
from local_update_server import serve_release


class LocalUpdateTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.folder = Path(self.stack.enter_context(tempfile.TemporaryDirectory())).resolve()
        self.stack.enter_context(patch.dict(os.environ, {'LOCALAPPDATA': str(self.folder / 'profile')}))
        configure_logging(directory=self.folder / 'logs')
        self.addCleanup(close_logging)
        self.target = self.folder / 'LCSC3D.exe'
        self.target.write_bytes(b'MZoriginal')
        self.candidate = self.folder / 'candidate.exe'
        self.candidate.write_bytes(b'MZreplacement' * 30000)

    def server(self, scenario='success'):
        return self.stack.enter_context(serve_release(self.candidate, '2.1.1', scenario=scenario))

    def stage(self):
        server = self.server()
        source = updater.LocalUpdateSource(server.origin, '2.1.0')
        client = updater.UpdateClient(source=source)
        return client.download(client.check('2.1.1'), self.target), source

    def test_local_source_accepts_only_an_explicit_numeric_loopback_origin(self):
        self.assertEqual(updater.LocalUpdateSource('http://127.0.0.1:8765/').origin, 'http://127.0.0.1:8765')
        for value in ('http://localhost:8765', 'http://192.168.1.2:8765', 'https://127.0.0.1:8765',
                      'http://127.0.0.1', 'http://127.0.0.1:0', 'http://127.0.0.1:99999',
                      'http://user:pass@127.0.0.1:8765', 'http://127.0.0.1:8765/a',
                      'http://127.0.0.1:8765?x=y', 'http://127.0.0.1:8765#fragment', None):
            with self.subTest(value=value), self.assertRaises(updater.UpdateError):
                updater.LocalUpdateSource(value)
        with self.assertRaises(updater.UpdateError):
            updater.LocalUpdateSource('http://127.0.0.1:8765', '2.1.1-beta')

    def test_check_download_hash_and_plan_use_real_local_http_without_proxy(self):
        server = self.server()
        source = updater.LocalUpdateSource(server.origin, '2.1.0')
        client = updater.UpdateClient(source=source)
        progress = []
        with patch('updater.proxy_for_url', side_effect=AssertionError('Local updates must bypass proxies')):
            release = client.check('2.1.1')
            plan = client.download(release, self.target, lambda done, total: progress.append((done, total)))
        self.assertEqual(release.version, '2.1.1')
        self.assertEqual((plan.parent / 'new.exe').read_bytes(), self.candidate.read_bytes())
        self.assertEqual(self.target.read_bytes(), b'MZoriginal')
        self.assertEqual(progress[-1], (server.size, server.size))
        self.assertEqual(json.loads(plan.read_text())['local_test'], {'origin': server.origin, 'current_version': '2.1.0'})
        self.assertEqual(server.requests, ['/latest.json', server.prefix + 'SHA256SUMS.txt', server.prefix + 'LCSC3D.exe'])

    def test_restarted_comparison_uses_real_version_so_same_build_is_up_to_date(self):
        server = self.server()
        client = updater.UpdateClient(source=updater.LocalUpdateSource(server.origin))
        self.assertIsNone(client.check('2.1.1'))
        self.assertEqual(server.requests, ['/latest.json'])

    def test_production_client_rejects_loopback_and_does_not_pick_up_test_environment(self):
        with patch.dict(os.environ, {'LCSC3D_UPDATE_SOURCE': 'http://127.0.0.1:8765'}):
            client = updater.UpdateClient()
        self.assertIsNone(client.source)
        with patch('updater.connection_pool') as pool, self.assertRaises(updater.UpdateError):
            client._open('http://127.0.0.1:8765/latest.json')
        pool.assert_not_called()

    def test_local_metadata_cannot_switch_asset_to_remote_or_another_port(self):
        server = self.server()
        source = updater.LocalUpdateSource(server.origin)
        for prefix in ('https://github.com/Travelerrrrrr/LCSC3D', 'http://127.0.0.1:1', 'http://localhost:8765'):
            release = server.release()
            release['assets'][0]['browser_download_url'] = prefix + server.prefix + 'LCSC3D.exe'
            with self.subTest(prefix=prefix), self.assertRaises(updater.UpdateError):
                updater.parse_release(release, '2.1.0', source=source)

    def test_local_redirect_cannot_escape_to_any_other_origin(self):
        class Response(io.BytesIO):
            status = 302
            def release_conn(self):
                pass
        source = updater.LocalUpdateSource('http://127.0.0.1:8765')
        for location in ('https://github.com/file', 'http://127.0.0.1:8766/file',
                         'http://localhost:8765/file', 'http://user@127.0.0.1:8765/file'):
            response = Response()
            response.headers = {'Location': location}
            pool = SimpleNamespace(request=lambda *a, **kw: response)
            with patch('updater.connection_pool', return_value=pool), self.subTest(location=location), self.assertRaises(updater.UpdateError):
                updater.UpdateClient(source=source)._open(source.origin + '/latest.json')

    def test_local_corrupt_checksum_preserves_original_and_removes_staging(self):
        server = self.server('bad-checksum')
        client = updater.UpdateClient(source=updater.LocalUpdateSource(server.origin, '2.1.0'))
        release = client.check('2.1.1')
        with self.assertRaisesRegex(updater.UpdateError, 'SHA-256'):
            client.download(release, self.target)
        self.assertEqual(self.target.read_bytes(), b'MZoriginal')
        self.assertFalse(list(updater.updates_directory().iterdir()))

    def test_server_exposes_no_arbitrary_files_or_other_host_headers(self):
        server = self.server()
        client = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        for path in ('/../candidate.exe', '/store-session.bin', '/LCSC3D.exe', '/latest.json?path=secret'):
            with self.subTest(path=path), self.assertRaises(urllib.error.HTTPError) as caught:
                client.open(server.origin + path)
            self.assertEqual(caught.exception.code, 404)
        with self.assertRaises(urllib.error.HTTPError) as caught:
            client.open(urllib.request.Request(server.origin + '/latest.json', headers={'Host': 'external.example'}))
        self.assertEqual(caught.exception.code, 400)

    def test_helper_preserves_local_source_and_drops_simulated_version_after_restart(self):
        manifest, source = self.stage()
        process = SimpleNamespace(poll=lambda: None, pid=123)
        def spawn(arguments, directory):
            updater.acknowledge_update(manifest, self.target)
            return process
        with patch('updater._wait_for_parent'), patch('updater._spawn', side_effect=spawn) as launched:
            self.assertEqual(updater.apply_update(manifest), 0)
        self.assertEqual(launched.call_args.args[0], [str(self.target), '--update-ack', str(manifest),
                                                     '--local-update-source', source.origin])
        self.assertEqual(self.target.read_bytes(), self.candidate.read_bytes())

    def test_failed_local_restart_rolls_back_with_original_test_comparison(self):
        manifest, source = self.stage()
        with patch('updater._wait_for_parent'), patch('updater._spawn', return_value=SimpleNamespace(poll=lambda: 1)) as launched, \
                patch('updater._wait_for_ack', return_value=False), patch('updater._show_failure'):
            self.assertEqual(updater.apply_update(manifest), 1)
        self.assertEqual(self.target.read_bytes(), b'MZoriginal')
        self.assertEqual(launched.call_args.args[0], [str(self.target), *source.arguments()])

    def test_invalid_local_plan_is_rejected_before_replacing_files(self):
        manifest, _ = self.stage()
        plan = json.loads(manifest.read_text())
        for local in ({'origin': 'https://external.example', 'current_version': '2.1.0'},
                      {'origin': 'http://127.0.0.1:8765', 'current_version': '2.1.0', 'arguments': ['--other']}):
            plan['local_test'] = local
            manifest.write_text(json.dumps(plan))
            with self.assertRaises(updater.UpdateError):
                updater._load_plan(manifest)
        self.assertEqual(self.target.read_bytes(), b'MZoriginal')
