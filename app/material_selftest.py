"""Capture Material UI and exercise navigation using local, public fixtures only."""
import json
from pathlib import Path
import sys
import time

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QApplication
from app_settings import Preferences
from app_theme import theme_manager
from export_targets import ExportTargetsDialog
from favorites import LoginDialog
from library_preview import build_library_preview
from settings_ui import SettingsDialog
from ui_components import HelpDialog


def start(window, folder):
    import main
    destination = Path(folder).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    app = QApplication.instance()
    app.setQuitOnLastWindowClosed(False)
    report = dict(version=main.VERSION, frozen=bool(getattr(sys, 'frozen', False)),
                  network_requests=False, saved_session_accessed=False, screenshots=[])
    window.request_component_info = lambda: None
    for part, title, model in (('C2040', 'RP2040', 'QFN-56'), ('C20197', '4D03WGJ0102T5E', 'R0603-8P')):
        source = json.loads((destination / 'fixtures' / (part + '_svgs.json')).read_text(encoding='utf-8'))
        window.library_cache[part] = build_library_preview(source, part)
        window.component_info[part] = {'title': title, 'model': model}
    window.path_input.setText('D:/LCSC3D/Models')
    window.set_preview_mode('symbol')
    window.input.setPlainText('C2040\nC20197')
    window.load_queue()
    window.table.item(1, main.DOWNLOAD_COLUMN).setCheckState(Qt.Unchecked)
    window.schlib_box.setChecked(True)
    window.pcblib_box.setChecked(True)
    window.apply_preferences(Preferences(theme_mode='light'))
    deadline = time.monotonic() + 40

    def settle():
        for _ in range(5):
            app.processEvents()

    def capture(widget, name):
        settle()
        assert widget.grab().save(str(destination / (name + '.png')))
        report['screenshots'].append(name + '.png')

    def finish(error=''):
        report.update(success=not error, error=error)
        (destination / 'material-verification.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        window.close()
        app.exit(1 if error else 0)

    def forms():
        try:
            settle()
            assert window.content_scroll.verticalScrollBar().maximum() == 0, 'Default workspace must fit without scrolling'
            report['default_workspace_fits'] = True
            capture(window, 'workspace-symbol-light')
            assert 'Qt-Material' in app.styleSheet()
            window.lib_merge_box.setChecked(True)
            capture(window, 'workspace-merge-light')
            window.apply_preferences(Preferences(theme_mode='dark'))
            capture(window, 'workspace-dark')
            window.lib_merge_box.setChecked(False)
            for language, size in (('zh_CN', 13), ('en_US', 24)):
                window.apply_preferences(Preferences(language=language, font_size=size, theme_mode='light'))
                window.resize(1060, 740)
                settle()
                window.content_scroll.ensureWidgetVisible(window.start_button)
                settle()
                viewport = window.content_scroll.viewport()
                assert viewport.rect().contains(window.start_button.mapTo(viewport, window.start_button.rect().center()))
                capture(window, 'workspace-' + language + '-' + str(size))
            window.apply_preferences(Preferences(theme_mode='light'))
            window.resize(1380, 880)
            settings = SettingsDialog(window.preferences, window.apply_preferences, window)
            settings.show()
            for index, name in enumerate(('appearance', 'network', 'diagnostics', 'about')):
                settings.section_buttons[index].click()
                capture(settings, 'settings-' + name)
            settings.reject()
            login = LoginDialog(window)
            login.show()  # Do not call begin(): no QR or account requests.
            for index, name in enumerate(('qr', 'password', 'sms')):
                login.login_tabs.setCurrentIndex(index)
                capture(login, 'login-' + name)
            login.reject()
            targets = ExportTargetsDialog({}, window)
            targets.show()
            capture(targets, 'append-targets')
            targets.reject()
            help_window = HelpDialog(window)
            help_window.setText('<h2>LCSC3D</h2><p>Material UI verification</p>')
            help_window.show()
            capture(help_window, 'help')
            help_window.reject()
            window.set_preview_mode('footprint')
            QTimer.singleShot(200, footprint)
        except Exception as exc:
            finish(str(exc) or type(exc).__name__)

    def footprint():
        if window.preview_state == 'ready':
            QTimer.singleShot(800, finish_footprint)
        elif time.monotonic() >= deadline:
            finish('Footprint preview timed out')
        else:
            QTimer.singleShot(100, footprint)

    def finish_footprint():
        try:
            capture(window, 'workspace-footprint-light')
            report['symbol_and_footprint_ready'] = True
            report['queue_checks_preserved'] = window.table.item(1, main.DOWNLOAD_COLUMN).checkState() == Qt.Unchecked
            assert report['queue_checks_preserved']
            finish()
        except Exception as exc:
            finish(str(exc) or type(exc).__name__)

    def ready():
        if window.preview_state == 'ready':
            QTimer.singleShot(800, forms)
        elif time.monotonic() >= deadline:
            finish('Symbol preview timed out: ' + window.preview_status.text())
        else:
            QTimer.singleShot(100, ready)
    QTimer.singleShot(100, ready)
