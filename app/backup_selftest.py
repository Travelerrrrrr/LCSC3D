"""Offline backup/branding acceptance tests using only the supplied sandbox."""
from dataclasses import replace
import json
from pathlib import Path
import sys
import time
import uuid

from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import QApplication
from altium import MergedLibrary
from altium_project import commit_user_file
from app_settings import Preferences, get_preferences
from backups import backup_directory, list_backups


def start(window, destination):
    import main
    app = QApplication.instance()
    app.setQuitOnLastWindowClosed(False)
    destination = Path(destination).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    report = dict(version=main.VERSION, frozen=bool(getattr(sys, 'frozen', False)),
                  saved_session_accessed=False, network_requests=False, screenshots=[])

    def settle():
        for _ in range(5):
            app.processEvents()

    def wait(settings):
        deadline = time.monotonic() + 10
        while settings.worker is not None and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(.01)
        settle()
        assert settings.worker is None, 'Backup worker timeout'

    def capture(name):
        settle()
        for notice in window.notifications.toasts:
            notice.animation.setCurrentTime(notice.animation.duration())
        settle()
        visible = [item for item in app.topLevelWidgets() if item.isVisible() and item.windowType() != Qt.ToolTip]
        assert visible == [window]
        assert window.grab().save(str(destination / (name + '.png')))
        report['screenshots'].append(name + '.png')

    def settings_page():
        window.open_settings()
        settings = window.settings_dialog
        settings.section_buttons[4].click()
        settle()
        return settings, settings.backup_panel

    def restore(panel, row, target):
        panel.table.selectRow(next(i for i, item in enumerate(panel.rows) if item.id == row.id))
        panel.restore_button.click()
        page = panel.restore_page
        page.target.setText(str(target))
        page.restore_button.click()
        wait(panel.settings)
        assert hasattr(page, 'confirmation')
        return page

    def verify():
        try:
            assert window.preferences == Preferences()
            assert window.preferences.theme_mode == 'light' and window.preferences.accent_color == '#087af5'
            report['light_blue_defaults'] = True
            capture('brand-workspace-light')
            settings, panel = settings_page()
            assert panel.auto_box.isChecked() and not panel.rows
            capture('backups-empty')
            settings.reject()
            source = json.loads((destination / 'fixtures/C2040.json').read_text('utf-8'))
            exports = destination / 'exports'
            exports.mkdir(exist_ok=True)
            originals = {}
            for suffix in ('SchLib', 'PcbLib'):
                lib = MergedLibrary(suffix.upper(), merge_pcb=True)
                lib.add(source, 'C2040', lambda: None)
                payload = lib.build(['C2040'], lambda: None)
                path = exports / ('Example.' + suffix)
                path.write_bytes(payload)
                # Back up the original valid library before a simulated update.
                commit_user_file(path, payload, payload + b'updated')
                originals[suffix] = payload
            project = exports / 'Example.PrjPcb'
            old, new = b'[Design]\r\nName=original\r\n', b'[Design]\r\nName=updated\r\n'
            project.write_bytes(old)
            commit_user_file(project, old, new)
            settings, panel = settings_page()
            assert len(panel.rows) == 3 and all(row.original_path for row in panel.rows)
            assert sum(row.stored_size for row in panel.rows) > sum(row.size for row in panel.rows)
            capture('backups-light')
            panel.auto_box.setChecked(False)
            window.settings_enabled = True
            settings.save_button.click()
            window.settings_enabled = False
            assert not window.preferences.auto_backup and not get_preferences().auto_backup
            assert json.loads(main.SETTINGS_PATH.read_text('utf-8'))['auto_backup'] is False
            window.apply_preferences(replace(window.preferences, auto_backup=True))
            window.settings_enabled = True
            window._restore_settings()
            window.settings_enabled = False
            assert not window.preferences.auto_backup and not get_preferences().auto_backup
            changed = new + b'User=changed\r\n'
            commit_user_file(project, new, changed)
            assert len(list_backups()) == 3
            report['automatic_backup_switch_saved'] = True
            settings, panel = settings_page()
            assert not panel.auto_box.isChecked()
            row = next(row for row in panel.rows if row.original_path == str(project))
            page = restore(panel, row, project)
            capture('restore-confirmation')
            page.confirmation.action_buttons[0].click()
            assert project.read_bytes() == changed
            page.restore_button.click()
            wait(settings)
            page.confirmation.action_buttons[1].click()
            wait(settings)
            assert project.read_bytes() == old
            assert len(panel.rows) == 4 and any(item.path.read_bytes() == changed for item in panel.rows)
            report['restore_cancel_and_safety_backup'] = True
            for suffix in ('SchLib', 'PcbLib'):
                path = exports / ('Example.' + suffix)
                row = next(row for row in panel.rows if row.original_path == str(path))
                page = restore(panel, row, path)
                page.confirmation.action_buttons[1].click()
                wait(settings)
                assert path.read_bytes() == originals[suffix]
            report['both_library_formats_restored'] = True
            legacy = backup_directory() / (uuid.uuid4().hex + '-Legacy.SchLib')
            legacy.write_bytes(originals['SchLib'])
            panel.refresh()
            row = next(row for row in panel.rows if row.legacy)
            recovered = exports / 'Recovered.SchLib'
            page = restore(panel, row, recovered)
            page.confirmation.action_buttons[1].click()
            wait(settings)
            assert recovered.read_bytes() == originals['SchLib']
            report['legacy_restore_to_new_file'] = True
            for notice in list(window.notifications.toasts):
                notice.close()
            window.apply_preferences(replace(window.preferences, theme_mode='dark'))
            capture('backups-dark')
            window.apply_preferences(replace(window.preferences, theme_mode='light', language='en_US', font_size=24))
            window.resize(1060, 740)
            settle()
            settings.pages.currentWidget().ensureWidgetVisible(panel.clear_button)
            capture('backups-english-large')
            assert window.width() <= 1060
            window.apply_preferences(replace(window.preferences, theme_mode='light', language='zh_CN', font_size=13))
            window.resize(1380, 880)
            row = next(item for item in panel.rows if not item.legacy)
            row.path.write_bytes(b'corrupted')
            panel.table.selectRow(next(i for i, item in enumerate(panel.rows) if item.id == row.id))
            panel.restore_button.click()
            page = panel.restore_page
            page.target.setText(str(project if row.path.suffix.lower() == '.prjpcb' else exports / ('Example' + row.path.suffix)))
            target = Path(page.target.text())
            before = target.read_bytes()
            page.restore_button.click()
            wait(settings)
            assert target.read_bytes() == before
            assert not hasattr(page, 'confirmation')
            page.reject()
            report['corrupt_backup_refused'] = True
            for notice in list(window.notifications.toasts):
                notice.close()
            panel.refresh()
            count = len(panel.rows)
            panel.clear_button.click()
            capture('clear-confirmation')
            panel.confirmation.action_buttons[0].click()
            assert len(list_backups()) == count
            panel.clear_button.click()
            panel.confirmation.action_buttons[1].click()
            wait(settings)
            assert not list_backups() and '0 B' in panel.usage.text()
            assert recovered.read_bytes() == originals['SchLib'] and project.read_bytes() == old
            report['clear_cancel_space_and_originals_preserved'] = True
            panel.auto_box.setChecked(True)
            settings.save_button.click()
            commit_user_file(project, old, new)
            assert len(list_backups()) == 1
            report['backups_continue_after_clear'] = True
            report.update(success=True, error='')
        except Exception as exc:
            report.update(success=False, error=str(exc) or type(exc).__name__)
        (destination / 'backups-verification.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        window.settings_enabled = False
        window.close()
        app.exit(0 if report['success'] else 1)
    QTimer.singleShot(0, verify)
