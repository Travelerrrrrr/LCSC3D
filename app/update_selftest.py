"""Drive the actual update dialog against the explicitly selected local source."""
import json
from pathlib import Path
import time

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication


def start(window, destination, *, startup=False):
    destination = Path(destination).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    state = {'deadline': time.monotonic() + 120, 'clicked_download': False, 'finished': False}
    report = {'source_kind': 'local_test', 'check_clicked': False, 'download_clicked': False,
              'startup_check': startup, 'notification_shown': False}
    application = QApplication.instance()
    timer = QTimer(window)
    timer.setInterval(25)

    def save():
        (destination / 'ui-verification.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')

    def stop(outcome, error=''):
        if state['finished']:
            return
        state['finished'] = True
        timer.stop()
        report.update(outcome=outcome, error=error)
        dialog = window.update_dialog
        if dialog is not None:
            dialog.grab().save(str(destination / 'update-dialog.png'))
        save()
        window.close()

    def prepared(manifest):
        # This slot runs before the worker's finished signal launches the helper.
        state['finished'] = True
        timer.stop()
        report.update(outcome='prepared', sha256_verified=True, manifest=str(manifest))
        window.update_dialog.grab().save(str(destination / 'update-verified.png'))
        save()

    def poll():
        try:
            if time.monotonic() > state['deadline']:
                stop('timeout')
                return
            dialog = window.update_dialog
            if startup and not state['clicked_download']:
                if window.startup_update_outcome is None:
                    return
                if window.startup_update_outcome in ('latest', 'failed'):
                    assert dialog is None
                    report['silent'] = True
                    stop('no_update_silent' if window.startup_update_outcome == 'latest' else 'check_failed_silent')
                    return
                assert window.startup_update_outcome == 'available'
                assert dialog is not None and dialog.isVisible()
                report['notification_shown'] = True
            if dialog.worker is not None:
                return
            if dialog.release is None:
                if '当前已是最新版本' in dialog.status.text():
                    stop('no_update')
                else:
                    stop('check_failed', dialog.status.text())
            elif not state['clicked_download']:
                assert dialog.install.isEnabled()
                assert '本地测试' in dialog.windowTitle()
                report.update(available_version=dialog.release.version, local_source_visible=True)
                dialog.grab().save(str(destination / 'update-available.png'))
                state['clicked_download'] = True
                report['download_clicked'] = True
                dialog.install.click()
                assert dialog.worker is not None
                dialog.worker.prepared.connect(prepared)
            else:
                stop('download_failed', dialog.status.text())
        except Exception as exc:
            stop('test_failed', type(exc).__name__ + ': ' + str(exc))

    def begin():
        if not startup:
            window.settings_button.click()
            assert window.settings_dialog.isVisible()
            window.settings_dialog.update_button.click()
            assert window.update_dialog.parent() is window.settings_dialog
            report['settings_entry'] = True
            report['check_clicked'] = True
        timer.timeout.connect(poll)
        timer.start()

    application.setQuitOnLastWindowClosed(True)
    QTimer.singleShot(250, begin)
