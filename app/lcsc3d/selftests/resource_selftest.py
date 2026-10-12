"""Anonymous model download/preview acceptance in the real application."""
import hashlib
import json
from pathlib import Path
import sys
import time

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication


def start(window, folder):
    main = sys.modules[window.__class__.__module__]
    destination = Path(folder).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    expected_file = destination / 'expected-step-sha256.json'
    expected = json.loads(expected_file.read_text('utf-8')) if expected_file.exists() else {}
    report = {'version': main.VERSION, 'frozen': bool(getattr(sys, 'frozen', False)),
              'saved_session_accessed': False, 'cases': []}
    timer = QTimer(window)
    state = {'phase': 'preview', 'case': 0, 'deadline': time.monotonic() + 120, 'done': False}
    window.input.setPlainText('C48800488\nC2040')
    window.load_queue()

    def finish(error=''):
        if state['done']:
            return
        state['done'] = True
        timer.stop()
        report['success'] = not error
        if error:
            report['error'] = error
        (destination / 'verification.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        window.close()
        QApplication.instance().exit(1 if error else 0)

    def begin():
        target = ('ad', 'kicad')[state['case']]
        window.export_target_box.setCurrentIndex(window.export_target_box.findData(target))
        window.path_input.setText(str(destination / target))
        window.step_box.setChecked(True)
        window.obj_box.setChecked(True)
        if target == 'ad':
            window.schlib_box.setChecked(True)
            window.pcblib_box.setChecked(True)
            window.lib_merge_box.setChecked(True)
            window.merge_schlib_box.setChecked(True)
            window.merge_pcblib_box.setChecked(True)
        else:
            window.kicad_symbol_box.setChecked(True)
            window.kicad_footprint_box.setChecked(True)
        window.start_button.click()
        assert window.batch_running and window.worker is not None, window.run_status.text()
        state['phase'] = 'running'

    def verify():
        results = list(window.results.values())
        assert {row.part for row in results} == {'C48800488', 'C2040'}
        assert all(row.status == '成功' for row in results), [vars(row) for row in results]
        for row in results:
            step = next(Path(name) for name in row.files if name.lower().endswith('.step'))
            raw = step.read_bytes()
            assert raw.startswith(b'ISO-10303-21') and raw.rstrip().endswith(b'END-ISO-10303-21;')
            if row.part in expected:
                assert hashlib.sha256(raw).hexdigest() == expected[row.part]
            if row.part == 'C48800488':
                assert '备用关联' in row.message
            else:
                assert '备用关联' not in row.message
            assert not row.export_errors
        target = ('ad', 'kicad')[state['case']]
        report['cases'].append({'target': target, 'results': [vars(row) for row in results], 'success': True})
        assert window.grab().save(str(destination / (target + '-complete.png')))
        if state['case'] == 0:
            state.update(case=1, phase='begin')
        else:
            finish()

    def tick():
        try:
            if time.monotonic() > state['deadline']:
                raise TimeoutError('Model resource acceptance timed out: ' + state['phase'])
            if state['phase'] == 'preview' and window.preview_state == 'ready':
                assert 'C48800488' in window.preview_status.text() and '5802' in window.preview_status.text(), window.preview_status.text()
                report['preview'] = {'part': 'C48800488', 'faces': 5802, 'ready': True}
                assert window.grab().save(str(destination / 'preview.png'))
                state['phase'] = 'begin'
            elif state['phase'] == 'begin':
                begin()
            elif state['phase'] == 'running' and not window.batch_running and not window.worker.isRunning():
                verify()
        except Exception as exc:
            finish(type(exc).__name__ + ': ' + str(exc))

    timer.timeout.connect(tick)
    timer.start(100)
