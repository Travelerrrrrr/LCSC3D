"""Cancellation undoes the entire batch across model and library export modes."""
import itertools
import json
from pathlib import Path
import sys
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lcsc3d.services import backend
from lcsc3d.services.output_transaction import OutputTransaction, BatchCancellation
from lcsc3d.core import app_logging as logs
from lcsc3d.eda import kicad_export
from test_export_modes import ExportFixture


def snapshot(root):
    return {str(path.relative_to(root)): path.read_bytes() for path in root.rglob('*') if path.is_file()}


class DownloadCancellationTests(ExportFixture):
    def run_batch(self, options, **kwargs):
        return backend.download_batch(['C2040', 'C20197'], options, self.api.cancelled,
                                      api=self.api, workers=1, **kwargs)

    def assert_cancelled(self, rows):
        self.assertTrue(all(row.status == '已取消' for row in rows), [(r.part, r.status, r.message) for r in rows])
        self.assertTrue(all(not r.files and not r.pending_formats and not r.completed_formats for r in rows))

    def test_cancel_after_completed_part_removes_whole_batch_and_keeps_unrelated_file(self):
        for formats in (('STEP',), ('OBJ',), ('STEP', 'OBJ'), ('SCHLIB',), ('PCBLIB',), ('SCHLIB', 'PCBLIB')):
            with self.subTest(formats=formats):
                self.api.cancelled.clear()
                root = self.root / '-'.join(formats)
                root.mkdir()
                (root / 'user.txt').write_bytes(b'unrelated')
                before = snapshot(root)
                published = []
                def publish(index, result):
                    published.append((index, result.status, result.files[:]))
                    if result.status == '成功':
                        self.api.cancelled.set()
                rows = self.run_batch(backend.Options(root, formats, bind_3d_models=False), on_result=publish)
                self.assert_cancelled(rows)
                self.assertEqual(snapshot(root), before)
                self.assertEqual([r for r in root.iterdir() if r.is_dir()], [])
                self.assertEqual([state for _, state, _ in published[-2:]], ['已取消', '已取消'])

    def test_cancel_between_formats_restores_overwritten_file_and_partial_results(self):
        root = self.root / 'mixed'
        options = backend.Options(root, ('STEP', 'OBJ'), bind_3d_models=False)
        rows = self.run_batch(options)
        self.assertTrue(all(row.status == '成功' for row in rows))
        for path in root.rglob('*'):
            if path.is_file():
                path.write_bytes(b'previous user file')
        before = snapshot(root)
        original = backend.atomic_write
        def write(path, payload):
            original(path, payload)
            self.api.cancelled.set()
        with patch.object(backend, 'atomic_write', side_effect=write):
            self.assert_cancelled(self.run_batch(options))
        self.assertEqual(snapshot(root), before)

    def test_ad_merge_append_models_and_individual_copy_cancellation_matrix(self):
        for mode, formats, models, keep in itertools.product(
                ('merge', 'append'), (('SCHLIB',), ('PCBLIB',), ('SCHLIB', 'PCBLIB')),
                ((), ('STEP',), ('OBJ',), ('STEP', 'OBJ')), (False, True)):
            with self.subTest(mode=mode, formats=formats, models=models, keep=keep):
                self.api.cancelled.clear()
                root = self.root / ('-'.join((mode, *formats, *models, str(keep))))
                root.mkdir()
                kwargs = self.targets(root, formats) if mode == 'append' else {}
                options = backend.Options(root, models + formats, merge_schlib='SCHLIB' in formats,
                    merge_pcblib='PCBLIB' in formats, append_mode=mode == 'append',
                    keep_schlib=keep, keep_pcblib=keep, keep_individual=keep,
                    bind_3d_models=False, **kwargs)
                before = snapshot(root)
                original = backend.atomic_write
                written = []
                def write(path, payload):
                    original(path, payload)
                    if Path(path).is_relative_to(root):
                        written.append(path)
                        self.api.cancelled.set()
                with patch.object(backend, 'atomic_write', side_effect=write):
                    self.assert_cancelled(self.run_batch(options))
                self.assertTrue(written)
                self.assertEqual(snapshot(root), before)

    def test_kicad_cancel_after_commit_restores_removed_members_and_other_exports(self):
        for merged, formats in itertools.product((False, True),
                (('KICAD_SYMBOL',), ('KICAD_FOOTPRINT',), ('KICAD_SYMBOL', 'KICAD_FOOTPRINT'))):
            with self.subTest(merged=merged, formats=formats):
                self.api.cancelled.clear()
                root = self.root / ('kicad-' + '-'.join(formats) + str(merged))
                options = backend.Options(root, ('STEP', 'OBJ') + formats,
                    kicad_merge_symbols=merged, kicad_merge_footprints=merged,
                    keep_kicad_symbols=merged, keep_kicad_footprints=merged, bind_3d_models=False)
                rows = self.run_batch(options)
                self.assertTrue(all(r.status == '成功' for r in rows), [r.message for r in rows])
                for pretty in root.rglob('*.pretty'):
                    (pretty / 'old.kicad_mod').write_bytes(b'previous member removed by overwrite')
                    (pretty / 'empty-directory').mkdir()
                for path in root.rglob('*'):
                    if path.is_file():
                        path.write_bytes(b'original ' + path.name.encode())
                before = snapshot(root)
                directories = {p.relative_to(root) for p in root.rglob('*') if p.is_dir()}
                original = kicad_export.commit_library
                def commit(*args, **kwargs):
                    result = original(*args, **kwargs)
                    self.api.cancelled.set()
                    return result
                with patch.object(kicad_export, 'commit_library', side_effect=commit):
                    self.assert_cancelled(self.run_batch(options))
                self.assertEqual(snapshot(root), before)
                self.assertEqual({p.relative_to(root) for p in root.rglob('*') if p.is_dir()}, directories)

    def test_cancel_after_project_commit_restores_project_and_appended_libraries(self):
        root = self.root / 'project'
        targets = self.targets(root, ('SCHLIB', 'PCBLIB'))
        project = self.project(root)
        options = backend.Options(root, ('STEP', 'SCHLIB', 'PCBLIB'), append_mode=True,
                                  project_path=str(project), import_existing_to_project=True,
                                  bind_3d_models=False, **targets)
        before = snapshot(root)
        original = backend.atomic_write
        def write(path, payload):
            original(path, payload)
            if Path(path) == project:
                self.api.cancelled.set()
        with patch.object(backend, 'atomic_write', side_effect=write):
            self.assert_cancelled(self.run_batch(options))
        self.assertTrue(self.api.cancelled.is_set())
        self.assertEqual(snapshot(root), before)

    def test_bound_3d_embedded_and_external_models_are_removed_on_cancel(self):
        for target in ('ad', 'embedded', 'external'):
            with self.subTest(target=target):
                self.api.cancelled.clear()
                root = self.root / ('bound-' + target)
                formats = ('PCBLIB',) if target == 'ad' else ('KICAD_FOOTPRINT',)
                options = backend.Options(root, formats, bind_3d_models=True,
                    kicad_3d_mode='external' if target == 'external' else 'embedded')
                original = backend.atomic_write if target == 'ad' else kicad_export.commit_library
                captured = []
                def commit(*args, **kwargs):
                    result = original(*args, **kwargs)
                    captured.extend(p.suffix for p in root.rglob('*') if p.is_file())
                    self.api.cancelled.set()
                    return result
                module, name = (backend, 'atomic_write') if target == 'ad' else (kicad_export, 'commit_library')
                with patch.object(module, name, side_effect=commit):
                    self.assert_cancelled(self.run_batch(options))
                self.assertTrue(captured)
                if target == 'external':
                    self.assertIn('.step', captured)
                self.assertEqual(snapshot(root), {})

    def test_external_file_added_to_new_directory_is_not_deleted(self):
        root = self.root / 'new-output'
        with OutputTransaction() as transaction:
            backend.atomic_write(root / 'component.step', b'batch model')
            (root / 'user.txt').write_bytes(b'external note')
            self.assertEqual(transaction.rollback(), [root])
        self.assertEqual(snapshot(root), {'user.txt': b'external note'})

    def test_recovery_copy_corruption_keeps_current_file_and_reports_error(self):
        path = self.root / 'original.step'
        path.write_bytes(b'original')
        with OutputTransaction() as transaction:
            backend.atomic_write(path, b'new')
            transaction.files[path]['backup'].write_bytes(b'corrupt')
            self.assertEqual(transaction.rollback(), [path])
        self.assertEqual(path.read_bytes(), b'new')

    def test_external_edit_during_write_is_not_adopted_as_batch_content(self):
        path = self.root / 'output.step'
        original = backend.replace_file
        def replace(source, target):
            original(source, target)
            Path(target).write_bytes(b'external edit')
        with OutputTransaction() as transaction:
            with patch.object(backend, 'replace_file', side_effect=replace):
                backend.atomic_write(path, b'batch data')
            self.assertEqual(transaction.rollback(), [path])
        self.assertEqual(path.read_bytes(), b'external edit')

    def test_external_edit_between_writes_is_not_overwritten_or_deleted(self):
        path = self.root / 'output.step'
        with OutputTransaction() as transaction:
            backend.atomic_write(path, b'first batch write')
            path.write_bytes(b'external edit')
            with self.assertRaises(OSError):
                backend.atomic_write(path, b'second batch write')
            self.assertEqual(transaction.rollback(), [path])
        self.assertEqual(path.read_bytes(), b'external edit')

    def test_concurrent_completed_downloads_are_removed_and_new_batch_can_succeed(self):
        options = backend.Options(self.root / 'concurrent', ('STEP', 'OBJ'), bind_3d_models=False)
        def publish(index, result):
            if result.status == '成功':
                self.api.cancelled.set()
        rows = backend.download_batch(['C2040', 'C20197', 'C2765186'] * 3, options,
            self.api.cancelled, api=self.api, workers=3, on_result=publish)
        self.assert_cancelled(rows)
        self.assertEqual(snapshot(options.destination), {})
        self.api.cancelled.clear()
        self.assertTrue(all(r.status == '成功' for r in self.run_batch(options)))

    def test_external_edit_is_preserved_and_cleanup_failure_has_recovery_and_diagnostics(self):
        root = self.root / 'external'
        options = backend.Options(root, ('STEP',), bind_3d_models=False)
        logs.configure_logging('DEBUG', version='test')
        edited = []
        def publish(index, result):
            if result.status == '成功':
                path = Path(result.files[0])
                path.write_bytes(b'external change')
                edited.append(path)
                self.api.cancelled.set()
        rows = self.run_batch(options, on_result=publish)
        self.assertTrue(all(r.status == '失败' for r in rows))
        self.assertEqual(edited[0].read_bytes(), b'external change')
        recovery = list((self.root / 'profile/LCSC3D/temp').glob('download-rollback-*/recovery.json'))
        self.assertEqual(len(recovery), 1)
        events = [json.loads(line) for line in (logs.get_log_directory() / 'LCSC3D.log').read_text('utf-8').splitlines()]
        self.assertTrue(any(e['event'] == 'download.rollback_file_failed' and e['frames'] for e in events))
        self.assertTrue(any(e['event'] == 'download.rollback_failed' for e in events))
        self.assertNotIn(str(edited[0]), json.dumps(events))

    def test_rollback_permission_failure_preserves_original_copy_and_reports_failure(self):
        path = self.root / 'previous.step'
        path.write_bytes(b'original')
        with OutputTransaction() as transaction:
            backend.atomic_write(path, b'new')
            with patch('lcsc3d.services.output_transaction.replace_file', side_effect=PermissionError('private path')):
                self.assertEqual(transaction.rollback(), [path])
            self.assertEqual(transaction.files[path]['backup'].read_bytes(), b'original')
            recovery = transaction.recovery
        self.assertTrue((recovery / 'recovery.json').is_file())

    def test_write_error_before_publication_does_not_remove_original(self):
        path = self.root / 'original.step'
        path.write_bytes(b'original')
        with OutputTransaction() as transaction:
            with patch.object(backend, 'replace_file', side_effect=OSError('write failed')):
                with self.assertRaises(OSError):
                    backend.atomic_write(path, b'new')
            self.assertFalse(transaction.rollback())
        self.assertEqual(path.read_bytes(), b'original')

    def test_repeated_write_restores_first_original_and_cleans_only_new_directories(self):
        root = self.root / 'existing'
        root.mkdir()
        old = root / 'old.step'
        old.write_bytes(b'original')
        with OutputTransaction() as transaction:
            backend.atomic_write(old, b'first')
            backend.atomic_write(old, b'second')
            backend.atomic_write(root / 'new/sub/file.obj', b'new model')
            self.assertFalse(transaction.rollback())
        self.assertEqual(snapshot(root), {'old.step': b'original'})
        self.assertFalse((root / 'new').exists())
        self.assertTrue(root.is_dir())

    def test_late_cancel_does_not_change_completed_token(self):
        stop = BatchCancellation()
        self.assertFalse(stop.finish())
        self.assertFalse(stop.set())
        self.assertFalse(stop.is_set())
        stop = BatchCancellation()
        self.assertTrue(stop.set())
        self.assertTrue(stop.finish())
        self.assertFalse(stop.set())

    def test_precancelled_batch_does_not_write_and_is_reported_cancelled(self):
        self.api.cancelled.set()
        self.assert_cancelled(self.run_batch(backend.Options(self.root / 'unused')))
        self.assertFalse((self.root / 'unused').exists())
