"""Capture Material UI and exercise navigation using local, public fixtures only."""
import json
from pathlib import Path
import sys
import time
import traceback

from PySide6.QtCore import Qt, QTimer, QPoint, QUrl
from PySide6.QtWidgets import QApplication
from PySide6.QtTest import QTest
from lcsc3d.core.app_settings import Preferences
from lcsc3d.ui.app_theme import theme_manager
from lcsc3d.ui.export_targets import ExportTargetsDialog
from lcsc3d.ui.favorites import LoginDialog
from lcsc3d.ui.library_preview import build_library_preview
from lcsc3d.ui.settings_ui import SettingsDialog
from lcsc3d.ui.ui_components import HelpDialog
from lcsc3d.ui.shell_ui import notify, choose_path


def start(window, folder):
    from lcsc3d.ui import main
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
        visible = [item for item in app.topLevelWidgets() if item.isVisible() and item.windowType() != Qt.ToolTip]
        assert visible == [window], 'An extra native window was opened'
        assert app.activeModalWidget() is None
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
            window.apply_preferences(Preferences(accent_text_color='#ffffff'))
            # Match the five-row layout regression's reference window size;
            # the startup size is clamped to the runner's available desktop.
            window.resize(1380, 880)
            window.input.setPlainText('\n'.join(['C2040', 'C20197'] + ['C' + str(10000 + n) for n in range(38)]))
            window.load_queue()
            window.table.setFocus()
            settle()
            QTest.qWait(100)
            visible_rows = window.table.viewport().height() // window.table.rowHeight(0)
            report['queue_geometry'] = dict(width=window.width(), height=window.height(),
                visible_rows=visible_rows, outer_scroll_max=window.content_scroll.verticalScrollBar().maximum())
            assert visible_rows >= 5 and window.content_scroll.verticalScrollBar().maximum() == 0, (
                visible_rows, window.size(), window.content_scroll.verticalScrollBar().maximum())
            assert window.export_card.isAncestorOf(window.progress_bar)
            assert window.export_card.isAncestorOf(window.run_status)
            report['queue_layout'] = dict(parts=len(window.ids), visible_rows=visible_rows,
                row_height=window.table.rowHeight(0), header_height=window.table.horizontalHeader().height(),
                scrollbar_width=window.table.verticalScrollBar().width(), progress_in_export_card=True)
            capture(window, 'workspace-queue-white-text')
            window.input.setPlainText('C2040\nC20197')
            window.load_queue()
            window.table.item(1, main.DOWNLOAD_COLUMN).setCheckState(Qt.Unchecked)
            window.apply_preferences(Preferences(theme_mode='light'))
            normal_geometry, hwnd = window.geometry(), int(window.winId())
            window.title_bar.maximize_button.click()
            settle()
            assert window.isMaximized() and window.title_bar.maximize_button.action == 'restore'
            assert window.screen().availableGeometry().contains(window.geometry())
            capture(window, 'workspace-maximized')
            window.title_bar.maximize_button.click()
            settle()
            assert not window.isMaximized() and window.geometry() == normal_geometry
            window.title_bar.minimize_button.click()
            settle()
            assert window.isMinimized()
            window.showNormal()
            settle()
            assert int(window.winId()) == hwnd and window.geometry() == normal_geometry
            report['integrated_window_controls'] = report['maximize_respects_work_area'] = True
            report['minimize_restore_preserves_window'] = True
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
            for index, name in enumerate(('appearance', 'network', 'diagnostics', 'about', 'backups')):
                settings.section_buttons[index].click()
                capture(window, 'settings-' + name)
            assert settings.star_button.text() == '⭐点个Star⭐'
            assert settings.sponsor_button.text() == '🍔赞助作者🍔'
            settings.section_buttons[0].click()
            for combo, name in ((settings.text_color_combo, 'color'), (settings.font_combo, 'font')):
                combo.showPopup()
                settle()
                panel = combo.popup_frame
                assert panel.width() == combo.width()
                assert panel.pos() == combo.mapTo(window, QPoint(0, combo.height() + 4))
                capture(window, name + '-dropdown')
                combo.hidePopup()
            settings.color_button.click()
            capture(window, 'color-picker')
            settings.color_page.reject()
            settings.section_buttons[3].click()
            settings.sponsor_button.click()
            capture(window, 'sponsorship')
            settings.sponsorship_dialog.reject()
            settings.reject()
            picker_root = destination / 'picker-files'
            for name in ('Models', 'Symbols', 'Footprints'):
                (picker_root / name).mkdir(parents=True, exist_ok=True)
            window.path_input.setText(str(picker_root))
            window.choose_folder()
            picker = window.folder_page
            picker.setSidebarUrls([QUrl.fromLocalFile(str(picker_root)),
                                   QUrl.fromLocalFile(str(picker_root / 'Models'))])
            for mode, language, size, name in (('light', 'zh_CN', 13, 'folder-picker'),
                                               ('dark', 'zh_CN', 13, 'folder-picker-dark'),
                                               ('light', 'en_US', 24, 'folder-picker-English-large')):
                window.apply_preferences(Preferences(theme_mode=mode, language=language, font_size=size))
                settle()
                for button in picker.toolbar_buttons.values():
                    frame = window._page_host.frames[picker]
                    frame.ensureWidgetVisible(button)
                    settle()
                    pixels = button.grab().toImage()
                    expected = theme_manager().tokens['accent_ink' if button.isEnabled() else 'disabled']
                    half = round(button.iconSize().width() * pixels.devicePixelRatio() / 2)
                    cx, cy = pixels.width() // 2, pixels.height() // 2
                    ink = sum(max(abs(int(pixels.pixelColor(x, y).name()[i:i + 2], 16) - int(expected[i:i + 2], 16))
                                  for i in (1, 3, 5)) < 30
                              for y in range(cy - half, cy + half) for x in range(cx - half, cx + half))
                    assert ink > 15, 'Missing file toolbar icon: ' + button.objectName()
                assert picker.locations_panel.geometry().right() < picker.listing_panel.geometry().left()
                capture(window, name)
            picker.toolbar_buttons['toParentButton'].click()
            assert Path(picker.directory().absolutePath()).resolve() == picker_root.parent.resolve()
            picker.toolbar_buttons['backButton'].click()
            assert Path(picker.directory().absolutePath()).resolve() == picker_root.resolve()
            picker.toolbar_buttons['listModeButton'].click()
            assert picker.viewMode() == picker.ViewMode.List
            picker.toolbar_buttons['detailModeButton'].click()
            assert picker.viewMode() == picker.ViewMode.Detail
            picker.selectFile(str(picker_root / 'Models'))
            picker.accept()
            assert Path(window.path_input.text()).resolve() == (picker_root / 'Models').resolve()
            report['file_picker_icons_visible'] = report['file_picker_material_panels'] = True
            report['file_picker_navigation_and_selection'] = True
            window.apply_preferences(Preferences(theme_mode='light'))
            library = picker_root / 'Symbols' / 'Example.SchLib'
            library.write_bytes(b'File picker fixture; not an exported library.')
            selected = []
            picker = choose_path(window, 'SchLib', str(library.parent), selected.append, filter='AD (*.SchLib)')
            picker.setSidebarUrls([QUrl.fromLocalFile(str(picker_root))])
            picker.selectFile(str(library))
            capture(window, 'library-picker')
            picker.accept()
            assert [Path(value).resolve() for value in selected] == [library.resolve()]
            report['file_picker_library_filter_and_selection'] = True
            login = LoginDialog(window)
            login.show()  # Do not call begin(): no QR or account requests.
            for index, name in enumerate(('qr', 'password', 'sms')):
                login.login_tabs.setCurrentIndex(index)
                capture(window, 'login-' + name)
            login.reject()
            targets = ExportTargetsDialog({}, window)
            targets.show()
            capture(window, 'append-targets')
            targets.reject()
            help_window = HelpDialog(window)
            help_window.setText('<h2>LCSC3D</h2><p>Material UI verification</p>')
            help_window.show()
            capture(window, 'help')
            help_window.reject()
            window.open_favorites()
            store = window.favorites_dialog
            assert not store.tabs.tabBar().drawBase()
            assert store.product_splitter.handleWidth() == window.workspace_splitter.handleWidth()
            for part, title in (('C2040', 'RP2040'), ('C20197', '4D03WGJ0102T5E')):
                store.append_row(store.search_table, {'part': part, 'title': title})
            store.search_table.item(0, 0).setCheckState(Qt.Checked)
            capture(window, 'store-light')
            window.apply_preferences(Preferences(theme_mode='dark', accent_color='#7c3aed'))
            capture(window, 'store-dark')
            store.client.account = {'name': 'Demo account'}
            window.refresh_store_account()
            window.open_account()
            settle()
            assert window.account_menu.width() == window.account_button.width()
            assert len({button.width() for button in window.account_menu.buttons}) == 1
            capture(window, 'account-menu')
            window.account_menu.close()
            store.client.account = None
            window.refresh_store_account()
            notice = notify(window, '加入下载列表成功', '元件已加入下载列表，可返回工作台继续操作。', persistent=True)
            notice.animation.setCurrentTime(notice.animation.duration())
            assert notice.y() > window.title_bar.mapTo(notice.parentWidget(), QPoint(0, window.title_bar.height())).y()
            capture(window, 'notification')
            notice.close()
            report['single_native_window'] = report['anchored_equal_width_dropdowns'] = True
            report['shared_splitter_and_tab_base'] = report['original_support_labels'] = True
            window.workspace_button.click()
            window.apply_preferences(Preferences(theme_mode='light'))
            window.set_preview_mode('footprint')
            QTimer.singleShot(200, footprint)
        except Exception:
            finish(traceback.format_exc())

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
        except Exception:
            finish(traceback.format_exc())

    def ready():
        if window.preview_state == 'ready':
            QTimer.singleShot(800, forms)
        elif time.monotonic() >= deadline:
            finish('Symbol preview timed out: ' + window.preview_status.text())
        else:
            QTimer.singleShot(100, ready)
    QTimer.singleShot(100, ready)
