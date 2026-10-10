"""Exercise settings and logs in an isolated folder in the frozen app."""
import json
from pathlib import Path
import sys
import time
import zipfile

from PySide6.QtCore import QTimer, QObject, Slot, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QApplication, QMessageBox

from app_settings import Preferences, get_preferences, set_preferences
from app_logging import get_log_directory, log_event, set_log_level, configure_logging


def start(window, destination):
    main = sys.modules[window.__class__.__module__]
    destination = Path(destination).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    QApplication.instance().setQuitOnLastWindowClosed(False)
    original_path = main.SETTINGS_PATH
    main.SETTINGS_PATH = destination / 'LCSC3D-settings.json'
    window.settings_enabled = True
    window.path_input.setText('下载目录')
    report = {'version': main.VERSION, 'frozen': bool(getattr(sys, 'frozen', False)),
              'isolated_settings': True, 'saved_session_accessed': False}

    class DirectoryReceiver(QObject):
        @Slot(QUrl)
        def opened(self, url):
            report['opened_bundle_directory'] = url.toLocalFile()

    receiver = DirectoryReceiver(window)
    QDesktopServices.setUrlHandler('file', receiver, 'opened')

    class RepositoryReceiver(QObject):
        @Slot(QUrl)
        def opened(self, url):
            report['opened_repository'] = url.toString()

    repository_receiver = RepositoryReceiver(window)
    QDesktopServices.setUrlHandler('https', repository_receiver, 'opened')

    def finish(error=''):
        QDesktopServices.unsetUrlHandler('file')
        QDesktopServices.unsetUrlHandler('https')
        report.update(success=not error, error=error)
        (destination / 'settings-verification.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        window.settings_enabled = False
        main.SETTINGS_PATH = original_path
        window.close()
        QApplication.instance().exit(1 if error else 0)

    def verify_package(deadline):
        dialog = window.settings_dialog
        if dialog.worker is not None:
            if time.monotonic() > deadline:
                # Keep the thread owned until it finishes even when reporting failure.
                dialog.worker.finished.connect(lambda: finish('Log package timed out'))
            else:
                QTimer.singleShot(25, lambda: verify_package(deadline))
            return
        try:
            from app_paths import data_directory
            archive_path = next((data_directory() / 'diagnostics').glob('*.zip'))
            assert Path(report['opened_bundle_directory']) == archive_path.parent
            with zipfile.ZipFile(archive_path) as archive:
                assert archive.testzip() is None
                assert {'diagnostics.json', '反馈说明.txt', 'logs/LCSC3D.log',
                        'logs/LCSC3D.log.1', 'logs/LCSC3D-update.log', 'logs/LCSC3D-crash-12345.log'} <= set(archive.namelist())
                metadata = json.loads(archive.read('diagnostics.json'))
                assert metadata['application']['version'] == main.VERSION
                assert metadata['environment']['frozen'] == report['frozen']
                assert metadata['preferences'] == get_preferences().to_mapping()
                assert b'settings.self_test_before_clear' in archive.read('logs/LCSC3D.log')
                assert not any('store-session' in name or 'settings.json' in name for name in archive.namelist())
            report['log_package_verified'] = True
            report['log_package'] = str(archive_path)
            folder = get_log_directory()
            markers = list(folder.glob('run-*.json'))
            assert markers
            def answer_clear(choice):
                message = QApplication.activeModalWidget()
                if isinstance(message, QMessageBox):
                    message.button(choice).click()
            QTimer.singleShot(25, lambda: answer_clear(QMessageBox.No))
            dialog.clear_log_button.click()
            assert 'settings.self_test_before_clear' in (folder / 'LCSC3D.log').read_text(encoding='utf-8')
            QTimer.singleShot(25, lambda: answer_clear(QMessageBox.Yes))
            dialog.clear_log_button.click()
            assert '日志已清除' in dialog.status.text()
            assert archive_path.exists() and all(path.exists() for path in markers)
            assert not (folder / 'LCSC3D.log.1').exists()
            assert not (folder / 'LCSC3D-update.log').exists()
            assert not (folder / 'LCSC3D-crash-12345.log').exists()
            assert log_event('ERROR', 'settings.self_test_after_clear')
            contents = (folder / 'LCSC3D.log').read_text(encoding='utf-8')
            assert 'settings.self_test_before_clear' not in contents
            assert 'settings.self_test_after_clear' in contents
            report['log_clear_verified'] = True
        except Exception as exc:
            finish(type(exc).__name__ + ': ' + str(exc))
            return
        finish()

    def check():
        try:
            window.settings_button.click()
            dialog = window.settings_dialog
            QApplication.processEvents()
            assert dialog.isVisible()
            assert dialog.store_proxy_combo.currentData() == dialog.update_proxy_combo.currentData() == 'system'
            assert dialog.log_level_combo.currentData() == 'DEBUG'
            assert dialog.grab().save(str(destination / '设置.png'))
            assert window.grab().save(str(destination / '主窗口.png'))
            dialog.star_button.click()
            assert report['opened_repository'] == 'https://github.com/Travelerrrrrr/LCSC3D'
            dialog.store_proxy_combo.setCurrentIndex(1)
            dialog.sponsor_button.click()
            QApplication.processEvents()
            sponsorship = dialog.sponsorship_dialog
            assert sponsorship.isVisible() and sponsorship.parent() is dialog
            assert len(sponsorship.code_labels) == 2
            assert all(label.isVisible() and not label.pixmap().isNull()
                       for label in sponsorship.code_labels)
            assert sponsorship.grab().save(str(destination / '赞助与支持.png'))
            sponsorship.close_button.click()
            assert not sponsorship.isVisible() and dialog.isVisible()
            assert dialog.store_proxy_combo.currentData() == 'direct'
            assert get_preferences() == Preferences()
            dialog.sponsor_button.click()
            assert dialog.sponsorship_dialog is sponsorship and sponsorship.isVisible()
            sponsorship.reject()
            dialog.store_proxy_combo.setCurrentIndex(0)
            report['support_buttons_verified'] = report['bundled_payment_codes_verified'] = True
            report['support_preserves_unsaved_preferences'] = True
            report['default_system_proxies'] = report['default_debug'] = True
            window.schlib_box.setChecked(True)
            window.merge_schlib_box.setChecked(True)
            window.keep_schlib_box.setChecked(True)
            window.export_targets = {'schlib_target': 'D:/库/原库.SchLib', 'pcblib_target': '',
                                     'project_path': 'D:/工程/测试.PrjPcb',
                                     'keep_individual': True, 'import_existing_to_project': True}
            window.lib_append_box.setChecked(True)
            dialog.store_proxy_combo.setCurrentIndex(1)
            dialog.log_level_combo.setCurrentIndex(dialog.log_level_combo.findData('ERROR'))
            dialog.save_button.click()
            expected = Preferences(store_proxy='direct', update_proxy='system', log_level='ERROR')
            assert get_preferences() == expected
            assert not dialog.isVisible()
            saved = json.loads(main.SETTINGS_PATH.read_text(encoding='utf-8'))
            assert Preferences.from_mapping(saved) == expected
            assert saved['destination'] == '下载目录'
            report['independent_proxy_preferences'] = report['saved_to_disk'] = True
            set_preferences(Preferences())
            set_log_level('DEBUG')
            window._restore_settings()
            assert get_preferences() == expected
            assert window.keep_schlib_box.isChecked()
            assert window.export_targets['schlib_target'] == 'D:/库/原库.SchLib'
            assert window.export_targets['project_path'] == 'D:/工程/测试.PrjPcb'
            assert window.lib_append_box.isChecked() and not window.lib_merge_box.isChecked()
            assert not window.path_input.isEnabled()
            assert window.export_targets['keep_individual'] and window.export_targets['import_existing_to_project']
            report['library_options_restored'] = True
            report['restored_from_disk'] = True
            assert not log_event('DEBUG', 'settings.self_test_debug')
            assert log_event('ERROR', 'settings.self_test_error')
            entries = [json.loads(line) for line in (get_log_directory() / 'LCSC3D.log').read_text(encoding='utf-8').splitlines()]
            assert any(entry['event'] == 'settings.self_test_error' for entry in entries)
            assert not any(entry['event'] == 'settings.self_test_debug' for entry in entries)
            report['log_level_filters_output'] = True
            window.settings_button.click()
            dialog = window.settings_dialog
            assert dialog.store_proxy_combo.currentData() == 'direct'
            assert dialog.log_level_combo.currentData() == 'ERROR'
            dialog.update_proxy_combo.setCurrentIndex(1)
            dialog.log_level_combo.setCurrentIndex(0)
            dialog.cancel_button.click()
            assert get_preferences() == expected
            report['cancel_preserves_preferences'] = True
            from app_paths import data_directory
            local_settings = data_directory() / 'LCSC3D-settings.json'
            main.SETTINGS_PATH = local_settings
            assert window.save_settings()
            assert local_settings.is_file()
            configure_logging(expected.log_level, version=main.VERSION)
            assert (get_log_directory()).is_relative_to(data_directory())
            report['app_data_settings'] = report['app_data_logs'] = True
            if getattr(sys, 'frozen', False):
                assert Path(sys._MEIPASS).resolve().is_relative_to((data_directory() / 'runtime').resolve())
                report['app_data_runtime'] = True
            from app_logging import stop_crash_capture, start_crash_capture
            stop_crash_capture()
            start_crash_capture()
            assert log_event('ERROR', 'settings.self_test_before_clear')
            for name in ('LCSC3D.log.1', 'LCSC3D-update.log', 'LCSC3D-crash-12345.log'):
                (get_log_directory() / name).write_text('synthetic diagnostic\n', encoding='utf-8')
            window.settings_button.click()
            dialog = window.settings_dialog
            assert dialog.package_log_button.isVisible() and dialog.clear_log_button.isVisible()
            dialog.package_log_button.click()
            assert not dialog.clear_log_button.isEnabled()
            QTimer.singleShot(25, lambda: verify_package(time.monotonic() + 15))
        except Exception as exc:
            finish(type(exc).__name__ + ': ' + str(exc))

    QTimer.singleShot(200, check)
