"""Exercise verified staging and replacement/rollback without updating this app."""
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import updater
from errors import Cancelled
from app_logging import configure_logging, close_logging

NEW = b'MZ' + b'new executable payload' * 100
OLD = b'MZoriginal executable'
SHA = hashlib.sha256(NEW).hexdigest()
PREFIX = f'https://github.com/{updater.REPOSITORY}/releases/download/v2.1.0/'


def release_data():
    return {'tag_name': 'v2.1.0', 'draft': False, 'prerelease': False, 'body': 'Release notes',
            'assets': [{'name': 'LCSC3D.exe', 'browser_download_url': PREFIX + 'LCSC3D.exe',
                        'size': len(NEW), 'digest': 'sha256:' + SHA},
                       {'name': 'SHA256SUMS.txt', 'browser_download_url': PREFIX + 'SHA256SUMS.txt'}]}


class Response(io.BytesIO):
    status = 200
    headers = {}

    def read(self, size=-1, decode_content=True):
        return super().read(size)

    def release_conn(self):
        pass


class UpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='更新 测试 ')
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name).resolve()
        environment = patch.dict(os.environ, {'LOCALAPPDATA': str(self.directory / '用户数据')})
        environment.start()
        self.addCleanup(environment.stop)
        configure_logging(directory=self.directory / '用户数据' / 'LCSC3D' / 'logs')
        self.addCleanup(close_logging)
        self.update_directory = self.directory / '用户数据' / 'LCSC3D' / 'updates'
        self.target = self.directory / '重命名的程序.exe'
        self.target.write_bytes(OLD)
        self.client = updater.UpdateClient()
        self.release = updater.parse_release(release_data(), '2.0.0')

    def open(self, url):
        if url == updater.LATEST_API:
            return Response(json.dumps(release_data()).encode())
        if url.endswith('SHA256SUMS.txt'):
            return Response((SHA + '  LCSC3D.exe\n').encode())
        return Response(NEW)

    def stage(self):
        with patch.object(self.client, '_open', side_effect=self.open):
            return self.client.download(self.release, self.target)

    def test_numeric_version_comparison_and_older_release(self):
        self.assertGreater(updater.version_tuple('v2.10.0'), updater.version_tuple('2.9.9'))
        self.assertIsNone(updater.parse_release(release_data(), '2.1.0'))
        self.assertIsNone(updater.parse_release(release_data(), '3.0.0'))
        for version in ('2.1', 'v2.1.0-beta', None, '2.1.0/../x'):
            with self.subTest(version=version), self.assertRaises(updater.UpdateError):
                updater.version_tuple(version)

    def test_only_stable_release_with_exact_repository_assets_is_accepted(self):
        for change in ({'draft': True}, {'prerelease': True}, {'assets': []}, {'tag_name': 'v2.1.0-rc1'}):
            data = release_data()
            data.update(change)
            with self.subTest(change=change), self.assertRaises(updater.UpdateError):
                updater.parse_release(data, '2.0.0')
        data = release_data()
        data['assets'][0]['browser_download_url'] = 'https://github.com/someone/other/releases/download/v2.1.0/LCSC3D.exe'
        with self.assertRaises(updater.UpdateError):
            updater.parse_release(data, '2.0.0')

    def test_demo_can_upgrade_to_same_version_stable_without_downgrading(self):
        self.assertEqual(updater.parse_release(release_data(), '2.1.0-demo.1').version, '2.1.0')
        self.assertIsNone(updater.parse_release(release_data(), '2.2.0-demo.1'))
        self.assertIsNone(updater.parse_release(release_data(), '2.1.0'))
        with self.assertRaises(updater.UpdateError):
            updater.parse_release(release_data(), '2.1.0-malformed')

    def test_check_uses_release_api_and_rejects_malformed_responses(self):
        with patch.object(self.client, '_open', side_effect=self.open):
            self.assertEqual(self.client.check('2.0.0').version, '2.1.0')
        with patch.object(self.client, '_open', return_value=Response(b'<html>error</html>')):
            with self.assertRaises(updater.UpdateError):
                self.client.check('2.0.0')

    def test_redirect_cannot_downgrade_https_or_change_to_an_untrusted_host(self):
        for location in ('http://github.com/file', 'https://unrelated.example/file'):
            response = Response(b'')
            response.status = 302
            response.headers = {'Location': location}
            with patch('updater.connection_pool', return_value=SimpleNamespace(request=lambda *a, **k: response)), \
                    self.subTest(location=location), self.assertRaises(updater.UpdateError):
                self.client._open(PREFIX + 'LCSC3D.exe')

    def test_checksum_requires_unique_exact_exe_entry(self):
        self.assertEqual(updater.checksum_for_exe((SHA.upper() + ' *LCSC3D.exe\r\n').encode()), SHA)
        for raw in ((SHA + '  other.exe').encode(), (SHA + '  LCSC3D.exe\n').encode() * 2, b'bad'):
            with self.subTest(raw=raw), self.assertRaises(updater.UpdateError):
                updater.checksum_for_exe(raw)

    def test_verified_download_stages_without_touching_running_executable(self):
        progress = []
        with patch.object(self.client, '_open', side_effect=self.open):
            manifest = self.client.download(self.release, self.target, lambda done, total: progress.append((done, total)))
        self.assertEqual(self.target.read_bytes(), OLD)
        self.assertEqual((manifest.parent / 'new.exe').read_bytes(), NEW)
        plan = json.loads(manifest.read_text(encoding='utf-8'))
        self.assertEqual(plan['target'], str(self.target))
        self.assertEqual(plan['sha256'], SHA)
        self.assertEqual(progress[-1], (len(NEW), len(NEW)))
        self.assertEqual(manifest.parent.parent, self.update_directory)
        self.assertFalse(list(self.directory.glob(updater.STAGE_PREFIX + '*')))

    def test_corrupt_or_truncated_download_cleans_stage_and_preserves_original(self):
        for payload in (NEW[:-1], NEW + b'extra', b'MZ' + b'x' * (len(NEW)-2)):
            def open_payload(url):
                return self.open(url) if url.endswith('.txt') else Response(payload)
            with patch.object(self.client, '_open', side_effect=open_payload), \
                    self.subTest(length=len(payload)), self.assertRaises(updater.UpdateError):
                self.client.download(self.release, self.target)
            self.assertEqual(self.target.read_bytes(), OLD)
            self.assertEqual(list(self.update_directory.glob(updater.STAGE_PREFIX + '*')), [])

    def test_release_digest_disagreement_never_downloads_executable(self):
        release = updater.Release('2.1.0', '', PREFIX+'LCSC3D.exe', PREFIX+'SHA256SUMS.txt', len(NEW), digest='f'*64)
        with patch.object(self.client, '_open', side_effect=self.open) as request, self.assertRaises(updater.UpdateError):
            self.client.download(release, self.target)
        self.assertEqual(request.call_count, 1)
        self.assertEqual(self.target.read_bytes(), OLD)

    def test_cancellation_before_check_and_during_download(self):
        self.client.cancelled.set()
        with patch('updater.connection_pool') as pool, self.assertRaises(Cancelled):
            self.client.check('2.0.0')
        pool.assert_not_called()
        self.client.cancelled.clear()
        def cancel(done, total):
            if done:
                self.client.cancelled.set()
        with patch.object(self.client, '_open', side_effect=self.open), self.assertRaises(Cancelled):
            self.client.download(self.release, self.target, cancel)
        self.assertEqual(self.target.read_bytes(), OLD)
        self.assertFalse(list(self.update_directory.glob(updater.STAGE_PREFIX + '*')))

    def test_helper_replaces_and_restarts_after_startup_ack(self):
        manifest = self.stage()
        child = SimpleNamespace(poll=lambda: None, pid=1)
        def spawn(arguments, directory):
            updater.acknowledge_update(manifest, self.target)
            return child
        with patch('updater._wait_for_parent') as wait, patch('updater._spawn', side_effect=spawn) as start:
            self.assertEqual(updater.apply_update(manifest), 0)
        wait.assert_called_once()
        self.assertEqual(self.target.read_bytes(), NEW)
        self.assertEqual((manifest.parent / 'previous.exe').read_bytes(), OLD)
        self.assertEqual(start.call_args.args[0], [str(self.target), '--update-ack', str(manifest)])

    def test_failed_startup_restores_and_restarts_original(self):
        manifest = self.stage()
        child = SimpleNamespace(poll=lambda: 1)
        with patch('updater._wait_for_parent'), patch('updater._spawn', return_value=child) as start, \
                patch('updater._wait_for_ack', return_value=False), patch('updater._show_failure'):
            self.assertEqual(updater.apply_update(manifest), 1)
        self.assertEqual(self.target.read_bytes(), OLD)
        self.assertEqual(start.call_args.args[0], [str(self.target)])
        result = json.loads((manifest.parent / 'result.json').read_text(encoding='utf-8'))
        self.assertEqual(result['status'], 'failed')

    def test_changed_stage_or_original_prevents_replacement(self):
        for changed in ('new.exe', 'target'):
            manifest = self.stage()
            path = self.target if changed == 'target' else manifest.parent / changed
            path.write_bytes(b'changed')
            with patch('updater._wait_for_parent'), patch('updater._spawn') as start, patch('updater._show_failure'):
                self.assertEqual(updater.apply_update(manifest), 1)
            start.assert_not_called()
            self.assertFalse((manifest.parent / 'previous.exe').exists())
            self.target.write_bytes(OLD)

    def test_plan_rejects_target_outside_original_directory(self):
        manifest = self.stage()
        plan = json.loads(manifest.read_text(encoding='utf-8'))
        plan['target'] = str(self.directory.parent / 'outside.exe')
        manifest.write_text(json.dumps(plan), encoding='utf-8')
        with self.assertRaises(updater.UpdateError):
            updater._load_plan(manifest)
        self.assertEqual(self.target.read_bytes(), OLD)

    def test_source_mode_does_not_launch_an_updater(self):
        manifest = self.stage()
        with patch('updater._spawn') as start, patch.object(sys, 'frozen', False, create=True), self.assertRaises(updater.UpdateError):
            updater.launch_update(manifest)
        start.assert_not_called()

    def test_completed_update_can_be_cleaned_while_failure_backup_is_kept(self):
        completed = self.stage()
        failed = self.stage()
        (completed.parent / 'result.json').write_text('{"status":"success"}')
        (failed.parent / 'result.json').write_text('{"status":"failed"}')
        updater.cleanup_updates(self.update_directory)
        self.assertFalse(completed.parent.exists())
        self.assertTrue(failed.parent.exists())

    def test_plan_rejects_stage_outside_app_data(self):
        manifest = self.stage()
        external = self.directory / (updater.STAGE_PREFIX + 'external')
        external.mkdir()
        outside = external / 'plan.json'
        outside.write_bytes(manifest.read_bytes())
        with self.assertRaises(updater.UpdateError):
            updater._load_plan(outside)

    def test_legacy_update_plan_is_supported_without_creating_new_sidecars(self):
        stage = self.directory / (updater.STAGE_PREFIX + 'legacy')
        stage.mkdir()
        manifest = stage / 'plan.json'
        plan = {'target': str(self.target), 'sha256': SHA, 'original_sha256': hashlib.sha256(OLD).hexdigest(),
                'parent_pid': 1, 'nonce': 'a' * 48, 'version': '2.1.0'}
        manifest.write_text(json.dumps(plan), encoding='utf-8')
        self.assertEqual(updater._load_plan(manifest)[2], self.target)


if __name__ == '__main__':
    unittest.main()
