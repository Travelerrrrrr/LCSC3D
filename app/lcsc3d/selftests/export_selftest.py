"""Offline, isolated end-to-end verification of the frozen export controls."""
import copy
import json
from pathlib import Path
import sys
import time

from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import QApplication

from lcsc3d.eda.altium import MergedLibrary
from lcsc3d.services.backend import safe_filename
from lcsc3d.ui.export_targets import ExportTargetsDialog
from lcsc3d.eda.library_merge import LibraryIndex


def start(window, folder):
    from lcsc3d.ui import main
    destination = Path(folder).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    data = {part: json.loads((destination / 'fixtures' / (part + '.json')).read_text(encoding='utf-8'))
            for part in ('C2040', 'C20197', 'C2765186', 'C23922', 'C8734')}
    originals = {}
    for fmt in ('SCHLIB', 'PCBLIB'):
        library = MergedLibrary(fmt, merge_pcb=True)
        library.add(data['C2765186'], 'C2765186', lambda: None)
        originals[fmt] = library.build(['C2765186'], lambda: None)

    class OfflineApi(main.NetworkApi):
        def get_cad_data_of_component(self, part):
            self.check_cancelled()
            return copy.deepcopy(data[part])

        def get_step_3d_model(self, uuid):
            self.check_cancelled()
            return b'ISO-10303-21;\nHEADER;\nENDSEC;\nDATA;\nENDSEC;\nEND-ISO-10303-21;'

        def get_raw_3d_model_obj(self, uuid):
            self.check_cancelled()
            return 'v 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n'

    main.NetworkApi = OfflineApi
    window.request_component_info = lambda: None
    window.show_preview = lambda *args, **kwargs: None
    window.resize(1240, 850)
    report = {'version': main.VERSION, 'frozen': bool(getattr(sys, 'frozen', False)), 'cases': []}
    cases = [('merge-grouped', 'merge', ('SCHLIB', 'PCBLIB'), False),
             ('merge-individual', 'merge', ('SCHLIB', 'PCBLIB'), True),
             ('append-grouped', 'append', ('SCHLIB', 'PCBLIB'), False),
             ('append-individual', 'append', ('SCHLIB', 'PCBLIB'), True),
             ('append-symbol', 'append', ('SCHLIB',), False),
             ('append-footprint', 'append', ('PCBLIB',), True),
             ('project-grouped', 'project', ('SCHLIB', 'PCBLIB'), False),
             ('project-individual', 'project', ('SCHLIB', 'PCBLIB'), True),
             ('empty-import', 'empty', ('SCHLIB', 'PCBLIB'), False),
             ('populated-import', 'button', ('SCHLIB', 'PCBLIB'), False),
             ('project-custom-grouped', 'project', ('SCHLIB', 'PCBLIB'), False),
             ('project-custom-individual', 'project', ('SCHLIB', 'PCBLIB'), True),
             ('append-custom-individual', 'append', ('SCHLIB', 'PCBLIB'), True),
             ('shared-merge-grouped', 'merge', ('SCHLIB', 'PCBLIB'), False),
             ('shared-merge-individual', 'merge', ('SCHLIB', 'PCBLIB'), True),
             ('shared-append-grouped', 'append', ('SCHLIB', 'PCBLIB'), False),
             ('shared-append-individual', 'append', ('SCHLIB', 'PCBLIB'), True)]
    state = {'case': -1, 'phase': 'next', 'done': False, 'deadline': time.monotonic() + 120}
    timer = QTimer(window)

    def capture(widget, name):
        QApplication.processEvents()
        assert widget.grab().save(str(destination / (name + '.png')))

    def finish(error=''):
        if state['done']:
            return
        state['done'] = True
        timer.stop()
        report.update(success=not error, error=error)
        (destination / 'export-verification.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        if window.worker and window.worker.isRunning():
            window.worker.cancelled.set()
        window.close()
        def exit_when_idle():
            if window.worker and window.worker.isRunning():
                QTimer.singleShot(100, exit_when_idle)
            else:
                QApplication.instance().exit(1 if error else 0)
        QTimer.singleShot(100, exit_when_idle)

    def configure_dialog():
        try:
            dialog = window.export_dialog
            assert isinstance(dialog, ExportTargetsDialog)
            assert dialog.window() is window and not dialog.isWindow()
            for key in dialog.inputs:
                dialog.inputs[key].setText(state['targets'].get(key, ''))
            dialog.keep_box.setChecked(state['keep'])
            if state['case'] == 2:
                capture(dialog, '追加配置')
            if 'custom' in state['name']:
                capture(dialog, state['name'] + '-配置')
                scroll = dialog.content_scroll.verticalScrollBar()
                scroll.setValue(scroll.maximum())
                capture(dialog, state['name'] + '-目录与库名')
                scroll.setValue(0)
            if state['mode'] not in ('empty', 'button'):
                dialog.save()
                assert dialog.result() == dialog.DialogCode.Accepted, dialog.status.text()
        except Exception as exc:
            if getattr(window, 'export_dialog', None):
                window.export_dialog.reject()
            finish(str(exc) or type(exc).__name__)

    def next_case():
        state['case'] += 1
        if state['case'] == len(cases):
            finish()
            return
        name, mode, formats, keep = cases[state['case']]
        root = destination / name
        root.mkdir(exist_ok=True)
        state.update(name=name, mode=mode, formats=formats, keep=keep, root=root, targets={}, phase='prepare')
        state['shared'] = name.startswith('shared-')
        state['originals'] = originals.copy()
        if state['shared']:
            for fmt in formats:
                library = MergedLibrary(fmt, merge_pcb=True)
                library.add(data['C23922'], 'C23922', lambda: None)
                state['originals'][fmt] = library.build(['C23922'], lambda: None)
        window.lib_append_box.setChecked(False)
        window.lib_merge_box.setChecked(False)
        window.path_input.setText(str(root / 'downloads'))
        window.step_box.setChecked(True)
        window.bind_3d_box.setChecked(False)  # 2D library mode regression; models have a dedicated self-test.
        window.obj_box.setChecked(True)
        window.schlib_box.setChecked(True)
        window.pcblib_box.setChecked(True)
        if mode == 'empty':
            window.select_all_downloads()
            window.remove_checked_downloads()
            assert not window.ids and not window.input.toPlainText().strip()
        else:
            window.input.setPlainText('C23922\nC8734' if state['shared'] else 'C2040\nC20197')
            window.load_queue()
        if mode == 'merge':
            window.lib_merge_box.click()
            window.merge_schlib_box.setChecked(True)
            window.merge_pcblib_box.setChecked(True)
            window.schlib_name_input.setText('项目符号.SchLib')
            window.pcblib_name_input.setText('项目封装')
            window.keep_schlib_box.setChecked(keep)
            window.keep_pcblib_box.setChecked(keep)
            assert window.merge_options.isVisible() and not window.append_options.isVisible()
            if not keep:
                capture(window, '合并配置')
        else:
            if mode != 'project':
                for fmt in formats:
                    path = root / ('已有符号.SchLib' if fmt == 'SCHLIB' else '已有封装.PcbLib')
                    path.write_bytes(state['originals'][fmt])
                    state['targets'][fmt.lower() + '_target'] = str(path)
            project = root / '验证工程.PrjPcb'
            project.write_bytes(b'[Design]\r\nCustom=preserved\r\n')
            state['targets']['project_path'] = str(project)
            if 'custom' in name:
                state['targets']['individual_directory'] = str(root / 'independent-parts')
                if mode == 'project':
                    state['targets'].update(project_schlib_directory=str(root / 'symbols'),
                        project_schlib_name='自定义符号', project_pcblib_directory=str(root / 'footprints'),
                        project_pcblib_name='自定义封装.PcbLib', project_model_directory=str(root / 'models'))
            window.lib_append_box.click()
            configure_dialog()
            if state['done']:
                return
            assert not window.lib_merge_box.isChecked() and not window.merge_options.isVisible()
            assert not window.path_input.isEnabled() and not window.browse_button.isEnabled()
        if mode in ('empty', 'button'):
            assert window.export_dialog.import_button.isEnabled()
            window.export_dialog.import_button.click()
        else:
            window.start_button.click()
        assert window.worker is not None and window.batch_running, window.run_status.text()
        state['phase'] = 'running'

    def verify_case():
        if state['mode'] in ('empty', 'button'):
            assert '新增 2 份库' in window.export_dialog.status.text(), window.export_dialog.status.text()
            capture(window, state['name'] + '-完成')
            window.export_dialog.save()
            assert window.export_dialog.result() == window.export_dialog.DialogCode.Accepted
        options = window.export_options()
        mode, keep = state['mode'], state['keep']
        root = state['root']
        if mode in ('empty', 'button'):
            assert '新增 2 份库' in window.run_status.text(), window.run_status.text()
            assert not window.ids if mode == 'empty' else bool(window.ids)
            for fmt, path in options.existing_targets().items():
                assert path.read_bytes() == originals[fmt]
            assert Path(options.project_path).read_text(encoding='utf-8').count('DocumentPath=') == 2
            report['cases'].append({'name': state['name'], 'success': True,
                                    'project': options.project_path, 'files': list(state['targets'].values())})
        else:
            rows = list(window.results.values())
            assert len(rows) == 2 and all(row.status == '成功' for row in rows), [vars(row) for row in rows]
            merged = options.merged_paths()
            for fmt, path in merged.items():
                index = LibraryIndex(path.read_bytes(), fmt)
                try:
                    expected = (1 if fmt == 'PCBLIB' else 2) if state['shared'] else 3 if mode == 'append' else 2
                    assert len(index.names) == expected
                    assert not any(part in name for name in index.names for part in ('C23922', 'C8734'))
                finally:
                    index.ole.close()
            for row in rows:
                files = [Path(file) for file in row.files]
                assert len(files) == 2 + len(merged) * (2 if keep else 1)
                model_folder = options.model_folder(options.output_root() / (safe_filename(row.title) + '_' + row.part))
                assert all(file.parent == model_folder for file in files if file.suffix in ('.step', '.obj'))
                if not keep and not options.project_model_directory:
                    assert model_folder.name.endswith('_3D')
                assert all(file.is_file() for file in files)
            if mode != 'merge':
                assert not (root / 'downloads').exists()
                assert not window.path_input.isEnabled()
                assert Path(options.project_path).read_text(encoding='utf-8').count('DocumentPath=') == (len(merged) if mode == 'project' else 0)
            if state['shared'] and mode == 'append':
                assert merged['PCBLIB'].read_bytes() == state['originals']['PCBLIB']
            report['cases'].append({'name': state['name'], 'success': True,
                'project': options.project_path, 'results': [vars(row) for row in rows]})
            if state['case'] == 2:
                capture(window, '追加完成')
            if state['shared'] and not keep:
                capture(window, '共享封装-' + mode)
        state['phase'] = 'next'

    def tick():
        try:
            if time.monotonic() > state['deadline']:
                raise TimeoutError('Export self-test timed out: ' + state['phase'])
            if state['phase'] == 'next':
                next_case()
            elif state['phase'] == 'running' and not window.batch_running and not window.worker.isRunning():
                verify_case()
        except Exception as exc:
            finish(type(exc).__name__ + ': ' + str(exc))

    assert not window.merge_options.isVisible() and not window.append_options.isVisible()
    capture(window, '默认界面')
    timer.timeout.connect(tick)
    timer.start(80)
