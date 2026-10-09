"""Capture the actual application with public products and empty login forms."""
from __future__ import annotations

import json
from pathlib import Path
import sys
import time

from PySide6.QtCore import QPoint, Qt, QTimer
from PySide6.QtWidgets import QApplication

from favorites import LoginDialog


def start(window, destination):
    from main import DOWNLOAD_COLUMN, VERSION
    QApplication.instance().setQuitOnLastWindowClosed(False)
    destination = Path(destination).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    window.path_input.setText('下载目录')
    window.input.setPlainText('C456013\nC2040\nC20197\nC163691')
    for box in (window.step_box, window.obj_box, window.schlib_box, window.pcblib_box):
        box.setChecked(True)
    window.load_queue()
    window.table.item(2, DOWNLOAD_COLUMN).setCheckState(Qt.Unchecked)
    window.table.item(3, DOWNLOAD_COLUMN).setCheckState(Qt.Unchecked)
    timer = QTimer(window)
    timer.setInterval(100)
    deadline = time.monotonic() + 180
    state = {'phase': 'main', 'done': False}
    report = {'version': VERSION, 'frozen': bool(getattr(sys, 'frozen', False)),
              'public_products_only': True, 'saved_session_accessed': False, 'screenshots': []}

    def capture(widget, name):
        pixmap = widget.grab()
        if name in ('symbol', 'footprint'):
            view = window.symbol_view if name == 'symbol' else window.footprint_view
            origin = view.mapTo(widget, QPoint(0, 0))
            scale = pixmap.devicePixelRatio()
            image = pixmap.toImage().copy(int(origin.x() * scale), int(origin.y() * scale),
                                         int(view.width() * scale), int(view.height() * scale))
            background = 255 if name == 'symbol' else 0
            visible = sum(max(abs(channel - background) for channel in image.pixelColor(x, y).getRgb()[:3]) > 32
                          for y in range(8, image.height() - 8, 8)
                          for x in range(8, image.width() - 8, 8))
            assert visible > 15, name + ': preview pixels are blank'
        assert pixmap.save(str(destination / (name + '.png'))), name
        report['screenshots'].append(name + '.png')

    def vector_frame_ready(phase):
        # DOM readiness precedes WebEngine's composited frame on Windows.
        if state.get('frame_phase') != phase:
            state.update(frame_phase=phase, frame_ready_at=time.monotonic())
        return time.monotonic() - state['frame_ready_at'] >= 2

    def finish(error=''):
        if state['done']:
            return
        state['done'] = True
        state['code'] = 1 if error else 0
        timer.stop()
        report.update({'success': not error, 'error': error})
        (destination / 'capture-verification.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        window.close()
        QTimer.singleShot(50, exit_when_idle)

    def exit_when_idle():
        if any(worker is not None and worker.isRunning() for worker in
               (window.info_worker, window.model_worker, window.library_worker)) or window.favorites_dialog and window.favorites_dialog.has_jobs():
            QTimer.singleShot(50, exit_when_idle)
        else:
            QApplication.instance().exit(state['code'])

    def check():
        try:
            if time.monotonic() > deadline:
                finish('Timed out in ' + state['phase'])
                return
            dialog = window.favorites_dialog
            phase = state['phase']
            if phase == 'main' and window.preview_state == 'ready' and window.info_worker is None:
                assert window.current_preview == 'C456013'
                capture(window, 'main')
                window.open_settings()
                settings = window.settings_dialog
                QApplication.processEvents()
                assert settings.store_proxy_combo.currentData() == settings.update_proxy_combo.currentData() == 'system'
                assert settings.log_level_combo.currentData() == 'DEBUG'
                capture(settings, 'settings')
                settings.reject()
                window.set_preview_mode('symbol')
                state['phase'] = 'symbol'
            elif phase == 'symbol' and window.preview_state == 'ready' and window.library_worker is None:
                if not vector_frame_ready(phase):
                    return
                capture(window, 'symbol')
                window.set_preview_mode('footprint')
                state['phase'] = 'footprint'
            elif phase == 'footprint' and window.preview_state == 'ready':
                if not vector_frame_ready(phase):
                    return
                capture(window, 'footprint')
                window.open_favorites('search')
                dialog = window.favorites_dialog
                assert not dialog.vault.exists() and not dialog.client.account
                dialog.search_input.setText('dcdc')
                dialog.start_search()
                state['phase'] = 'store-search'
            elif phase == 'store-search' and not dialog.searching and not dialog.has_jobs():
                assert len(dialog.search_items) == 50 and dialog.current_product
                capture(dialog, 'store-search')
                dialog.search_input.setText('C499531')
                dialog.start_search()
                state['phase'] = 'store-details'
            elif phase == 'store-details' and not dialog.searching and not dialog.has_jobs():
                assert dialog.current_product and dialog.current_product['part'] == 'C499531'
                assert dialog.search_table.cellWidget(0, 5).currentData() == 1
                capture(dialog, 'store-details')
                dialog.detail_scroll.verticalScrollBar().setValue(dialog.detail_scroll.verticalScrollBar().maximum())
                state['phase'] = 'store-parameters'
            elif phase == 'store-parameters':
                capture(dialog, 'store-parameters')
                dialog.gallery_button.click()
                state['phase'] = 'gallery'
            elif phase == 'gallery' and dialog.galleries and dialog.galleries[0].view.photo:
                gallery = dialog.galleries[0]
                assert gallery.thumbnails.count() >= 3
                capture(gallery, 'gallery')
                gallery.close()
                login = LoginDialog(window)
                dialog.login_dialog = login
                login.login_tabs.setCurrentIndex(1)
                login.show()
                login.begin()
                state['phase'] = 'password'
            elif phase == 'password':
                assert not dialog.login_dialog.account_input.text() and not dialog.login_dialog.password_input.text()
                capture(dialog.login_dialog, 'login-password')
                dialog.login_dialog.login_tabs.setCurrentIndex(2)
                state['phase'] = 'sms'
            elif phase == 'sms':
                assert not dialog.login_dialog.phone_input.text() and not dialog.login_dialog.sms_input.text()
                capture(dialog.login_dialog, 'login-sms')
                dialog.login_dialog.reject()
                finish()
        except Exception as exc:
            finish(state['phase'] + ': ' + (str(exc) or type(exc).__name__))

    timer.timeout.connect(check)
    timer.start()
