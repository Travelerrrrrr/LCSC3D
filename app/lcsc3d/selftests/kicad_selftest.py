"""Isolated GUI integration check used by the local frozen-demo verifier."""
import hashlib
import json
from pathlib import Path
import sys
import time
import shutil

from PySide6.QtCore import QTimer, QPoint, QEvent, QObject
from PySide6.QtWidgets import QApplication


def start(window, folder):
    main = sys.modules[type(window).__module__]
    root = Path(folder).resolve()
    root.mkdir(parents=True, exist_ok=True)
    data = {part: json.loads((root / 'fixtures' / (part + '.json')).read_text('utf-8'))
            for part in ('C20197', 'C2765186', 'C23922', 'C8734', 'C2040', 'C5879483', 'C31966')}
    class OfflineApi(main.NetworkApi):
        def fetch(self, *args, **kwargs):
            raise AssertionError('Offline self-test must not access the network')
        def get_cad_data_of_component(self, part):
            self.check_cancelled()
            return data['C5879483'] if part.startswith('C90000') else data[part]
        def get_step_3d_model(self, uuid):
            return b'ISO-10303-21;\nEND-ISO-10303-21;\n'
    main.NetworkApi = OfflineApi
    window.request_component_info = lambda: None
    window.show_preview = lambda *args, **kwargs: None
    window.resize(1380, 880)
    cases = [('individual', False, False, 'kicad', ['C20197', 'C2765186']),
             ('combined', True, True, 'kicad', ['C23922', 'C8734']),
             ('merge-symbols', True, False, 'kicad', ['C23922', 'C8734']),
             ('merge-footprints', False, True, 'kicad', ['C23922', 'C8734']),
             ('footprint-only', False, True, 'kicad', ['C23922', 'C8734']),
             ('copies', True, True, 'kicad', ['C23922','C8734']),
             ('reversed', False, False, 'kicad', ['C5879483']),
             ('ad', False, False, 'ad', ['C20197', 'C2765186', 'C5879483']),
             ('ad-layout', False, False, 'ad', ['C5879483'] + [f'C90000{i}' for i in range(11)]),
             ('ad-replace', False, False, 'ad', ['C5879483']),
             ('replace', True, True, 'kicad', ['C5879483']),
             ('via-ad', True, True, 'ad', ['C31966']),
             ('via-kicad', True, True, 'kicad', ['C31966']),
             ('preview-sample', True, True, 'kicad', ['C2040'])]
    state = {'index': -1, 'phase': 'next', 'deadline': time.monotonic() + 90, 'done': False}
    report = {'version': main.VERSION, 'frozen': bool(getattr(sys, 'frozen', False)), 'cases': []}
    timer = QTimer(window)
    timer.setInterval(70)
    frames = []
    class PaintObserver(QObject):
        def eventFilter(self,widget,event):
            if event.type() == QEvent.Paint:
                frames.append((widget.geometry().getRect(),widget.mapTo(window,QPoint()).y()))
            return False
    observer = PaintObserver(window)
    def finish(error=''):
        state['done'] = True
        timer.stop()
        report.update(success=not error, error=error)
        (root / 'kicad-verification.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), 'utf-8')
        if window.worker and window.worker.isRunning():
            window.worker.cancelled.set()
        def close():
            if window.worker and window.worker.isRunning():
                QTimer.singleShot(100, close)
            else:
                window.close()
                QApplication.instance().exit(1 if error else 0)
        close()
    def hashes(path):
        return {str(p.relative_to(path)): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in path.rglob('*') if p.is_file()}
    def verify_layout():
        def top(widget): return widget.mapTo(window, QPoint(0,0)).y()
        assert top(window.detail) >= top(window.table) + window.table.height() + 8
        assert window.detail.height() >= window.detail.heightForWidth(window.detail.width())
        assert top(window.export_card) >= top(window.workspace_splitter) + window.workspace_splitter.height() + 12
        report['wrapped_results_and_card_spacing_verified'] = True
    def check_merge_frames(index=0):
        try:
            if index == 4:
                window.export_card.removeEventFilter(observer)
                report['merge_transition_frames_verified'] = True
                finish()
                return
            target = 'ad' if index < 2 else 'kicad'
            window.export_target_box.setCurrentIndex(window.export_target_box.findData(target))
            box = window.lib_merge_box if target == 'ad' else window.kicad_lib_merge_box
            window.content_scroll.ensureWidgetVisible(box)
            def click():
                frames.clear()
                box.click()
                def inspect():
                    try:
                        final = (window.export_card.geometry().getRect(),window.export_card.mapTo(window,QPoint()).y())
                        assert frames and set(frames) == {final}, (target,frames,final)
                        check_merge_frames(index+1)
                    except Exception as exc:
                        finish('Merge paint: '+str(exc))
                QTimer.singleShot(180,inspect)
            QTimer.singleShot(120,click)
        except Exception as exc:
            finish('Merge control: '+str(exc))
    def tick():
        try:
            if time.monotonic() > state['deadline']:
                raise AssertionError('KiCad GUI check timed out')
            if state['phase'] == 'next':
                state['index'] += 1
                if state['index'] == len(cases):
                    from lcsc3d.ui.library_preview import build_library_preview
                    preview = build_library_preview(json.loads((root / 'fixtures/C2040_svgs.json').read_text('utf-8')), 'C2040')
                    window.library_cache['C2040'] = preview
                    window.current_preview = 'C2040'
                    window.set_preview_mode('symbol')
                    window.display_library_preview('C2040', preview)
                    state['phase'] = 'preview'
                    return
                name, symbols, footprints, target, parts = cases[state['index']]
                ad = target == 'ad'
                output = root / name
                if name in ('replace','ad-replace'):
                    shutil.copytree(root/('combined' if name == 'replace' else 'ad-layout'), output)
                state.update(name=name, output=output, before=hashes(output), parts=parts)
                window.lib_append_box.setChecked(False)
                window.lib_merge_box.setChecked(name in ('ad-layout','ad-replace','via-ad'))
                window.step_box.setChecked(name == 'copies')
                window.bind_3d_box.setChecked(False)
                window.obj_box.setChecked(False)
                window.schlib_box.setChecked(ad)
                window.pcblib_box.setChecked(ad)
                window.export_target_box.setCurrentIndex(window.export_target_box.findData(target))
                window.kicad_symbol_box.setChecked(name != 'footprint-only')
                window.kicad_footprint_box.setChecked(True)
                window.kicad_lib_merge_box.setChecked(symbols or footprints)
                window.kicad_merge_symbol_box.setChecked(symbols)
                window.kicad_merge_footprint_box.setChecked(footprints)
                window.kicad_electrical_box.setCurrentIndex(window.kicad_electrical_box.findData(
                    'input' if name == 'merge-symbols' else 'source'))
                window.kicad_symbol_name_input.setText('Symbols' if name == 'copies' else 'LCSC3D')
                window.kicad_footprint_name_input.setText('Pads' if name == 'copies' else 'LCSC3D')
                window.keep_kicad_symbol_box.setChecked(name == 'copies')
                window.keep_kicad_footprint_box.setChecked(name == 'copies')
                window.schlib_name_input.setText('lcsc3d_v2_3_0_hb')
                window.pcblib_name_input.setText('lcsc3d_v2_3_0_hb')
                window.symbol_style_box.setCurrentIndex(window.symbol_style_box.findData('ad'))
                window.path_input.setText(str(output))
                window.input.setPlainText('\n'.join(parts))
                assert window.load_queue()
                expected = ('SCHLIB', 'PCBLIB') if ad else ('KICAD_FOOTPRINT',) if name == 'footprint-only' else ('KICAD_SYMBOL', 'KICAD_FOOTPRINT')
                if name == 'copies': expected = ('STEP',) + expected
                assert window.export_options().formats == expected
                window.start_batch()
                state['phase'] = 'wait'
            elif state['phase'] == 'preview':
                assert window.symbol_view.load_state[0] != 'error', window.symbol_view.load_state
                if window.symbol_view.load_state[0] == 'ready':
                    assert window.symbol_view.document.symbol_style == 'kicad'
                    assert not window.symbol_style_row.isVisible() and window.kicad_style_hint.isVisible()
                    state['phase'] = 'inspect-preview'
                    def inspected(fills):
                        try:
                            details = json.loads(fills)
                            report['preview_geometry'] = details['geometry']
                            fills = details['fills']
                        except (ValueError, TypeError):
                            finish('KiCad preview did not return inspectable SVG styles')
                            return
                        if not fills or any(row != ['rgb(255, 255, 194)', '1'] for row in fills):
                            finish('KiCad rectangle preview fill mismatch: ' + str(fills))
                            return
                        report['kicad_preview_verified'] = True
                        report['preview_rectangle_fills'] = fills
                        # Chromium's DOM-ready message can precede the native
                        # compositor's painted frame. Capture on a later tick.
                        state.update(phase='capture-preview', capture_after=time.monotonic() + 0.6)
                    window.symbol_view.page().runJavaScript(
                        "JSON.stringify({geometry:window.getSvgPreviewState(), fills:[...document.querySelectorAll('svg g rect')].map(n=>{const s=getComputedStyle(n);return [s.fill,s.fillOpacity];})})", inspected)
            elif state['phase'] == 'capture-preview' and time.monotonic() >= state['capture_after']:
                snapshot = window.symbol_view.grab().toImage()
                body_pixels = sum(1 for x in range(0, snapshot.width(), 4) for y in range(0, snapshot.height(), 4)
                    if (color := snapshot.pixelColor(x, y)).red() > 230 and color.green() > 230 and color.blue() < 225)
                if body_pixels > 50:
                    window.grab().save(str(root / 'kicad-preview.png'))
                    report['preview_painted_pixels_verified'] = True
                    from lcsc3d.ui.library_preview import build_library_preview
                    window.export_target_box.setCurrentIndex(window.export_target_box.findData('ad'))
                    window.schlib_box.setChecked(True)
                    window.pcblib_box.setChecked(True)
                    window.lib_merge_box.setChecked(True)
                    window.path_input.setText(str(root/'ad-layout'))
                    window.input.setPlainText('\n'.join(row.part for row in state['layout_rows']))
                    window.load_queue()
                    for index,row in enumerate(state['layout_rows']): window.update_result(index,row)
                    window.complete_batch(state['layout_rows'])
                    window.current_preview = 'C5879483'
                    preview = build_library_preview(json.loads((root/'fixtures/C5879483_svgs.json').read_text('utf-8')), 'C5879483')
                    window.library_cache['C5879483'] = preview
                    window.display_library_preview('C5879483', preview)
                    state['phase'] = 'ad-preview'
                elif time.monotonic() > state['capture_after'] + 3:
                    snapshot.save(str(root / 'preview-missing-pixels.png'))
                    window.grab().save(str(root / 'window-missing-pixels.png'))
                    finish('KiCad preview has no painted symbol pixels')
            elif state['phase'] == 'ad-preview' and window.symbol_view.load_state[0] == 'ready':
                state['phase'] = 'inspect-ad'
                def inspect_pins(value):
                    try:
                        pins = {p[0]:p[1:] for p in json.loads(value)}
                        assert pins == {'1':[370,300,390,300,370,300], '2':[430,300,410,300,430,300]}, pins
                        report['reversed_pin_preview_verified'] = True
                        state.update(phase='capture-ad', capture_after=time.monotonic()+0.6)
                    except Exception as exc: finish('Pin preview: '+str(exc))
                window.symbol_view.page().runJavaScript('''JSON.stringify([...document.querySelectorAll('g[c_partid=part_pin]')].map(pin=>{
                    const p=pin.querySelector('path'),d=pin.querySelector('circle[c_pindot]');
                    const a=p.getPointAtLength(0),b=p.getPointAtLength(p.getTotalLength());
                    return [pin.getAttribute('c_spicepin'),a.x,a.y,b.x,b.y,d.cx.baseVal.value,d.cy.baseVal.value];}))''', inspect_pins)
            elif state['phase'] == 'capture-ad' and time.monotonic() >= state['capture_after']:
                verify_layout()
                window.grab().save(str(root/'ad-reversed-and-layout.png'))
                timer.stop()
                window.export_card.installEventFilter(observer)
                check_merge_frames()
            elif state['phase'] == 'wait' and not window.batch_running and not (window.worker and window.worker.isRunning()):
                rows = [window.results[p] for p in state['parts']]
                assert all(r.status == '成功' for r in rows), [r.message for r in rows]
                assert all(Path(p).is_file() for r in rows for p in r.files)
                if state['name'] in ('combined','replace'):
                    assert len(list(state['output'].glob('*.pretty/*.kicad_mod'))) == 1
                if state['name'] in ('replace','ad-replace'):
                    assert hashes(state['output']) != state['before']
                if state['name'] == 'merge-footprints':
                    window.grab().save(str(root / 'kicad-controls.png'))
                if state['name'] == 'copies':
                    window.grab().save(str(root/'kicad-copies-controls.png'))
                if state['name'] == 'ad-layout':
                    state['layout_rows'] = rows
                if state['name'] in ('via-ad','via-kicad'):
                    window.grab().save(str(root/(state['name']+'.png')))
                report['cases'].append({'name': state['name'], 'statuses': [r.status for r in rows],
                                        'files': sorted(set(p for r in rows for p in r.files)), 'success': True})
                state['phase'] = 'next'
        except Exception as exc:
            finish(type(exc).__name__ + ': ' + str(exc))
    timer.timeout.connect(tick)
    timer.start()
