"""Exercise the user-visible export matrix and isolate all application data."""
import copy
import itertools
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import backend
import main
from altium import MergedLibrary
from altium_inspect import schematic, pcb, merged_pcb_section
from app_logging import configure_logging, close_logging
from errors import DownloadError, Cancelled
from export_targets import ExportTargetsDialog
from library_merge import LibraryIndex
from test_backend import STEP, OBJ
from PySide6.QtCore import QEvent, Qt, QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QPushButton


AD_CHOICES = (('SCHLIB',), ('PCBLIB',), ('SCHLIB', 'PCBLIB'))
MODEL_CHOICES = ((), ('STEP',), ('OBJ',), ('STEP', 'OBJ'))


class ExportFixture(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        environment = patch.dict(os.environ, {'LOCALAPPDATA': str(self.root / 'profile')})
        environment.start()
        self.addCleanup(environment.stop)
        self.addCleanup(close_logging)
        self.data = {part: json.loads((Path(__file__).parent / 'fixtures' / (part + '.json')).read_text(encoding='utf-8'))
                     for part in ('C2040', 'C20197', 'C2765186')}
        self.api = backend.NetworkApi()
        self.api.get_cad_data_of_component = lambda part: copy.deepcopy(self.data[part])
        self.api.get_step_3d_model = lambda uuid: STEP
        self.api.get_raw_3d_model_obj = lambda uuid: OBJ
        self.originals = {}
        for fmt in ('SCHLIB', 'PCBLIB'):
            library = MergedLibrary(fmt, merge_pcb=True)
            library.add(self.data['C2765186'], 'C2765186', lambda: None)
            self.originals[fmt] = library.build(['C2765186'], lambda: None)

    def targets(self, root, formats):
        result = {}
        for fmt in formats:
            path = root / ('符号库.SchLib' if fmt == 'SCHLIB' else '封装库.PcbLib')
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(self.originals[fmt])
            result[fmt.lower() + '_target'] = str(path)
        return result

    def project(self, root):
        path = root / '测试工程.PrjPcb'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'[Design]\r\nCustom=preserved\r\n[Document1]\r\nDocumentPath=existing.PcbDoc\r\n')
        return path

    def assert_libraries(self, paths, count, parts=('C2040', 'C20197')):
        for fmt, path in paths.items():
            index = LibraryIndex(path.read_bytes(), fmt)
            try:
                self.assertEqual(len(index.names), count)
            finally:
                index.ole.close()
        if set(paths) == {'SCHLIB', 'PCBLIB'}:
            sch, board = (paths[fmt].read_bytes() for fmt in ('SCHLIB', 'PCBLIB'))
            for part in parts:
                _, records, _ = schematic(sch, part + '_Symbol')
                name, _, _ = pcb(board, merged_pcb_section(board, part))
                self.assertEqual(next(row['MODELNAME'] for row in records if row.get('RECORD') == '45'), name)

    def assert_outputs(self, options, rows, models, keep):
        self.assertEqual([row.status for row in rows], ['成功'] * len(rows))
        paths = options.merged_paths()
        primary = paths.get('SCHLIB') or paths['PCBLIB']
        for row in rows:
            files = [Path(file) for file in row.files]
            self.assertTrue(all(file.is_file() for file in files))
            self.assertEqual(len(files), len(models) + len(paths) * (2 if keep else 1))
            singles = [file for file in files if file.suffix.lower() in ('.schlib', '.pcblib') and file not in paths.values()]
            self.assertEqual(len(singles), len(paths) if keep else 0)
            model_files = [file for file in files if file.suffix.lower() in ('.step', '.obj')]
            folder = options.output_root() / (backend.safe_filename(row.title) + '_' + row.part) if keep else primary.with_suffix('')
            self.assertTrue(all(file.parent == folder for file in model_files))
            for file in model_files:
                self.assertEqual(file.read_bytes(), STEP if file.suffix == '.step' else OBJ.encode('utf-8'))
            self.assertTrue(Path(row.folder).is_dir())
            if keep and len(paths) == 2:
                sch = next(file for file in singles if file.suffix == '.SchLib')
                board = next(file for file in singles if file.suffix == '.PcbLib')
                _, records, _ = schematic(sch.read_bytes())
                name, _, _ = pcb(board.read_bytes())
                self.assertEqual(next(row['MODELNAME'] for row in records if row.get('RECORD') == '45'), name)


@unittest.skipUnless(sys.platform == 'win32', 'Native AD libraries')
class ExportMatrixTests(ExportFixture):
    def test_append_72_combinations_of_targets_models_individual_and_project_link(self):
        for number, (formats, models, keep, project_mode) in enumerate(itertools.product(
                AD_CHOICES, MODEL_CHOICES, (False, True), ('none', 'unlinked', 'linked'))):
            with self.subTest(formats=formats, models=models, keep=keep, project=project_mode):
                root = self.root / str(number)
                targets = self.targets(root / 'libraries', formats)
                project = self.project(root / 'project') if project_mode != 'none' else None
                original_project = project.read_bytes() if project else None
                options = backend.Options(root / 'must-not-create', models + ('SCHLIB', 'PCBLIB'),
                    append_mode=True, keep_individual=keep, **targets,
                    project_path=str(project) if project else '', import_existing_to_project=project_mode == 'linked')
                rows = backend.download_batch(['C2040', 'C20197'], options, api=self.api)
                self.assert_outputs(options, rows, models, keep)
                self.assert_libraries(options.merged_paths(), 3)
                self.assertFalse(options.destination.exists())
                self.assertEqual(set(options.formats), set(models + formats))
                if project:
                    if project_mode == 'linked':
                        self.assertTrue(project.read_bytes().startswith(original_project))
                        self.assertEqual(project.read_text(encoding='utf-8').count('DocumentPath='), 1 + len(formats))
                    else:
                        self.assertEqual(project.read_bytes(), original_project)
                        self.assertTrue(all('库已加入 PCB 工程' not in row.message for row in rows))

    def test_merge_24_combinations_of_library_and_model_formats_and_individual_output(self):
        for number, (formats, models, keep) in enumerate(itertools.product(AD_CHOICES, MODEL_CHOICES, (False, True))):
            with self.subTest(formats=formats, models=models, keep=keep):
                options = backend.Options(self.root / str(number), models + formats,
                    'SCHLIB' in formats, 'PCBLIB' in formats, '自定义符号.SchLib', '自定义封装', keep, keep)
                rows = backend.download_batch(['C2040', 'C20197'], options, api=self.api)
                self.assert_outputs(options, rows, models, keep)
                self.assert_libraries(options.merged_paths(), 2)

    def test_project_only_24_combinations_and_second_run_preserves_existing_members(self):
        for number, (formats, models, keep) in enumerate(itertools.product(AD_CHOICES, MODEL_CHOICES, (False, True))):
            with self.subTest(formats=formats, models=models, keep=keep):
                project = self.project(self.root / str(number))
                options = backend.Options(self.root / 'unused', models + formats, append_mode=True,
                                          keep_individual=keep, project_path=str(project))
                rows = backend.download_batch(['C2040', 'C20197'], options, api=self.api)
                self.assert_outputs(options, rows, models, keep)
                self.assert_libraries(options.merged_paths(), 2)
                self.assertTrue(all(file.stem == project.stem for file in options.merged_paths().values()))
                original_project = project.read_bytes()
                rows = backend.download_batch(['C2765186'], options, api=self.api)
                self.assertEqual(rows[0].status, '成功')
                self.assert_libraries(options.merged_paths(), 3)
                self.assertEqual(project.read_bytes(), original_project)
                self.assertFalse(options.destination.exists())

    def test_independent_merge_switches_and_mixed_keep_switches(self):
        for sch_merge, pcb_merge, sch_keep, pcb_keep in itertools.product((False, True), repeat=4):
            with self.subTest(merges=(sch_merge, pcb_merge), keeps=(sch_keep, pcb_keep)):
                root = self.root / f'{sch_merge}-{pcb_merge}-{sch_keep}-{pcb_keep}'
                options = backend.Options(root, ('STEP', 'SCHLIB', 'PCBLIB'), sch_merge, pcb_merge,
                                          'symbols', 'footprints', sch_keep, pcb_keep)
                row = backend.download_batch(['C2040'], options, api=self.api)[0]
                self.assertEqual(row.status, '成功')
                merged = options.merged_paths()
                grouped = bool(merged) and not (sch_merge and sch_keep or pcb_merge and pcb_keep)
                model = next(Path(file) for file in row.files if file.endswith('.step'))
                self.assertEqual(model.parent, (merged.get('SCHLIB') or merged['PCBLIB']).with_suffix('') if grouped
                                 else root / 'RP2040_C2040')
                self.assertEqual(len(row.files), 3 + int(sch_merge and sch_keep) + int(pcb_merge and pcb_keep))

    def test_different_library_directories_use_schematic_folder_for_models(self):
        targets = {**self.targets(self.root / 'symbols', ('SCHLIB',)),
                   **self.targets(self.root / 'footprints', ('PCBLIB',))}
        options = backend.Options(self.root / 'unused', ('STEP',), append_mode=True, **targets)
        rows = backend.download_batch(['C2040', 'C20197'], options, api=self.api)
        self.assert_outputs(options, rows, ('STEP',), False)
        self.assertEqual({Path(file).parent for row in rows for file in row.files if file.endswith('.step')},
                         {self.root / 'symbols/符号库'})

    def test_open_folder_follows_current_output_despite_previous_individual_directories(self):
        root = self.root / 'exports'
        legacy = root / 'RP2040_C2040'
        legacy.mkdir(parents=True)
        (legacy / 'older.step').write_bytes(b'keep this older export')
        for formats in (('STEP', 'SCHLIB'), ('SCHLIB',)):
            options = backend.Options(root, formats, merge_schlib=True, schlib_name='current')
            row = backend.download_batch(['C2040'], options, api=self.api)[0]
            self.assertEqual(row.status, '成功')
            self.assertEqual(Path(row.folder), root / 'current' if 'STEP' in formats else root)
            self.assertEqual((legacy / 'older.step').read_bytes(), b'keep this older export')

    def test_empty_queue_imports_unchanged_libraries_and_skips_duplicate_references(self):
        for formats in AD_CHOICES:
            with self.subTest(formats=formats):
                root = self.root / '-'.join(formats)
                targets = self.targets(root, formats)
                project = self.project(root)
                options = backend.Options(root / 'unused', (), append_mode=True, **targets,
                    project_path=str(project), import_existing_to_project=True)
                result = backend.import_existing_libraries(options)
                self.assertEqual((result['added'], result['skipped']), (len(formats), 0))
                original = project.read_bytes()
                result = backend.import_existing_libraries(options)
                self.assertEqual((result['added'], result['skipped']), (0, len(formats)))
                self.assertEqual(original, project.read_bytes())
                self.assertFalse(options.destination.exists())
                for fmt, path in options.existing_targets().items():
                    self.assertEqual(path.read_bytes(), self.originals[fmt])

    def test_invalid_targets_missing_options_cancel_and_no_silent_project_import(self):
        project = self.project(self.root)
        targets = self.targets(self.root, ('SCHLIB', 'PCBLIB'))
        options = backend.Options(self.root / 'unused', ('STEP',), append_mode=True,
            **targets, project_path=str(project), import_existing_to_project=True)
        original = project.read_bytes()
        Path(targets['pcblib_target']).write_bytes(b'invalid')
        with self.assertRaises(DownloadError):
            backend.import_existing_libraries(options)
        self.assertEqual(project.read_bytes(), original)
        Path(targets['pcblib_target']).write_bytes(self.originals['PCBLIB'])
        def cancel():
            raise Cancelled()
        with self.assertRaises(Cancelled):
            backend.import_existing_libraries(options, cancel)
        self.assertEqual(project.read_bytes(), original)
        options.import_existing_to_project = False
        with self.assertRaises(DownloadError):
            backend.import_existing_libraries(options)
        with self.assertRaises(DownloadError):
            backend.download_batch(['C2040'], backend.Options(self.root, append_mode=True), api=self.api)

    def test_cancel_before_append_commit_preserves_original_libraries_and_project(self):
        targets = self.targets(self.root, ('SCHLIB', 'PCBLIB'))
        project = self.project(self.root)
        old_project = project.read_bytes()
        options = backend.Options(self.root / 'unused', ('STEP',), append_mode=True,
            **targets, project_path=str(project), import_existing_to_project=True)
        def stop(index, row):
            if row.pending_formats:
                self.api.cancelled.set()
        rows = backend.download_batch(['C2040'], options, self.api.cancelled, api=self.api, on_result=stop)
        self.assertEqual(rows[0].status, '部分完成')
        self.assertEqual([Path(file).suffix for file in rows[0].files], ['.step'])
        self.assertEqual(project.read_bytes(), old_project)
        for fmt, path in options.existing_targets().items():
            self.assertEqual(path.read_bytes(), self.originals[fmt])

    def test_failed_append_only_registers_successful_library_and_logs_outcome(self):
        folder = self.root / 'profile/LCSC3D/logs'
        configure_logging(version=main.VERSION, directory=folder)
        targets = self.targets(self.root, ('SCHLIB', 'PCBLIB'))
        project = self.project(self.root)
        Path(targets['pcblib_target']).write_bytes(b'invalid')
        options = backend.Options(self.root / 'unused', ('STEP', 'OBJ'), append_mode=True,
            **targets, project_path=str(project), import_existing_to_project=True)
        rows = backend.download_batch(['C2040', 'C20197'], options, api=self.api)
        self.assertEqual([row.status for row in rows], ['部分完成', '部分完成'])
        self.assertIn('符号库.SchLib', project.read_text(encoding='utf-8'))
        self.assertNotIn('封装库.PcbLib', project.read_text(encoding='utf-8'))
        self.assertEqual(Path(targets['pcblib_target']).read_bytes(), b'invalid')
        logs = [json.loads(line) for line in (folder / 'LCSC3D.log').read_text(encoding='utf-8').splitlines()]
        self.assertTrue(any(row['event'] == 'download.append_target_invalid' and row['format'] == 'PCBLIB'
                            and row['stage'] == 'read' and row['causes'] for row in logs))
        self.assertTrue(any(row['event'] == 'download.export_plan' and row['import_project'] for row in logs))
        self.assertTrue(any(row['event'] == 'download.output_layout' and row['model_layout'] == 'library' for row in logs))
        self.assertTrue(any(row['event'] == 'download.library_committed' and row['mode'] == 'append' for row in logs))
        self.assertTrue(any(row['event'] == 'download.project_completed' and row['added'] == 1 for row in logs))
        self.assertNotIn(str(self.root), (folder / 'LCSC3D.log').read_text(encoding='utf-8'))

    def test_project_write_failure_reports_partial_without_losing_downloaded_libraries(self):
        targets = self.targets(self.root, ('SCHLIB',))
        project = self.project(self.root)
        original = project.read_bytes()
        options = backend.Options(self.root / 'unused', (), append_mode=True, **targets,
                                  project_path=str(project), import_existing_to_project=True)
        with patch('backend.add_libraries_to_project', side_effect=PermissionError('locked')):
            row = backend.download_batch(['C2040'], options, api=self.api)[0]
        self.assertEqual(row.status, '部分完成')
        self.assertIn('加入 PCB 工程失败', row.message)
        self.assertEqual(project.read_bytes(), original)
        self.assert_libraries(options.merged_paths(), 2, ())

    def test_project_named_library_appearing_during_download_is_not_overwritten(self):
        project = self.project(self.root)
        options = backend.Options(self.root / 'unused', ('SCHLIB',), append_mode=True, project_path=str(project))
        target = options.merged_paths()['SCHLIB']
        def appeared(index, row):
            if row.pending_formats:
                target.write_bytes(self.originals['SCHLIB'])
        row = backend.download_batch(['C2040'], options, api=self.api, on_result=appeared)[0]
        self.assertEqual(row.status, '失败')
        self.assertEqual(target.read_bytes(), self.originals['SCHLIB'])


@unittest.skipUnless(sys.platform == 'win32', 'Native Windows UI')
class ExportWindowTests(ExportFixture):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setStyle('Fusion')
        cls.app.setStyleSheet(main.STYLES)

    def setUp(self):
        super().setUp()
        for name in ('request_component_info', 'show_preview'):
            mock = patch.object(main.MainWindow, name)
            mock.start()
            self.addCleanup(mock.stop)
        self.window = main.MainWindow(settings_enabled=False)
        self.window.path_input.setText(str(self.root / 'downloads'))
        self.window.show()
        self.app.processEvents()
        self.addCleanup(self.close_window)

    def close_window(self):
        if self.window.worker:
            self.window.worker.cancelled.set()
            self.window.worker.wait(10000)
        self.window.close()
        self.window.deleteLater()
        self.app.sendPostedEvents(None, QEvent.DeferredDelete)

    def dialog(self, values=None):
        dialog = ExportTargetsDialog(values or {}, self.window)
        self.addCleanup(dialog.deleteLater)
        return dialog

    def test_default_hidden_mutual_exclusion_and_path_locks_survive_batch(self):
        w = self.window
        self.assertFalse(w.merge_options.isVisible())
        self.assertFalse(w.append_options.isVisible())
        self.assertNotIn('库与工程…', [button.text() for button in w.findChildren(QPushButton)])
        w.schlib_box.setChecked(True)
        w.lib_merge_box.click()
        self.assertTrue(w.merge_options.isVisible())
        self.assertEqual(w.keep_schlib_box.text(), '独立导出器件')
        self.assertTrue(w.schlib_name_input.isEnabled())
        self.assertFalse(w.pcblib_name_input.isEnabled())
        w.lib_append_box.setChecked(True)
        self.assertFalse(w.lib_merge_box.isChecked())
        self.assertFalse(w.merge_options.isVisible())
        self.assertTrue(w.append_options.isVisible())
        for running in (True, False):
            w.set_running(running)
            self.assertFalse(w.path_input.isEnabled())
            self.assertFalse(w.browse_button.isEnabled())
        w.lib_merge_box.click()
        self.assertFalse(w.lib_append_box.isChecked())
        self.assertTrue(w.path_input.isEnabled())
        w.lib_merge_box.click()
        self.assertFalse(w.merge_options.isVisible())
        self.assertFalse(w.append_options.isVisible())
        self.assertFalse(w.export_options().merged_paths())

    def test_revealing_controls_at_minimum_size_never_overlaps_table_and_progress(self):
        w = self.window
        for mode in ('individual', 'merge', 'append', 'merge'):
            w.lib_merge_box.setChecked(mode == 'merge')
            w.lib_append_box.setChecked(mode == 'append')
            w.resize(1060, 740)
            QTest.qWait(30)
            self.assertLess(w.table.geometry().bottom(), w.progress_bar.geometry().top())
            self.assertGreaterEqual(w.width(), w.minimumSizeHint().width())
            self.assertGreaterEqual(w.height(), w.minimumSizeHint().height())

    def test_checkbox_opens_dialog_and_confirm_and_cancel_apply_expected_state(self):
        w = self.window
        targets = self.targets(self.root, ('SCHLIB',))
        def confirm():
            dialog = self.app.activeModalWidget()
            self.assertIsInstance(dialog, ExportTargetsDialog)
            dialog.inputs['schlib_target'].setText(targets['schlib_target'])
            dialog.keep_box.setChecked(True)
            dialog.save()
        QTimer.singleShot(0, confirm)
        w.lib_append_box.click()
        self.assertTrue(w.lib_append_box.isChecked())
        self.assertTrue(w.export_targets['keep_individual'])
        self.assertEqual(w.export_targets['schlib_target'], targets['schlib_target'])
        before = w.export_targets.copy()
        def cancel():
            dialog = self.app.activeModalWidget()
            dialog.inputs['schlib_target'].clear()
            dialog.keep_box.setChecked(False)
            dialog.reject()
        QTimer.singleShot(0, cancel)
        w.append_configure_button.click()
        self.assertEqual(w.export_targets, before)
        w.lib_append_box.setChecked(False)
        w.export_targets = dict(schlib_target='', pcblib_target='', project_path='')
        QTimer.singleShot(0, cancel)
        w.lib_append_box.click()
        self.assertFalse(w.lib_append_box.isChecked())
        self.assertTrue(w.path_input.isEnabled())

    def test_append_dialog_gate_tracks_each_path_and_clears_stale_check(self):
        d = self.dialog()
        self.assertEqual(d.keep_box.text(), '独立导出器件')
        for sch, pcb, project in itertools.product((False, True), repeat=3):
            d.inputs['schlib_target'].setText('test.SchLib' if sch else '')
            d.inputs['pcblib_target'].setText('test.PcbLib' if pcb else '')
            d.inputs['project_path'].setText('test.PrjPcb' if project else '')
            self.assertEqual(d.project_box.isEnabled(), (sch or pcb) and project)
            if d.project_box.isEnabled():
                d.project_box.setChecked(True)
                d.inputs['project_path'].clear()
                self.assertFalse(d.project_box.isEnabled())
                self.assertFalse(d.project_box.isChecked())

    def test_append_dialog_validates_without_writing_and_keeps_unlinked_project(self):
        d = self.dialog()
        d.save()
        self.assertIn('至少选择', d.status.text())
        targets = self.targets(self.root, ('PCBLIB',))
        project = self.project(self.root)
        original = project.read_bytes()
        d.inputs['pcblib_target'].setText(targets['pcblib_target'])
        d.inputs['project_path'].setText(str(project))
        d.keep_box.setChecked(True)
        d.save()
        self.assertEqual(d.result(), d.DialogCode.Accepted)
        self.assertEqual(d.values['project_path'], str(project))
        self.assertTrue(d.values['keep_individual'])
        self.assertFalse(d.values['import_existing_to_project'])
        self.assertEqual(project.read_bytes(), original)

    def test_inactive_targets_never_leak_and_single_target_locks_corresponding_format(self):
        w = self.window
        w.schlib_box.setChecked(True)
        w.pcblib_box.setChecked(True)
        w.export_targets.update(self.targets(self.root, ('SCHLIB',)))
        w.export_targets.update(project_path=str(self.project(self.root)), import_existing_to_project=True)
        self.assertFalse(w.export_options().schlib_target)
        self.assertFalse(w.export_options().project_path)
        w.lib_append_box.setChecked(True)
        self.assertTrue(w.schlib_box.isChecked())
        self.assertFalse(w.pcblib_box.isChecked())
        self.assertFalse(w.schlib_box.isEnabled())
        self.assertFalse(w.pcblib_box.isEnabled())
        self.assertEqual(w.export_options().formats, ('STEP', 'SCHLIB'))
        w.lib_merge_box.setChecked(True)
        self.assertTrue(w.pcblib_box.isChecked())
        self.assertTrue(w.pcblib_box.isEnabled())
        self.assertFalse(w.export_options().schlib_target)
        self.assertFalse(w.export_options().project_path)

    def test_empty_queue_direct_import_runs_without_component_requests_or_download_directory(self):
        w = self.window
        targets = self.targets(self.root, ('SCHLIB', 'PCBLIB'))
        project = self.project(self.root)
        w.export_targets.update(**targets, project_path=str(project), import_existing_to_project=True)
        w.lib_append_box.setChecked(True)
        self.assertEqual(w.start_button.text(), '导入已有库')
        with patch.object(main, 'NetworkApi', side_effect=AssertionError('No resource requests allowed')):
            w.start_button.click()
            self.assertIsInstance(w.worker, main.LibraryImportWorker)
            self.assertTrue(w.worker.wait(10000))
            self.app.processEvents()
        self.assertIn('新增 2 份库', w.run_status.text())
        self.assertFalse(w.batch_running)
        self.assertFalse(w.path_input.isEnabled())
        self.assertEqual(w.table.rowCount(), 0)
        self.assertFalse((self.root / 'downloads').exists())
        self.assertEqual(project.read_text(encoding='utf-8').count('DocumentPath='), 3)

    def test_empty_queue_missing_link_does_not_write_and_nonempty_unchecked_queue_does_not_import(self):
        w = self.window
        targets = self.targets(self.root, ('SCHLIB',))
        project = self.project(self.root)
        original = project.read_bytes()
        w.export_targets.update(**targets, project_path=str(project))
        w.lib_append_box.setChecked(True)
        w.start_button.click()
        self.assertIsNone(w.worker)
        self.assertIn('并勾选', w.run_status.text())
        w.export_targets['import_existing_to_project'] = True
        w.input.setPlainText('C2040')
        w.load_queue()
        w.table.item(0, main.DOWNLOAD_COLUMN).setCheckState(Qt.Unchecked)
        w.start_button.click()
        self.assertIsNone(w.worker)
        self.assertIn('至少勾选', w.run_status.text())
        self.assertEqual(project.read_bytes(), original)

    def test_settings_roundtrip_and_legacy_mode_migration(self):
        w = self.window
        path = self.root / 'profile/LCSC3D/settings.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        w.settings_enabled = True
        self.addCleanup(setattr, w, 'settings_enabled', False)
        targets = self.targets(self.root, ('PCBLIB',))
        with patch.object(main, 'SETTINGS_PATH', path):
            for mode in ('individual', 'merge', 'append'):
                w.export_targets.update(**targets, keep_individual=True, import_existing_to_project=False)
                w.lib_merge_box.setChecked(mode == 'merge')
                w.lib_append_box.setChecked(mode == 'append')
                w.save_settings()
                w._restore_settings()
                self.assertEqual(w.library_mode(), mode)
                self.assertTrue(w.export_targets['keep_individual'])
                self.assertFalse(w.export_targets['import_existing_to_project'])
            path.write_text(json.dumps({**targets, 'merge_pcblib': True, 'keep_pcblib': True,
                                       'project_path': str(self.project(self.root))}), encoding='utf-8')
            w._restore_settings()
            self.assertEqual(w.library_mode(), 'append')
            self.assertTrue(w.export_targets['keep_individual'])
            self.assertTrue(w.export_targets['import_existing_to_project'])


if __name__ == '__main__':
    unittest.main()
