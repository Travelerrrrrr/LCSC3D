"""LCSC3D. Copyright (C) 2026. SPDX-License-Identifier: AGPL-3.0-or-later."""
from __future__ import annotations

from i18n import text as ui_text, message as ui_message
import argparse
import json
import logging
import os
import sys
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from PySide6.QtCore import Qt, QThread, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices, QFont, QIcon, QPixmap
from PySide6.QtWidgets import (QApplication, QButtonGroup, QFrame, QHBoxLayout, QHeaderView, QProgressBar, QScrollArea, QSizePolicy, QSplitter, QStackedWidget, QVBoxLayout, QWidget, QAbstractItemView)
from localized_widgets import (QCheckBox, QComboBox, QLabel, QLineEdit, QMainWindow, QPlainTextEdit, QPushButton, QTableWidget, QTableWidgetItem)
from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile, QWebEngineSettings
from PySide6.QtWebEngineWidgets import QWebEngineView
from shiboken6 import isValid

from backend import (Cancelled, DownloadError, NetworkApi, Options, Result, download_batch,
                     get_component_metadata, parse_part_numbers, import_existing_libraries)
from model3d import model_reference, read_obj
from library_preview import build_library_preview, VectorPreviewView, SYMBOL_BACKGROUND, FOOTPRINT_BACKGROUND
from update_ui import UpdateDialog, StartupUpdateCheck
from updater import acknowledge_update, cleanup_updates, launch_update
from favorites import FavoritesDialog, normalize_items
from app_settings import Preferences, set_preferences, write_settings, read_settings, initial_log_level
from app_theme import theme_manager, colors, stylesheet
from i18n import set_language, render
from app_logging import (configure_logging, log_event, set_log_level, record_error, traced,
                         new_context, current_context, log_context, contextual, submit_logged,
                         safe_part, install_exception_hooks, log_runtime)
from settings_ui import SettingsDialog
from export_targets import ExportTargetsDialog
from product_preview import ProductPreview
from ui_components import IconButton, title_block, surface, HelpDialog, ColumnSplitter
from shell_ui import PageHost, NotificationCenter, AccountPanel, choose_path, notify
from international_store import storefront_url
from app_paths import data_directory, configure_runtime_paths, updates_directory

VERSION = '2.2.1'
DOWNLOAD_COLUMN, PART_COLUMN, MODEL_COLUMN, RESULT_COLUMN = range(4)
ROOT = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent))
APP_DIR = Path(sys.executable).parent if getattr(sys, 'frozen', False) else Path(__file__).resolve().parent
SETTINGS_PATH = data_directory() / 'LCSC3D-settings.json'
LEGACY_SETTINGS_PATH = APP_DIR / 'LCSC3D-settings.json'

STYLES = stylesheet(colors())


def label(text, role=None):
    widget = QLabel(text)
    if role:
        widget.setObjectName(role)
    return widget


def card():
    return surface()


class BatchWorker(QThread):
    phase = Signal(int, str, str)
    result = Signal(int, object)
    completed = Signal(object)

    def __init__(self, ids: list[str], options: Options, parent=None, rows=None):
        super().__init__(parent)
        self.ids, self.options = ids, options
        self.rows = list(range(len(ids))) if rows is None else rows
        self.cancelled = threading.Event()
        self.log_context = new_context(feature='download')

    @contextual
    def run(self):
        log_event('INFO', 'download.batch_started', count=len(self.ids))
        results = download_batch(self.ids, self.options, self.cancelled,
                                 lambda index, status, title: self.phase.emit(self.rows[index], status, title),
                                 lambda index, result: self.result.emit(self.rows[index], result),
                                 api=NetworkApi(self.cancelled, use_cache=False))
        log_event('INFO', 'download.batch_completed', count=len(results), cancelled=self.cancelled.is_set())
        self.completed.emit(results)


class LibraryImportWorker(QThread):
    completed = Signal(object, str)

    def __init__(self, options, parent):
        super().__init__(parent)
        self.options = options
        self.cancelled = threading.Event()
        self.log_context = new_context(feature='library_import')

    @contextual
    def run(self):
        def check_cancelled():
            if self.cancelled.is_set():
                raise Cancelled()
        try:
            result = import_existing_libraries(self.options, check_cancelled)
        except Cancelled:
            self.completed.emit(None, ui_text('导入已取消，已完成的文件保留'))
        except Exception as exc:
            record_error(exc, 'export.existing_import_failed', stage='import')
            self.completed.emit(None, ui_text('导入已有库失败：') + str(exc))
        else:
            self.completed.emit(result, '')


class ComponentInfoWorker(QThread):
    loaded = Signal(int, str, object, str)

    def __init__(self, ids, revision, parent=None):
        super().__init__(parent)
        self.ids, self.revision = ids, revision
        self.cancelled = threading.Event()
        self.log_context = new_context(feature='component_lookup', revision=revision)

    @contextual
    def run(self):
        remaining = iter(self.ids)
        lock = threading.Lock()

        def query_parts():
            api = None
            while not self.cancelled.is_set():
                with lock:
                    part = next(remaining, None)
                if part is None:
                    return
                try:
                    if api is None:
                        api = NetworkApi(self.cancelled)
                    info = get_component_metadata(part, api)
                    if not self.cancelled.is_set():
                        self.loaded.emit(self.revision, part, info, '')
                except Cancelled:
                    return
                except Exception as exc:
                    if not self.cancelled.is_set():
                        record_error(exc, 'component.lookup_failed', part=safe_part(part))
                        self.loaded.emit(self.revision, part, {}, str(exc))

        with ThreadPoolExecutor(max_workers=3, thread_name_prefix='component-info') as pool:
            tasks = [submit_logged(pool, query_parts) for _ in range(min(3, len(self.ids)))]
            for task in tasks:
                task.result()


class PreviewPage(QWebEnginePage):
    state = Signal(int, str, str)

    def javaScriptConsoleMessage(self, level, message, line, source):
        if message.startswith('LCSC3D_GRAPHICS:'):
            try:
                data = json.loads(message.removeprefix('LCSC3D_GRAPHICS:'))
                with log_context(getattr(self, 'diagnostic_context', {})):
                    log_event('INFO', 'preview.graphics_environment',
                              **{'graphics_' + key: data[key][:160] for key in ('vendor', 'renderer', 'version')
                                 if isinstance(data.get(key), str)})
            except (ValueError, TypeError, AttributeError) as exc:
                record_error(exc, 'preview.graphics_metadata_failed', level='WARNING')
        elif message.startswith('LCSC3D_STATE:'):
            try:
                info = json.loads(message[len('LCSC3D_STATE:'):])
                with log_context(getattr(self, 'diagnostic_context', {})):
                    if info.get('status') == 'error':
                        log_event('ERROR', 'preview.render_failed', viewer='3d',
                                  reason=info.get('diagnostic') if info.get('diagnostic') in
                                  ('webgl_unavailable', 'shader_compile', 'program_link', 'mesh_upload', 'context_lost', 'model_load', 'initialization') else 'renderer_error')
                self.state.emit(int(info['token']), info['status'], info['message'])
            except (ValueError, KeyError) as exc:
                record_error(exc, 'preview.state_parse_failed', viewer='3d')
        elif 'faild to initial webgl' in message.lower():
            log_event('ERROR', 'preview.webgl_initialization_failed', viewer='3d', script_line=line)
            self.state.emit(-1, 'error', ui_text('显卡未能初始化在线预览，可打开商城页面查看'))
        elif getattr(level, 'value', 0) >= 1:
            from app_logging import log_script_error
            with log_context(getattr(self, 'diagnostic_context', {})):
                log_script_error(level, message, line, '3d')


class LibraryPreviewWorker(QThread):
    loaded = Signal(str, object)
    failed = Signal(str, str)

    def __init__(self, part, parent=None):
        super().__init__(parent)
        self.part = part
        self.cancelled = threading.Event()
        self.log_context = new_context(feature='preview', part=safe_part(part), viewer='library')

    @contextual
    def run(self):
        try:
            log_event('DEBUG', 'preview.library_load_started')
            api = NetworkApi(self.cancelled)
            data = api.get_svg_data_of_component(self.part)
            api.check_cancelled()
            preview = build_library_preview(data, self.part)
            api.check_cancelled()
            self.loaded.emit(self.part, preview)
            log_event('DEBUG', 'preview.library_load_completed', symbol_count=len(preview.symbols))
        except Cancelled:
            log_event('INFO', 'preview.library_load_cancelled')
        except Exception as exc:
            record_error(exc, 'preview.library_load_failed')
            self.failed.emit(self.part, str(exc))


class ModelPreviewWorker(QThread):
    loaded = Signal(str, int, object)
    failed = Signal(str, int, str)

    def __init__(self, part, revision, refresh=False, parent=None):
        super().__init__(parent)
        self.part, self.revision, self.refresh = part, revision, refresh
        self.cancelled = threading.Event()
        self.log_context = new_context(feature='preview', part=safe_part(part), viewer='3d', revision=revision)

    @contextual
    def run(self):
        try:
            log_event('DEBUG', 'preview.model_load_started', refresh=self.refresh)
            api = NetworkApi(self.cancelled, use_cache=not self.refresh)
            model = model_reference(api.get_cad_data_of_component(self.part))
            if model is None:
                raise ValueError(ui_text('官方库没有关联的 3D 模型'))
            raw = api.get_raw_3d_model_obj(model.uuid)
            if not raw:
                raise ValueError(ui_text('官方库没有可预览的 OBJ 模型'))
            mesh = read_obj(raw, api.check_cancelled)
            api.check_cancelled()
            self.loaded.emit(self.part, self.revision, mesh.preview_payload())
            log_event('DEBUG', 'preview.model_load_completed')
        except Cancelled:
            log_event('INFO', 'preview.model_load_cancelled')
        except Exception as exc:
            if not self.cancelled.is_set():
                record_error(exc, 'preview.model_load_failed')
                self.failed.emit(self.part, self.revision, str(exc))


class MainWindow(QMainWindow):
    batch_done = Signal()
    preview_changed = Signal(str)

    def __init__(self, settings_enabled=True, *, update_source=None):
        super().__init__()
        self.update_source = update_source
        self.setWindowTitle(ui_text('LCSC3D（本地更新测试）') if update_source is not None else 'LCSC3D')
        self.resize(1380, 880)
        self.setMinimumSize(1060, 740)
        self.setWindowIcon(QIcon(str(ROOT / 'assets' / 'app.ico')))
        self.settings_enabled = settings_enabled
        self.preferences = Preferences()
        self.settings_dialog = None
        self.export_targets = {'schlib_target': '', 'pcblib_target': '', 'project_path': '',
                               'keep_individual': False, 'import_existing_to_project': False}
        self._normal_library_formats = None
        self.worker = None
        self.batch_running = False
        self.batch_ids = []
        self.ids = []
        self.results = {}
        self.component_info = {}
        self.info_rows = {}
        self.info_revision = 0
        self.info_worker = None
        self.info_pending = None
        self.current_preview = ''
        self.preview_state = 'empty'
        self.preview_mode = '3d'
        self.current_3d = ''
        self.web_revision = 0
        self.viewer_started = False
        self.viewer_page_loads = 0
        self.viewer_ready = False
        self.web_model = None
        self.web_error = ''
        self.model_worker = None
        self.model_pending = None
        self.web_state = ('empty', ui_text('选择器件后获取官方模型并在本地显示'))
        self.library_cache = OrderedDict()
        self.library_worker = None
        self.library_pending = None
        self.symbol_unit_part = ''
        self.web = None
        self.web_profile = QWebEngineProfile(self)
        self.web_profile.setHttpCacheMaximumSize(8 * 1024 * 1024)
        self.close_when_finished = False
        self.update_dialog = None
        self.startup_update_check = None
        self.startup_update_attempted = False
        self.startup_update_outcome = None
        self.startup_update_timer = QTimer(self)
        self.startup_update_timer.setSingleShot(True)
        self.startup_update_timer.timeout.connect(self.check_startup_update)
        self.export_layout_timer = QTimer(self)
        self.export_layout_timer.setSingleShot(True)
        self.export_layout_timer.timeout.connect(self.adjust_export_layout)
        self.favorites_dialog = None
        self.retired_stores = []
        self.account_menu = None
        self._setup_ui()
        self._setup_preview()
        theme_manager().changed.connect(self.refresh_appearance)
        self.setMinimumHeight(max(self.minimumHeight(), self.minimumSizeHint().height()))
        self._restore_settings()
        if self.settings_enabled:
            QTimer.singleShot(0, self.ensure_store)

    def _setup_ui(self):
        canvas = QWidget()
        canvas.setObjectName('canvas')
        # Large interface fonts can exceed a small screen; keep every control
        # reachable without forcing the top-level window off screen.
        self.content_scroll = QScrollArea()
        self.content_scroll.setFrameShape(QFrame.NoFrame)
        self.content_scroll.setWidgetResizable(True)
        self.content_scroll.setWidget(canvas)
        shell = QWidget()
        shell_layout = QHBoxLayout(shell)
        shell_layout.setContentsMargins(0, 0, 0, 0)
        shell_layout.setSpacing(0)
        self.navigation = QFrame()
        self.navigation.setObjectName('navigation')
        nav = QVBoxLayout(self.navigation)
        nav.setContentsMargins(12, 24, 12, 16)
        nav.setSpacing(8)
        logo = label('')
        logo.setPixmap(QPixmap(str(ROOT / 'assets' / 'app.png')).scaled(44, 44, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        nav.addWidget(logo, 0, Qt.AlignHCenter)
        brand = label('LCSC3D', 'section')
        brand.setAlignment(Qt.AlignCenter)
        nav.addWidget(brand)
        nav.addSpacing(28)
        self.workspace_button = IconButton(ui_text('工作台'), 'workspace', role='navButton')
        self.workspace_button.setCheckable(True)
        self.workspace_button.setChecked(True)
        self.workspace_button.clicked.connect(lambda: self._page_host.present(self.content_scroll, navigation=True))
        self.market_button = IconButton(ui_text('立创商城'), 'store', role='navButton')
        self.market_button.setToolTip(ui_text('搜索商品、查看原图、登录及管理账号收藏'))
        self.market_button.clicked.connect(self.open_favorites)
        nav.addWidget(self.workspace_button)
        nav.addWidget(self.market_button)
        nav.addStretch()
        self.settings_button = IconButton(ui_text('设置'), 'settings', role='navButton')
        self.settings_button.clicked.connect(self.open_settings)
        self.help_button = IconButton(ui_text('使用说明'), 'help', role='navButton')
        self.help_button.clicked.connect(self.show_help)
        nav.addWidget(self.settings_button)
        nav.addWidget(self.help_button)
        version = label('v' + VERSION, 'muted')
        version.setAlignment(Qt.AlignCenter)
        nav.addSpacing(12)
        nav.addWidget(version)
        shell_layout.addWidget(self.navigation)
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(0)
        global_header = QHBoxLayout()
        global_header.setContentsMargins(24, 12, 24, 0)
        self.back_button = QPushButton(ui_text('返回'))
        self.back_button.setProperty('variant', 'text')
        global_header.addWidget(self.back_button)
        self.page_caption = label('', 'muted')
        global_header.addWidget(self.page_caption, 1)
        self.account_button = IconButton(ui_text('账号登录'), 'user')
        self.account_button.clicked.connect(self.open_account)
        global_header.addWidget(self.account_button)
        right_layout.addLayout(global_header)
        self._page_host = PageHost(self, self.content_scroll)
        self.back_button.clicked.connect(self._page_host.back)
        self._page_host.changed.connect(self.page_changed)
        right_layout.addWidget(self._page_host, 1)
        shell_layout.addWidget(right, 1)
        self.setCentralWidget(shell)
        self.notifications = NotificationCenter(shell)
        for button in (self.workspace_button, self.market_button, self.settings_button, self.help_button):
            button.setCheckable(True)
        self.page_changed(self.content_scroll)
        layout = QVBoxLayout(canvas)
        layout.setContentsMargins(24, 20, 24, 10)
        layout.setSpacing(12)
        heading = QHBoxLayout()
        heading.addWidget(title_block(ui_text('元件工作台'), ui_text('从器件编号到 3D 模型与 AD 元件库'), large=True), 1)
        layout.addLayout(heading)

        splitter = self.workspace_splitter = ColumnSplitter()
        # Wrapped preview captions must not force QScrollArea to allocate the
        # splitter's preferred height. Reserve only its actual child minimums.
        splitter.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Ignored)
        left_column = QWidget()
        left_layout = QVBoxLayout(left_column)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(14)

        input_card, input_layout = card()
        input_layout.setSpacing(8)
        input_head = QHBoxLayout()
        input_head.addWidget(label(ui_text('添加器件'), 'section'))
        input_head.addStretch()
        sample = QPushButton(ui_text('填入示例'))
        sample.setProperty('variant', 'text')
        sample.clicked.connect(lambda: self.input.setPlainText('C2040\nC20197\nC163691'))
        self.sample_button = sample
        input_head.addWidget(sample)
        clear = QPushButton(ui_text('清空'))
        clear.setProperty('variant', 'text')
        clear.clicked.connect(lambda: self.input.clear())
        self.clear_button = clear
        input_head.addWidget(clear)
        input_layout.addLayout(input_head)
        self.input = QPlainTextEdit()
        self.input.setPlaceholderText(ui_text('例如：C2040, C20197\n支持换行、空格、中英文逗号分隔；重复编号自动合并'))
        self.input.setFixedHeight(76)
        self.input.textChanged.connect(self.input_changed)
        input_layout.addWidget(self.input)
        count_row = QHBoxLayout()
        self.input_info = label(ui_text('输入立创商城 C 开头的器件编号'), 'muted')
        self.input_info.setWordWrap(True)
        count_row.addWidget(self.input_info, 1)
        self.queue_button = IconButton(ui_text('载入列表'), 'add', role='primary')
        self.queue_button.clicked.connect(self.load_queue)
        count_row.addWidget(self.queue_button)
        input_layout.addLayout(count_row)
        left_layout.addWidget(input_card)

        output_card, output_layout = card()
        output_layout.setSpacing(8)
        path_row = QHBoxLayout()
        path_row.addWidget(label(ui_text('导出设置'), 'section'))
        self.path_input = QLineEdit()
        self.path_input.setPlaceholderText(ui_text('选择资源保存目录'))
        path_row.addWidget(self.path_input, 1)
        self.browse_button = IconButton(ui_text('选择文件夹…'), 'folder')
        self.browse_button.clicked.connect(self.choose_folder)
        path_row.addWidget(self.browse_button)
        open_button = QPushButton(ui_text('打开目录'))
        open_button.clicked.connect(self.open_output)
        path_row.addWidget(open_button)
        output_layout.addLayout(path_row)
        option_row = QHBoxLayout()
        option_row.addWidget(label(ui_text('3D 模型'), 'muted'))
        self.step_box = QCheckBox('STEP')
        self.step_box.setChecked(True)
        self.step_box.setToolTip(ui_text('原始 STEP 文件，适用于 SolidWorks、FreeCAD 等 CAD 软件'))
        self.obj_box = QCheckBox('OBJ')
        self.obj_box.setToolTip(ui_text('下载官方 OBJ 模型文本'))
        for widget in (self.step_box, self.obj_box):
            option_row.addWidget(widget)
        option_row.addSpacing(12)
        option_row.addWidget(label(ui_text('AD 元件库'), 'muted'))
        self.schlib_box = QCheckBox('SchLib')
        self.schlib_box.setToolTip(ui_text('导出原生 AD 符号库，保留引脚编号及封装引用'))
        self.pcblib_box = QCheckBox('PcbLib')
        self.pcblib_box.setToolTip(ui_text('导出原生 AD 封装库，保留焊盘、钻孔与槽孔；不内嵌 3D 模型'))
        for widget in (self.schlib_box, self.pcblib_box):
            option_row.addWidget(widget)
        option_row.addSpacing(12)
        option_row.addWidget(label('Lib', 'muted'))
        self.lib_merge_box = QCheckBox(ui_text('合并'))
        self.lib_append_box = QCheckBox(ui_text('追加'))
        option_row.addWidget(self.lib_merge_box)
        option_row.addWidget(self.lib_append_box)
        option_row.addStretch()
        self.stop_button = QPushButton(ui_text('停止'))
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self.stop_batch)
        self.start_button = IconButton(ui_text('开始下载'), 'download', role='primary')
        self.start_button.clicked.connect(self.start_batch)
        option_row.addWidget(self.stop_button)
        option_row.addWidget(self.start_button)
        output_layout.addLayout(option_row)
        self.merge_schlib_box = QCheckBox(ui_text('合并 .SchLib'))
        self.merge_pcblib_box = QCheckBox(ui_text('合并 .PcbLib'))
        self.schlib_name_input = QLineEdit('LCSC3D')
        self.pcblib_name_input = QLineEdit('LCSC3D')
        self.keep_schlib_box = QCheckBox(ui_text('独立导出器件'))
        self.keep_pcblib_box = QCheckBox(ui_text('独立导出器件'))
        self.merge_options = QWidget()
        merge_layout = QVBoxLayout(self.merge_options)
        merge_layout.setContentsMargins(0, 0, 0, 0)
        merge_layout.setSpacing(8)
        for box, name, format_box, suffix, keep in (
                (self.merge_schlib_box, self.schlib_name_input, self.schlib_box, '.SchLib', self.keep_schlib_box),
                (self.merge_pcblib_box, self.pcblib_name_input, self.pcblib_box, '.PcbLib', self.keep_pcblib_box)):
            row = QHBoxLayout()
            row.addWidget(box)
            name.setPlaceholderText(ui_text('合并库名称'))
            name.setAccessibleName(ui_text('合并 ') + suffix + ui_text(' 名称'))
            name.setToolTip(ui_text('保存在所选目录根部；可填写名称或带扩展名的文件名'))
            row.addWidget(name, 1)
            row.addWidget(label(suffix, 'muted'))
            row.addWidget(keep)
            merge_layout.addLayout(row)
            box.toggled.connect(self.update_merge_controls)
            format_box.toggled.connect(self.update_merge_controls)
        model_hint = label(ui_text('未勾选独立导出器件时，3D 文件集中到“SchLib 名称_3D”文件夹；仅合并 PcbLib 时跟随其名称。'), 'muted')
        model_hint.setWordWrap(True)
        merge_layout.addWidget(model_hint)
        output_layout.addWidget(self.merge_options)
        self.append_options = QWidget()
        targets_row = QHBoxLayout(self.append_options)
        targets_row.setContentsMargins(0, 0, 0, 0)
        self.targets_summary = label('', 'muted')
        self.targets_summary.setWordWrap(True)
        targets_row.addWidget(self.targets_summary, 1)
        self.append_configure_button = QPushButton(ui_text('配置追加…'))
        self.append_configure_button.clicked.connect(self.configure_export_targets)
        targets_row.addWidget(self.append_configure_button)
        output_layout.addWidget(self.append_options)
        self.lib_merge_box.toggled.connect(lambda checked: self.change_library_mode('merge', checked))
        self.lib_append_box.toggled.connect(lambda checked: self.change_library_mode('append', checked))
        self.lib_append_box.clicked.connect(self.append_clicked)
        self.update_merge_controls()
        self.export_card = output_card

        list_card, list_layout = card()
        list_layout.setSpacing(8)
        list_head = QHBoxLayout()
        list_head.addWidget(label(ui_text('下载列表'), 'section'))
        list_head.addStretch()
        self.summary = label(ui_text('0 个器件'), 'badge')
        list_head.addWidget(self.summary)
        list_layout.addLayout(list_head)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels([ui_text('下载'), ui_text('器件编号'), ui_text('型号 / 模型'), ui_text('结果')])
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setShowGrid(False)
        self.table.setWordWrap(False)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(40)
        self.table.setMinimumHeight(128)
        self.table.setAlternatingRowColors(True)
        self.table.horizontalHeader().setSectionResizeMode(DOWNLOAD_COLUMN, QHeaderView.Fixed)
        self.table.setColumnWidth(DOWNLOAD_COLUMN, 56)
        self.table.horizontalHeader().setSectionResizeMode(PART_COLUMN, QHeaderView.Fixed)
        self.table.setColumnWidth(PART_COLUMN, 104)
        self.table.horizontalHeader().setSectionResizeMode(MODEL_COLUMN, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(RESULT_COLUMN, QHeaderView.Fixed)
        self.table.setColumnWidth(RESULT_COLUMN, 100)
        self.table.currentCellChanged.connect(self.selection_changed)
        self.table.itemChanged.connect(self.download_selection_changed)
        list_layout.addWidget(self.table, 1)
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 1)
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setFixedHeight(8)
        list_layout.addWidget(self.progress_bar)
        self.run_status = label(ui_text('准备就绪'), 'muted')
        self.run_status.setWordWrap(True)
        list_layout.addWidget(self.run_status)
        self.detail = label(ui_text('选择器件自动预览，并查看结果详情'), 'muted')
        self.detail.setWordWrap(True)
        self.detail.setMinimumHeight(26)
        list_layout.addWidget(self.detail)
        list_actions = QHBoxLayout()
        self.select_all_button = QPushButton(ui_text('全选'))
        self.select_all_button.setEnabled(False)
        self.select_all_button.clicked.connect(self.select_all_downloads)
        list_actions.addWidget(self.select_all_button)
        self.invert_selection_button = QPushButton(ui_text('反选'))
        self.invert_selection_button.setEnabled(False)
        self.invert_selection_button.clicked.connect(self.invert_download_selection)
        list_actions.addWidget(self.invert_selection_button)
        self.remove_checked_button = QPushButton(ui_text('删除已勾选器件'))
        self.remove_checked_button.setProperty('variant', 'danger')
        self.remove_checked_button.setEnabled(False)
        self.remove_checked_button.setToolTip(ui_text('从下载列表及输入框移除勾选器件，保留已下载文件'))
        self.remove_checked_button.clicked.connect(self.remove_checked_downloads)
        list_actions.addWidget(self.remove_checked_button)
        self.selection_summary = label(ui_text('已勾选 0 / 0'), 'muted')

        list_actions.addStretch()
        self.part_folder_button = QPushButton(ui_text('打开器件目录'))
        self.part_folder_button.clicked.connect(self.open_part_folder)
        self.part_folder_button.setEnabled(False)
        list_layout.insertLayout(1, list_actions)
        list_footer = QHBoxLayout()
        list_footer.addWidget(self.selection_summary)
        list_footer.addStretch()
        list_footer.addWidget(self.part_folder_button)
        list_layout.addLayout(list_footer)
        left_layout.addWidget(list_card, 1)
        splitter.addWidget(left_column)

        preview_card, preview_layout = card()
        preview_head = QHBoxLayout()
        preview_head.addWidget(label(ui_text('器件预览'), 'section'))
        preview_head.addStretch()
        preview_layout.addLayout(preview_head)
        preview_modes = QHBoxLayout()
        self.preview_mode_group = QButtonGroup(self)
        self.preview_mode_buttons = {}
        for mode, text in (('3d', ui_text('3D 模型')), ('symbol', ui_text('符号')), ('footprint', ui_text('封装')), ('photo', ui_text('商品图片'))):
            button = QPushButton(text)
            button.setObjectName('previewMode')
            button.setCheckable(True)
            button.setChecked(mode == self.preview_mode)
            button.clicked.connect(lambda checked=False, choice=mode: self.set_preview_mode(choice))
            self.preview_mode_group.addButton(button)
            self.preview_mode_buttons[mode] = button
            preview_modes.addWidget(button)

        self.symbol_unit_box = QComboBox()
        self.symbol_unit_box.setToolTip(ui_text('选择符号单元'))
        self.symbol_unit_box.currentIndexChanged.connect(self.show_symbol_unit)
        self.symbol_unit_box.hide()
        preview_head.addWidget(self.symbol_unit_box)
        preview_layout.addLayout(preview_modes)
        self.preview_caption = label(ui_text('选择左侧列表中的器件'), 'muted')
        self.preview_caption.setWordWrap(True)
        preview_layout.addWidget(self.preview_caption)
        self.preview_stack = QStackedWidget()
        self.product_preview = ProductPreview(self)
        self.product_preview.state.connect(self.product_preview_state)
        self.product_preview.jobs.idle.connect(self.store_activity_finished)
        self.preview_stack.addWidget(self.product_preview)
        empty = QFrame()
        empty.setObjectName('previewEmpty')
        empty_layout = QVBoxLayout(empty)
        empty_layout.addStretch()
        cube = label('◇')
        cube.setAlignment(Qt.AlignCenter)
        cube.setObjectName('previewCube')
        empty_layout.addWidget(cube)
        hint = label(ui_text('载入列表后自动预览所选器件\n鼠标拖动旋转，滚轮缩放'))
        hint.setAlignment(Qt.AlignCenter)
        hint.setObjectName('previewHint')
        empty_layout.addWidget(hint)
        empty_layout.addStretch()
        self.preview_stack.addWidget(empty)
        self.preview_stack.setCurrentWidget(empty)
        self.preview_empty = empty
        self.preview_hint = hint
        self.symbol_view = VectorPreviewView(SYMBOL_BACKGROUND, profile=self.web_profile)
        self.footprint_view = VectorPreviewView(FOOTPRINT_BACKGROUND, profile=self.web_profile)
        for view in (self.symbol_view, self.footprint_view):
            view.state.connect(lambda status, message, canvas=view: self.on_vector_preview_state(canvas, status, message))
        self.preview_stack.addWidget(self.symbol_view)
        self.preview_stack.addWidget(self.footprint_view)
        self.preview_stack.setMinimumHeight(180)
        preview_layout.addWidget(self.preview_stack, 1)
        self.preview_legend = label(ui_text('<span style="color:#ff0000">■ 顶层焊盘</span>　'
                                    '<span style="color:#0000ff">■ 底层焊盘</span>　'
                                    '<span style="color:#cc9900">■ 丝印</span>　'
                                    '<span style="color:#c0c0c0">■ 多层</span>'), 'muted')
        self.preview_legend.setWordWrap(True)
        self.preview_legend.hide()
        preview_layout.addWidget(self.preview_legend)
        self.preview_status = label(ui_text('选择器件后获取官方模型并在本地显示'), 'muted')
        self.preview_status.setObjectName('previewStatus')
        self.preview_status.setWordWrap(True)
        preview_layout.addWidget(self.preview_status)
        preview_actions = QHBoxLayout()
        self.reload_button = IconButton(ui_text('重新加载'), 'refresh')
        self.reload_button.setEnabled(False)
        self.reload_button.clicked.connect(lambda: self.show_preview(self.current_preview, reload=True))
        preview_actions.addWidget(self.reload_button)
        self.fit_button = QPushButton(ui_text('适应窗口'))
        self.fit_button.clicked.connect(self.fit_preview)
        self.fit_button.hide()
        preview_actions.addWidget(self.fit_button)
        preview_actions.addStretch()
        self.store_button = QPushButton(ui_text('打开商城页面'))
        self.store_button.setEnabled(False)
        self.store_button.clicked.connect(self.open_store)
        preview_actions.addWidget(self.store_button)
        preview_layout.addLayout(preview_actions)
        splitter.addWidget(preview_card)
        splitter.setSizes([560, 590])
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 1)
        splitter.setChildrenCollapsible(False)
        layout.addWidget(splitter, 1)
        layout.addWidget(output_card)

        footer = QHBoxLayout()
        footer.addWidget(label(ui_text('官方资源下载 · AGPL-3.0'), 'muted'))
        footer.addStretch()
        source = label(ui_text('数据来源：<a style="color:#638397" href="https://lceda.cn/">JLCEDA</a> / <a style="color:#638397" href="https://easyeda.com/">EasyEDA 官方库</a>'), 'muted')
        source.setOpenExternalLinks(True)
        footer.addWidget(source)
        layout.addLayout(footer)

    def page_changed(self, page):
        key = 'workspace'
        current = page
        while current is not None and current is not self:
            if isinstance(current, FavoritesDialog):
                key = 'store'
                break
            if isinstance(current, (SettingsDialog, UpdateDialog)):
                key = 'settings'
                break
            if isinstance(current, HelpDialog):
                key = 'help'
                break
            current = getattr(current, '_page_owner', None)
        for name, button in (('workspace', self.workspace_button), ('store', self.market_button),
                             ('settings', self.settings_button), ('help', self.help_button)):
            button.setChecked(name == key)
        self.back_button.setVisible(page is not self.content_scroll)
        self.page_caption.setText(page.windowTitle() if page is not self.content_scroll else 'LCSC3D')
        if self.account_menu is not None:
            self.account_menu.close()

    def _restore_settings(self):
        default = str(data_directory() / 'downloads')
        self.path_input.setText(default)
        settings = {}
        if self.settings_enabled:
            settings = read_settings(SETTINGS_PATH, LEGACY_SETTINGS_PATH)
        destination = settings.get('destination')
        self.path_input.setText(destination if isinstance(destination, str) and destination else default)
        self.step_box.setChecked(bool(settings.get('step', True)))
        self.obj_box.setChecked(bool(settings.get('obj')))
        self.schlib_box.setChecked(bool(settings.get('schlib')))
        self.pcblib_box.setChecked(bool(settings.get('pcblib')))
        self.merge_schlib_box.setChecked(settings.get('merge_schlib') is True)
        self.merge_pcblib_box.setChecked(settings.get('merge_pcblib') is True)
        self.keep_schlib_box.setChecked(settings.get('keep_schlib') is True)
        self.keep_pcblib_box.setChecked(settings.get('keep_pcblib') is True)
        self.export_targets = {key: settings[key] if isinstance(settings.get(key), str) else ''
                               for key in ('schlib_target', 'pcblib_target', 'project_path')}
        legacy_append = any(self.export_targets.values())
        self.export_targets['keep_individual'] = settings.get('keep_individual',
            settings.get('keep_schlib') is True or settings.get('keep_pcblib') is True) is True
        self.export_targets['import_existing_to_project'] = settings.get('import_existing_to_project',
            legacy_append and bool(self.export_targets['project_path'])) is True
        for key, widget in (('schlib_name', self.schlib_name_input), ('pcblib_name', self.pcblib_name_input)):
            value = settings.get(key)
            widget.setText(value if isinstance(value, str) else 'LCSC3D')
        mode = settings.get('library_mode')
        if mode not in ('individual', 'merge', 'append'):
            mode = 'append' if legacy_append else 'merge' if (
                self.merge_schlib_box.isChecked() or self.merge_pcblib_box.isChecked()) else 'individual'
        self.lib_merge_box.setChecked(mode == 'merge')
        self.lib_append_box.setChecked(mode == 'append')
        self.update_merge_controls()
        # Migrate removed library/WRL-only selections to the default model format.
        if not any(box.isChecked() for box in (self.step_box, self.obj_box, self.schlib_box, self.pcblib_box)):
            self.step_box.setChecked(True)
        self.preferences = Preferences.from_mapping(settings)
        set_preferences(self.preferences)
        set_log_level(self.preferences.log_level)
        set_language(self.preferences.language)
        theme_manager().apply(self.preferences)

    def save_settings(self, preferences=None):
        if not self.settings_enabled:
            return True
        preferences = preferences or self.preferences
        try:
            write_settings(SETTINGS_PATH, {'destination': self.path_input.text(), 'step': self.step_box.isChecked(),
                'obj': self.obj_box.isChecked(), 'schlib': self.schlib_box.isChecked(),
                'pcblib': self.pcblib_box.isChecked(),
                'merge_schlib': self.merge_schlib_box.isChecked(), 'merge_pcblib': self.merge_pcblib_box.isChecked(),
                'schlib_name': self.schlib_name_input.text(), 'pcblib_name': self.pcblib_name_input.text(),
                'keep_schlib': self.keep_schlib_box.isChecked(), 'keep_pcblib': self.keep_pcblib_box.isChecked(),
                'library_mode': self.library_mode(),
                **self.export_targets,
                **preferences.to_mapping()})
            return True
        except OSError as exc:
            record_error(exc, 'settings.save_failed', level='WARNING')
            return False

    @traced('settings.apply')
    def apply_preferences(self, preferences):
        if not self.save_settings(preferences):
            return False
        region_changed = self.preferences.language != preferences.language
        self.preferences = preferences
        set_preferences(preferences)
        set_log_level(preferences.log_level)
        set_language(preferences.language)
        theme_manager().apply(preferences)
        if region_changed:
            if self.favorites_dialog is not None:
                dialog, self.favorites_dialog = self.favorites_dialog, None
                dialog.shutdown()
                dialog.close()
                self.retired_stores.append(dialog)
            self.product_preview.clear()
            if self.preview_mode == 'photo' and self.current_preview:
                self.product_preview.select(self.current_preview)
            self.refresh_store_account()
            self.store_activity_finished()
        log_event('INFO', 'settings.saved', **preferences.to_mapping())
        return True

    def refresh_appearance(self):
        tokens = theme_manager().tokens
        scale = tokens['font_size'] / 13
        self.input.setFixedHeight(max(76, self.input.fontMetrics().lineSpacing() * 2 + 22))
        self.table.verticalHeader().setDefaultSectionSize(max(round(40 * scale), self.table.fontMetrics().height() + 14))
        for row, part in enumerate(self.ids):
            item = self.table.item(row, RESULT_COLUMN)
            if item is not None:
                status = self.results[part].status if part in self.results else ''
                role = {ui_text('成功'): 'success', ui_text('部分完成'): 'warning', ui_text('失败'): 'error',
                        ui_text('无模型'): 'warning', ui_text('已取消'): 'muted'}.get(status, 'text')
                item.setForeground(QColor(tokens[role]))
        self.table.setColumnWidth(DOWNLOAD_COLUMN, round((85 if self.preferences.language == 'en_US' else 56) * scale))
        self.table.setColumnWidth(PART_COLUMN, round(104 * scale))
        self.table.setColumnWidth(RESULT_COLUMN, round((125 if self.preferences.language == 'en_US' else 100) * scale))
        if self.web is not None:
            self.web.page().setBackgroundColor(QColor(tokens['field']))
            self.update_viewer_appearance()
        self.export_layout_timer.start(0)
        self.page_changed(self._page_host.current_page())
        self.update()

    def update_viewer_appearance(self):
        if self.web is not None:
            appearance = dict(theme_manager().tokens, language=self.preferences.language)
            self.web.page().runJavaScript('window.setAppearance && window.setAppearance(' + json.dumps(appearance) + ')')

    @traced('settings.open')
    def open_settings(self):
        if self.settings_dialog is not None and not self.settings_dialog._page_finished:
            self.settings_dialog.raise_()
            self.settings_dialog.activateWindow()
            return
        if self.settings_dialog is not None:
            if (self.update_dialog is not None and self.update_dialog._page_owner is self.settings_dialog
                    and self.update_dialog._page_finished and self.update_dialog.worker is None):
                self.update_dialog.deleteLater()
                self.update_dialog = None
            self.settings_dialog.deleteLater()
        self.settings_dialog = SettingsDialog(self.preferences, self.apply_preferences, self)
        self.settings_dialog.show()

    def input_changed(self):
        self.update_start_button()
        ids, invalid, duplicates = parse_part_numbers(self.input.toPlainText())
        text = ui_message('已识别 {0} 个器件', len(ids))
        if duplicates:
            text += ui_message(' · 合并 {0} 个重复编号', duplicates)
        if invalid:
            text += ui_text(' · 请修正：') + ', '.join(invalid[:4]) + ('…' if len(invalid) > 4 else '')
        self.input_info.setText(text)

    @traced('queue.load', level='INFO')
    def load_queue(self):
        if self.batch_running:
            return False
        ids, invalid, duplicates = parse_part_numbers(self.input.toPlainText())
        if invalid:
            log_event('WARNING', 'queue.input_rejected', reason='invalid_part_numbers', count=len(invalid))
            self.run_status.setText(ui_text('请先修正输入中无法识别的内容：') + ', '.join(invalid[:6]))
            return False
        if not ids:
            log_event('DEBUG', 'queue.input_rejected', reason='empty')
            self.run_status.setText(ui_text('请先输入 C 开头的立创器件编号'))
            return False
        checks = {part: self.table.item(row, DOWNLOAD_COLUMN).checkState()
                  for row, part in enumerate(self.ids)}
        self.info_revision += 1
        self.component_info = {part: self.component_info[part] for part in ids if part in self.component_info}
        self.info_rows = {part: row for row, part in enumerate(ids)}
        self.table.blockSignals(True)
        try:
            self.ids, self.results = ids, {}
            self.batch_ids = []
            self.table.clearSelection()
            self.table.setCurrentCell(-1, -1)
            self.table.setRowCount(len(ids))
            for row, part in enumerate(ids):
                check = QTableWidgetItem()
                check.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable | Qt.ItemIsUserCheckable)
                check.setCheckState(checks.get(part, Qt.Checked))
                check.setToolTip(ui_text('勾选后下载此器件；单击器件行可预览'))
                self.table.setItem(row, DOWNLOAD_COLUMN, check)
                self.table.setItem(row, PART_COLUMN, QTableWidgetItem(part))
                info = self.component_info.get(part, {})
                model_item = QTableWidgetItem(info.get('title') or info.get('model') or ui_text('查询中…'))
                model_item.setToolTip(self.component_tooltip(info))
                self.table.setItem(row, MODEL_COLUMN, model_item)
                self.table.setItem(row, RESULT_COLUMN, QTableWidgetItem(ui_text('等待下载')))
        finally:
            self.table.blockSignals(False)
        self.summary.setText(ui_message('{0} 个器件', len(ids)))
        log_event('INFO', 'queue.loaded', count=len(ids), duplicate_count=duplicates)
        self.download_selection_changed()
        self.progress_bar.setRange(0, len(ids))
        self.progress_bar.setValue(0)
        self.run_status.setText(ui_text('列表已载入，请勾选需要下载的器件；单击器件行自动预览'))
        self.table.selectRow(0)
        self.request_component_info()
        return True

    def bind_store(self, dialog):
        self.favorites_dialog = dialog
        dialog.import_requested.connect(self.import_store_selection)
        dialog.preview_requested.connect(self.preview_store_product)
        dialog.activity_finished.connect(self.store_activity_finished)
        dialog.account_changed.connect(self.refresh_store_account)
        self.refresh_store_account()
        return dialog

    def ensure_store(self):
        if self.favorites_dialog is None:
            self.bind_store(FavoritesDialog(self, vault=None if self.settings_enabled else False))
        return self.favorites_dialog

    def refresh_store_account(self):
        if self.preferences.language == 'en_US':
            self.account_button.setEnabled(True)
            self.account_button.setText('LCSC account')
            self.account_button.setToolTip('Manage your international account on LCSC.com')
            if self.account_menu:
                self.account_menu.close()
            return
        dialog = self.favorites_dialog
        account = dialog.client.account if dialog else None
        restoring = bool(dialog and dialog.restoring)
        self.account_button.setEnabled(not restoring)
        name = account['name'] if account else ''
        self.account_button.setText(ui_text('恢复登录…') if restoring else ui_text('账号：') + name[:12] if account else ui_text('账号登录'))
        self.account_button.setToolTip(ui_text('已登录：') + name + ui_text('，点击管理登录状态') if account else ui_text('登录立创商城账号，支持记住登录'))
        if self.account_menu:
            self.account_menu.close()

    @traced('login.open')
    def open_account(self):
        dialog = self.ensure_store()
        if dialog.client.account:
            if self.account_menu is not None:
                if self.account_menu.isVisible():
                    self.account_menu.close()
                    return
                self.account_menu.deleteLater()
            self.account_menu = AccountPanel(self.account_button)
            self.account_menu.addAction(ui_text('已登录：') + dialog.client.account['name']).setEnabled(False)
            self.account_menu.addAction(ui_text('退出登录'), dialog.clear_session)
            self.account_menu.popup(self.account_button.mapToGlobal(self.account_button.rect().bottomLeft()))
        else:
            dialog.open_login()

    def preview_store_product(self, part):
        self.show_preview(part)
        self._page_host.present(self.content_scroll, navigation=True)

    @traced('store.open')
    def open_favorites(self, mode=None):
        self.ensure_store()
        if isinstance(mode, str):
            self.favorites_dialog.set_mode(mode)
        self.favorites_dialog.set_import_enabled(not self.batch_running)
        self.favorites_dialog.show()
        self.favorites_dialog.raise_()
        self.favorites_dialog.activateWindow()

    def store_activity_finished(self):
        for dialog in self.retired_stores[:]:
            if not dialog.has_jobs():
                self.retired_stores.remove(dialog)
                dialog.deleteLater()
        if self.close_when_finished:
            self.close()

    @traced('queue.import', lambda self, items: {'count': len(items)}, level='INFO')
    def import_favorites(self, items):
        """Append catalog/favorite products and preserve existing queue state."""
        def status(message, success=False):
            self.import_result = (success, message)
            self.run_status.setText(message)
            if self.favorites_dialog:
                self.favorites_dialog.status.setText(message)

        if self.batch_running or self.close_when_finished:
            status(ui_text('请等待当前下载任务完成后再导入元件。'))
            return 0
        items = normalize_items(items)
        if not items:
            status(ui_text('没有可导入的有效 C 编号。'))
            return 0
        pending, invalid, _ = parse_part_numbers(self.input.toPlainText())
        if invalid:
            status(ui_text('请先修正主窗口输入框中的无效内容，再导入元件：') + ', '.join(invalid[:4]))
            return 0
        existing = set(self.ids)
        merged = list(dict.fromkeys(self.ids + pending + [item['part'] for item in items]))
        new_parts = [part for part in merged if part not in existing]
        titles = {item['part']: item['title'] for item in items if item['title']}
        self.table.blockSignals(True)
        try:
            for part in new_parts:
                row = self.table.rowCount()
                self.table.insertRow(row)
                check = QTableWidgetItem()
                check.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable | Qt.ItemIsUserCheckable)
                check.setCheckState(Qt.Checked)
                check.setToolTip(ui_text('勾选后下载此器件；单击器件行可预览'))
                self.table.setItem(row, DOWNLOAD_COLUMN, check)
                self.table.setItem(row, PART_COLUMN, QTableWidgetItem(part))
                if part in titles:
                    self.component_info[part] = {'title': titles[part]}
                self.table.setItem(row, MODEL_COLUMN, QTableWidgetItem(titles.get(part) or ui_text('查询中…')))
                self.table.setItem(row, RESULT_COLUMN, QTableWidgetItem(ui_text('等待下载')))
        finally:
            self.table.blockSignals(False)
        self.ids = merged
        self.info_rows = {part: row for row, part in enumerate(self.ids)}
        self.input.setPlainText('\n'.join(self.ids))
        self.summary.setText(ui_message('{0} 个器件', len(self.ids)))
        self.download_selection_changed()
        if new_parts:
            self.info_revision += 1
            self.request_component_info()
            if self.table.currentRow() < 0:
                self.table.selectRow(0)
        added = sum(item['part'] not in existing for item in items)
        status(ui_message('元件导入完成：新增 {0} 个，跳过 {1} 个已有元件；下载列表共 {2} 个。', added, len(items) - added, len(self.ids)), success=True)
        return added

    def import_store_selection(self, items):
        try:
            self.import_favorites(items)
            success, message = self.import_result
        except Exception as exc:
            record_error(exc, 'queue.import_failed')
            success, message = False, ui_text('导入失败，请重试。错误详情已记录到日志。')
        if self.favorites_dialog:
            self.favorites_dialog.show_import_result(success, message)

    @staticmethod
    def component_tooltip(info):
        return '\n'.join(ui_message('{0}：{1}', label, info[key]) for key, label in (('title', ui_text('型号')), ('model', ui_text('模型'))) if info.get(key))

    def request_component_info(self):
        if self.close_when_finished:
            return
        self.info_pending = (self.ids[:], self.info_revision)
        if self.info_worker is not None:
            self.info_worker.cancelled.set()
            return
        ids, revision = self.info_pending
        self.info_pending = None
        worker = ComponentInfoWorker(ids, revision, self)
        self.info_worker = worker
        worker.loaded.connect(self.update_component_info)
        worker.finished.connect(self.component_info_finished)
        worker.start()

    def update_component_info(self, revision, part, info, error):
        if revision != self.info_revision or self.close_when_finished or part not in self.info_rows:
            return
        item = self.table.item(self.info_rows[part], MODEL_COLUMN)
        result = self.results.get(part)
        if error:
            if part not in self.component_info and not (result and (result.title or result.model)):
                item.setText(ui_text('查询失败'))
                item.setToolTip(error + ui_text('\n点击「载入列表」重试；仍可勾选下载'))
            return
        display_info = {'title': result.title or info.get('title', ''),
                        'model': result.model or info.get('model', '')} if result else info
        self.component_info[part] = display_info
        item.setText(display_info.get('title') or display_info.get('model') or ui_text('未提供型号'))
        item.setToolTip(self.component_tooltip(display_info))

    def component_info_finished(self):
        worker, self.info_worker = self.info_worker, None
        if worker:
            worker.deleteLater()
        if self.close_when_finished:
            self.close()
        elif self.info_pending is not None:
            self.request_component_info()

    def checked_rows(self):
        return [row for row in range(len(self.ids))
                if self.table.item(row, DOWNLOAD_COLUMN).checkState() == Qt.Checked]

    def download_selection_changed(self, item=None):
        if item is not None and item.column() != DOWNLOAD_COLUMN:
            return
        self.selection_summary.setText(ui_message('已勾选 {0} / {1}', len(self.checked_rows()), len(self.ids)))
        enabled = bool(self.ids) and not self.batch_running
        self.select_all_button.setEnabled(enabled)
        self.invert_selection_button.setEnabled(enabled)
        self.remove_checked_button.setEnabled(enabled and bool(self.checked_rows()) and not self.close_when_finished)
        self.update_start_button()

    def update_start_button(self):
        empty = not self.ids and not self.input.toPlainText().strip()
        self.start_button.setText(ui_text('导入已有库') if empty and self.lib_append_box.isChecked() else ui_text('开始下载'))

    @traced('queue.delete_checked', level='INFO')
    def remove_checked_downloads(self):
        if self.batch_running or self.close_when_finished or self.worker and self.worker.isRunning():
            return 0
        rows = self.checked_rows()
        if not rows:
            return 0
        removed = {self.ids[row] for row in rows}
        old_row, selected = self.table.currentRow(), self.selected_part()
        pending, invalid, _ = parse_part_numbers(self.input.toPlainText())
        self.info_revision += 1
        self.info_pending = None
        if self.info_worker:
            self.info_worker.cancelled.set()
        self.table.blockSignals(True)
        try:
            for row in reversed(rows):
                self.table.removeRow(row)
            self.ids = [part for part in self.ids if part not in removed]
            self.results = {part: result for part, result in self.results.items() if part not in removed}
            self.component_info = {part: info for part, info in self.component_info.items() if part not in removed}
            self.info_rows = {part: row for row, part in enumerate(self.ids)}
            self.batch_ids = []
            self.table.clearSelection()
            self.table.setCurrentCell(-1, -1)
            if self.ids:
                self.table.selectRow(self.ids.index(selected) if selected in self.ids else min(max(old_row, 0), len(self.ids) - 1))
        finally:
            self.table.blockSignals(False)
        extra = [part for part in pending if part not in removed and part not in self.ids]
        self.input.setPlainText('\n'.join(self.ids + extra + invalid))
        if self.ids:
            self.update_download_detail(self.selected_part())
            if self.current_preview in removed:
                self.show_preview(self.selected_part())
        elif self.current_preview in removed:
            self.clear_preview()
        if not self.ids:
            self.detail.setText(ui_text('选择器件自动预览，并查看结果详情'))
            self.detail.setToolTip('')
            self.part_folder_button.setEnabled(False)
        self.summary.setText(ui_message('{0} 个器件', len(self.ids)))
        self.progress_bar.setRange(0, max(1, len(self.ids)))
        self.progress_bar.setValue(sum(part in self.results for part in self.ids))
        self.download_selection_changed()
        self.run_status.setText(ui_message('已从下载列表删除 {0} 个器件，剩余 {1} 个。已下载文件保留。', len(removed), len(self.ids)))
        log_event('INFO', 'queue.deleted', count=len(removed), remaining=len(self.ids))
        if self.ids:
            self.request_component_info()
        return len(removed)

    def clear_preview(self):
        self.product_preview.clear()
        self.current_preview = self.current_3d = ''
        self.web_revision += 1
        self.model_pending = self.library_pending = None
        for worker in (self.model_worker, self.library_worker):
            if worker:
                worker.cancelled.set()
        self.web_model, self.web_error = None, ''
        self.web_state = ('empty', ui_text('选择器件后加载预览'))
        self.preview_stack.setCurrentWidget(self.preview_empty)
        self.preview_caption.setText(ui_text('选择左侧列表中的器件'))
        self.preview_hint.setText(ui_text('载入列表后自动预览所选器件\n鼠标拖动旋转，滚轮缩放'))
        self.symbol_unit_box.hide()
        self.symbol_unit_box.clear()
        self.symbol_unit_part = ''
        self.reload_button.setEnabled(False)
        self.fit_button.setEnabled(False)
        self.store_button.setEnabled(False)
        self.on_preview_state(*self.web_state)

    def select_all_downloads(self):
        self.change_download_selection()

    def invert_download_selection(self):
        self.change_download_selection(invert=True)

    def change_download_selection(self, invert=False):
        log_event('DEBUG', 'queue.selection_changed', action='invert' if invert else 'select_all')
        if self.batch_running:
            return
        self.table.blockSignals(True)
        try:
            for row in range(len(self.ids)):
                item = self.table.item(row, DOWNLOAD_COLUMN)
                item.setCheckState(Qt.Unchecked if invert and item.checkState() == Qt.Checked else Qt.Checked)
        finally:
            self.table.blockSignals(False)
        self.download_selection_changed()

    @traced('settings.choose_export_folder')
    def choose_folder(self):
        def selected(folder):
            self.path_input.setText(folder)
            self.save_settings()
            log_event('INFO', 'settings.export_folder_changed')
        self.folder_page = choose_path(self, ui_text('选择模型保存目录'), self.path_input.text(), selected, directory=True)

    def open_output(self):
        try:
            path = self.export_options().output_root()
        except DownloadError as exc:
            self.run_status.setText(str(exc))
            return
        if path.is_dir():
            opened = QDesktopServices.openUrl(QUrl.fromLocalFile(str(path.resolve())))
            log_event('INFO' if opened else 'WARNING', 'navigation.output_folder', opened=opened)
        else:
            log_event('WARNING', 'navigation.output_folder', opened=False, reason='not_created')
            self.run_status.setText(ui_text('目录尚未创建，开始下载时会自动创建'))

    def set_running(self, running):
        self.batch_running = running
        if self.favorites_dialog:
            self.favorites_dialog.set_import_enabled(not running)
        for widget in (self.input, self.sample_button, self.clear_button, self.path_input, self.browse_button, self.queue_button, self.start_button, self.step_box, self.obj_box, self.schlib_box, self.pcblib_box):
            widget.setEnabled(not running)
        self.stop_button.setEnabled(running)
        self.update_merge_controls()
        self.table.blockSignals(True)
        try:
            for row in range(len(self.ids)):
                item = self.table.item(row, DOWNLOAD_COLUMN)
                item.setFlags(item.flags() & ~Qt.ItemIsEnabled if running else item.flags() | Qt.ItemIsEnabled)
        finally:
            self.table.blockSignals(False)
        self.download_selection_changed()

    def library_mode(self):
        return 'append' if self.lib_append_box.isChecked() else 'merge' if self.lib_merge_box.isChecked() else 'individual'

    def change_library_mode(self, mode, checked):
        other = self.lib_append_box if mode == 'merge' else self.lib_merge_box
        if checked:
            other.setChecked(False)
        if mode == 'append':
            if checked and self._normal_library_formats is None:
                self._normal_library_formats = (self.schlib_box.isChecked(), self.pcblib_box.isChecked())
            elif not checked and self._normal_library_formats is not None:
                values, self._normal_library_formats = self._normal_library_formats, None
                for box, value in zip((self.schlib_box, self.pcblib_box), values):
                    box.blockSignals(True)
                    box.setChecked(value)
                    box.blockSignals(False)
        if mode == 'merge' and checked and not any(box.isChecked() for box in (self.merge_schlib_box, self.merge_pcblib_box)):
            self.merge_schlib_box.setChecked(True)
            self.merge_pcblib_box.setChecked(True)
        self.update_merge_controls()
        log_event('INFO', 'export.mode_changed', mode=self.library_mode())

    def append_clicked(self, checked):
        if checked:
            self.configure_export_targets()

    def update_merge_controls(self):
        append = self.lib_append_box.isChecked()
        merging = self.lib_merge_box.isChecked()
        self.merge_options.setVisible(merging)
        self.append_options.setVisible(append)
        for widget in (self.lib_merge_box, self.lib_append_box, self.append_configure_button):
            widget.setEnabled(not self.batch_running)
        for widget in (self.path_input, self.browse_button):
            widget.setEnabled(not self.batch_running and not append)
        self.path_input.setToolTip(ui_text('追加模式按所选已有库或 PCB 工程的位置导出') if append else '')
        targeted = append and any(self.export_targets.get(key) for key in ('schlib_target', 'pcblib_target'))
        for key, format_box in (('schlib_target', self.schlib_box), ('pcblib_target', self.pcblib_box)):
            if targeted:
                format_box.blockSignals(True)
                format_box.setChecked(bool(self.export_targets.get(key)))
                format_box.blockSignals(False)
            format_box.setEnabled(not self.batch_running and not targeted)
        for box, name, format_box, keep in (
                (self.merge_schlib_box, self.schlib_name_input, self.schlib_box, self.keep_schlib_box),
                (self.merge_pcblib_box, self.pcblib_name_input, self.pcblib_box, self.keep_pcblib_box)):
            enabled = not self.batch_running and merging and format_box.isChecked()
            box.setEnabled(enabled)
            keep.setEnabled(enabled and box.isChecked())
            name.setEnabled(enabled and box.isChecked())
        targets = [self.export_targets[key] for key in ('schlib_target', 'pcblib_target') if self.export_targets.get(key)]
        summary = [ui_message('追加到 {0} 份已有库', len(targets))] if targets else []
        if self.export_targets.get('project_path') and (not targets or self.export_targets.get('import_existing_to_project')):
            summary.append(ui_text('下载后加入 PCB 工程'))
        if self.export_targets.get('keep_individual'):
            summary.append(ui_text('独立导出器件'))
        self.targets_summary.setText(' · '.join(summary) or ui_text('请配置已有库或 PCB 工程'))
        self.targets_summary.setToolTip('\n'.join(targets + [self.export_targets.get('project_path', '')]))
        self.update_start_button()
        self.export_layout_timer.start(0)

    def adjust_export_layout(self):
        # Reflow the canvas inside its scroll area when merge controls appear.
        self.workspace_splitter.setMinimumHeight(0)
        self.workspace_splitter.setMinimumHeight(self.workspace_splitter.minimumSizeHint().height())
        self.content_scroll.widget().layout().activate()
        self.setMinimumHeight(740)

    def configure_export_targets(self):
        current = getattr(self, 'export_dialog', None)
        if current is not None and not current._page_finished:
            current.show()
            return
        if current is not None:
            current.deleteLater()
        dialog = self.export_dialog = ExportTargetsDialog(self.export_targets, self)
        def completed(result):
            if result == dialog.DialogCode.Accepted:
                self.export_targets = dialog.values
                if not any(self.export_targets[key] for key in ('schlib_target', 'pcblib_target')) and not any(
                        box.isChecked() for box in (self.schlib_box, self.pcblib_box)):
                    self.schlib_box.setChecked(True)
                    self.pcblib_box.setChecked(True)
                self.update_merge_controls()
                self.save_settings()
            elif not any(self.export_targets.get(key) for key in ('schlib_target', 'pcblib_target', 'project_path')):
                self.lib_append_box.setChecked(False)
        dialog.finished.connect(completed)
        dialog.show()

    def export_options(self):
        formats = tuple(name for name, box in [('STEP', self.step_box), ('OBJ', self.obj_box),
                        ('SCHLIB', self.schlib_box), ('PCBLIB', self.pcblib_box)] if box.isChecked())
        merge = self.lib_merge_box.isChecked()
        return Options(Path(self.path_input.text().strip()).expanduser().resolve(), formats,
                       merge and self.merge_schlib_box.isChecked(), merge and self.merge_pcblib_box.isChecked(),
                       self.schlib_name_input.text(), self.pcblib_name_input.text(),
                       merge and self.keep_schlib_box.isChecked(), merge and self.keep_pcblib_box.isChecked(),
                       append_mode=self.lib_append_box.isChecked(),
                       **(self.export_targets if self.lib_append_box.isChecked() else {}))

    def start_batch(self):
        if self.batch_running or self.worker and self.worker.isRunning():
            return
        if self.lib_append_box.isChecked() and not self.ids and not self.input.toPlainText().strip():
            self.start_existing_library_import()
            return
        formats = tuple(name for name, box in [('STEP', self.step_box), ('OBJ', self.obj_box),
                        ('SCHLIB', self.schlib_box), ('PCBLIB', self.pcblib_box)] if box.isChecked())
        if not formats:
            self.run_status.setText(ui_text('请至少选择一种模型或 AD 元件库格式'))
            return
        if not self.lib_append_box.isChecked() and not self.path_input.text().strip():
            self.run_status.setText(ui_text('请选择保存目录'))
            return
        ids, invalid, _ = parse_part_numbers(self.input.toPlainText())
        if invalid or not ids or ids != self.ids:
            if not self.load_queue():
                return
        rows = self.checked_rows()
        if not rows:
            self.run_status.setText(ui_text('请至少勾选一个要下载的器件'))
            return
        try:
            options = self.export_options()
            destination = options.output_root()
            options.merged_paths()
            from library_merge import read_library
            from altium_project import read_project
            for fmt, path in options.merged_paths().items():
                if options.appends_to(fmt, path):
                    read_library(path, fmt)
            if options.project_path:
                if not any(fmt in options.formats for fmt in ('SCHLIB', 'PCBLIB')):
                    raise DownloadError(ui_text('请至少选择一种 AD 库格式以加入工程'))
                read_project(options.project_path)
        except (DownloadError, OSError, UnicodeError) as exc:
            record_error(exc, 'export.validation_failed', mode=self.library_mode(), stage='validate')
            self.run_status.setText(str(exc))
            return
        try:
            destination.mkdir(parents=True, exist_ok=True)
            if not os.access(destination, os.W_OK):
                raise PermissionError(ui_text('下载目录不可写'))
        except OSError as exc:
            record_error(exc, 'download.destination_unwritable')
            self.run_status.setText(ui_text('保存目录不可写：') + str(exc))
            return
        self.batch_ids = [self.ids[row] for row in rows]
        for row in rows:
            self.results.pop(self.ids[row], None)
            item = self.table.item(row, RESULT_COLUMN)
            item.setText(ui_text('等待下载'))
            item.setToolTip('')
            item.setForeground(QColor(theme_manager().tokens['text']))
        self.progress_bar.setRange(0, len(rows))
        self.progress_bar.setValue(0)
        self.summary.setText(ui_message('{0} 个器件', len(self.ids)))
        if not options.append_mode:
            self.path_input.setText(str(destination))
        self.save_settings()
        self.set_running(True)
        self.run_status.setText(ui_message('开始下载，共勾选 {0} 个器件…', len(rows)))
        worker = BatchWorker(self.batch_ids[:], options, self, rows=rows)
        self.worker = worker
        worker.phase.connect(self.update_phase)
        worker.result.connect(self.update_result)
        worker.completed.connect(self.complete_batch)
        worker.finished.connect(self.worker_finished)
        worker.start()

    def start_existing_library_import(self):
        options = self.export_options()
        if not options.existing_targets() or not options.imports_project():
            self.run_status.setText(ui_text('请配置已有库和 PCB 工程，并勾选“将已选已有库导入PCB工程”'))
            log_event('WARNING', 'export.existing_import_rejected', reason='missing_targets_or_project_link')
            return
        self.save_settings()
        self.set_running(True)
        self.run_status.setText(ui_text('正在将已选已有库加入 PCB 工程…'))
        worker = LibraryImportWorker(options, self)
        self.worker = worker
        worker.completed.connect(self.complete_existing_library_import)
        worker.finished.connect(self.worker_finished)
        worker.start()

    def complete_existing_library_import(self, result, error):
        self.run_status.setText(error if error else
            ui_message('工程导入完成：新增 {0} 份库，跳过 {1} 份已有引用。', result['added'], result['skipped']))
        self.batch_done.emit()

    def update_phase(self, row, status, title):
        result = self.results.get(self.ids[row])
        if result is not None and result.status != ui_text('等待合并'):
            return
        self.table.item(row, RESULT_COLUMN).setText(status)
        if title:
            self.table.item(row, MODEL_COLUMN).setText(title)
        batch_ids = self.batch_ids or self.ids
        completed = sum(part in self.results and self.results[part].status != ui_text('等待合并') for part in batch_ids)
        self.run_status.setText(ui_message('已完成 {0} / {1} · {2} · {3}', completed, len(batch_ids), self.ids[row], ui_text(status)))

    def update_result(self, row, result):
        self.results[result.part] = result
        item = self.table.item(row, RESULT_COLUMN)
        item.setText(result.status)
        item.setToolTip(result.message)
        role = {ui_text('成功'): 'success', ui_text('部分完成'): 'warning', ui_text('失败'): 'error', ui_text('无模型'): 'warning', ui_text('已取消'): 'muted'}.get(result.status, 'text')
        item.setForeground(QColor(theme_manager().tokens[role]))
        info = self.component_info.get(result.part, {})
        title = result.title or info.get('title', '')
        model = result.model or info.get('model', '')
        model_item = self.table.item(row, MODEL_COLUMN)
        if title or model:
            self.component_info[result.part] = {'title': title, 'model': model}
            model_item.setText(title or model)
            model_item.setToolTip(self.component_tooltip({'title': title, 'model': model}))
        self.progress_bar.setValue(sum(part in self.results and self.results[part].status != ui_text('等待合并')
                                       for part in (self.batch_ids or self.ids)))
        self.selection_changed()

    def complete_batch(self, results):
        ok = sum(r.status == ui_text('成功') for r in results)
        partial = sum(r.status == ui_text('部分完成') for r in results)
        failed = sum(r.status in (ui_text('失败'), ui_text('无模型')) for r in results)
        cancelled = sum(r.status == ui_text('已取消') for r in results)
        self.run_status.setText(ui_message('完成 · 成功 {0} · 部分完成 {1} · 失败或无模型 {2} · 取消 {3}', ok, partial, failed, cancelled))
        self.summary.setText(ui_message('{0} / {1} 成功', ok, len(results)))
        self.batch_done.emit()

    def worker_finished(self):
        self.set_running(False)
        if self.close_when_finished:
            self.close()

    @traced('download.stop_requested', level='INFO')
    def stop_batch(self):
        if self.worker:
            self.worker.cancelled.set()
            self.stop_button.setEnabled(False)
            self.run_status.setText(ui_text('正在停止，等待当前网络请求结束…'))

    def selected_part(self):
        row = self.table.currentRow()
        return self.ids[row] if 0 <= row < len(self.ids) else ''

    def selection_changed(self):
        part = self.selected_part()
        self.update_download_detail(part)
        if part and part != self.current_preview:
            self.show_preview(part)

    def update_download_detail(self, part):
        self.store_button.setEnabled(bool(part or self.current_preview))
        result = self.results.get(part)
        self.part_folder_button.setEnabled(bool(result and result.folder and Path(result.folder).is_dir()))
        if result:
            self.detail.setText(part + ' · ' + ui_text(result.message))
            self.detail.setToolTip(result.message)
        elif part:
            self.detail.setText(part + ui_text(' · 右侧自动预览，可切换 3D 模型、符号、封装和商品图片'))
            self.detail.setToolTip('')

    def _setup_preview(self):
        self.web = QWebEngineView(self.preview_stack)
        page = PreviewPage(self.web_profile, self.web)
        page.state.connect(self.on_web_preview_state)
        self.web.setPage(page)
        self.web.loadFinished.connect(self.on_viewer_page_loaded)
        page.renderProcessTerminated.connect(self.on_viewer_terminated)
        self.web.settings().setAttribute(QWebEngineSettings.WebAttribute.WebGLEnabled, True)
        self.web.settings().setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, False)
        self.web.setContextMenuPolicy(Qt.NoContextMenu)
        self.preview_stack.addWidget(self.web)
        # setPage alone leaves Qt's rendering widget uninitialized. Load a local
        # blank page before show() so the first preview cannot recreate the HWND.
        # The placeholder remains visible; no model or remote page is loaded.
        self.web.setHtml('<!doctype html><html><body style="background:#f3f6fa"></body></html>')

    def set_preview_mode(self, mode):
        self.preview_mode = mode
        self.preview_mode_buttons[mode].setChecked(True)
        self.fit_button.setVisible(True)
        self.preview_legend.setVisible(mode == 'footprint')
        self.symbol_unit_box.setVisible(mode == 'symbol' and self.symbol_unit_box.count() > 1)
        part = self.current_preview or self.selected_part()
        if part:
            self.show_preview(part)
        else:
            self.preview_hint.setText(ui_text('选择左侧列表中的器件\n即可查看') + {'3d': ui_text('3D 模型'), 'symbol': ui_text('符号'), 'footprint': ui_text('封装'), 'photo': ui_text('商品图片')}[mode])
            self.preview_stack.setCurrentWidget(self.preview_empty)
            self.on_preview_state('empty', ui_text('选择器件后加载预览'))

    @traced('preview.select', lambda self, part, *a, **kw: {'part': safe_part(part)})
    def show_preview(self, part, reload=False):
        if not part or self.close_when_finished:
            return
        self.current_preview = part
        self.preview_diagnostic_context = current_context()
        if self.web is not None:
            self.web.page().diagnostic_context = self.preview_diagnostic_context
        self.reload_button.setEnabled(True)
        self.store_button.setEnabled(True)
        if self.preview_mode == 'photo':
            self.library_pending = self.model_pending = None
            self.symbol_unit_box.hide()
            self.preview_caption.setText(part + ui_text(' · 商品图片'))
            self.preview_stack.setCurrentWidget(self.product_preview)
            self.product_preview.select(part, refresh=reload)
            return
        if self.preview_mode != '3d':
            self.show_library_preview(part, reload)
            return
        self.library_pending = None
        self.symbol_unit_box.hide()
        self.preview_caption.setText(part + ui_text(' · 3D 模型'))
        self.preview_stack.setCurrentWidget(self.web)
        if part == self.current_3d and self.viewer_started and not reload:
            self.fit_button.setEnabled(self.web_state[0] == 'ready')
            self.on_preview_state(*self.web_state)
            return
        self.current_3d = part
        self.web_revision += 1
        self.web_model = None
        self.web_error = ''
        self.web_state = ('loading', ui_text('正在获取官方 3D 模型…'))
        self.on_preview_state(*self.web_state)
        self.fit_button.setEnabled(False)
        if not self.viewer_started or reload:
            self.viewer_started = True
            self.viewer_ready = False
            self.viewer_page_loads += 1
            html = (ROOT / 'viewer.html').read_text(encoding='utf-8').replace('__PART_JSON__', json.dumps(part))
            html = html.replace('__REVISION_JSON__', str(self.web_revision))
            self.web.setHtml(html)
        else:
            self.update_viewer_part()
        self.request_model_preview(part, self.web_revision, reload)

    def update_viewer_part(self):
        if not self.viewer_ready:
            return
        args = json.dumps(self.current_3d) + ',' + str(self.web_revision)
        self.web.page().runJavaScript(ui_message('window.loadLcscPart && window.loadLcscPart({0})', args))
        if self.web_model is not None:
            payload = json.dumps(self.web_model, separators=(',', ':'))
            self.web.page().runJavaScript(ui_message('window.showMesh && window.showMesh({0},{1})', payload, args))
        elif self.web_error:
            message = json.dumps(self.web_error)
            self.web.page().runJavaScript(ui_message('window.modelError && window.modelError({0},{1})', args, message))

    def on_viewer_page_loaded(self, ok):
        self.viewer_ready = ok
        if ok and self.viewer_started:
            self.update_viewer_appearance()
            self.update_viewer_part()
        elif self.viewer_started:
            self.on_web_preview_state(self.web_revision, 'error', ui_text('3D 画布加载失败，请重新加载'))

    def request_model_preview(self, part, revision, refresh=False):
        self.model_pending = (part, revision, refresh)
        if self.model_worker is not None:
            self.model_worker.cancelled.set()
            return
        pending, self.model_pending = self.model_pending, None
        with log_context(getattr(self, 'preview_diagnostic_context', {})):
            worker = ModelPreviewWorker(*pending, parent=self)
        self.model_worker = worker
        worker.loaded.connect(self.model_preview_loaded)
        worker.failed.connect(self.model_preview_failed)
        worker.finished.connect(self.model_preview_finished)
        worker.start()

    def model_preview_loaded(self, part, revision, payload):
        if self.close_when_finished or revision != self.web_revision or part != self.current_3d:
            log_event('DEBUG', 'preview.stale_result_ignored', part=safe_part(part), revision=revision)
            return
        self.web_model = payload
        self.update_viewer_part()

    def model_preview_failed(self, part, revision, message):
        if self.close_when_finished or revision != self.web_revision or part != self.current_3d:
            return
        self.web_error = message
        self.on_web_preview_state(revision, 'error', message)
        args = ','.join(json.dumps(value) for value in (part, revision, message))
        self.web.page().runJavaScript(ui_message('window.modelError && window.modelError({0})', args))

    def model_preview_finished(self):
        worker, self.model_worker = self.model_worker, None
        if worker:
            worker.deleteLater()
        pending, self.model_pending = self.model_pending, None
        if self.close_when_finished:
            self.close()
        elif pending and pending[1] == self.web_revision:
            self.request_model_preview(*pending)

    def on_viewer_terminated(self, *args):
        log_event('ERROR', 'preview.renderer_terminated', viewer='3d', part=safe_part(self.current_3d),
                  termination_status=getattr(args[0], 'value', None) if args else None,
                  exit_code=args[1] if len(args) > 1 else None, revision=self.web_revision)
        self.viewer_started = False
        self.on_web_preview_state(self.web_revision, 'error', ui_text('3D 查看器已停止，请重新加载'))

    def on_web_preview_state(self, token, status, message):
        if token not in (-1, self.web_revision):
            return
        self.web_state = (status, message)
        if self.preview_mode == '3d':
            self.fit_button.setEnabled(status == 'ready')
            self.on_preview_state(status, message)

    @traced('preview.library_select', lambda self, part, *a, **kw: {'part': safe_part(part)})
    def show_library_preview(self, part, reload=False):
        self.preview_diagnostic_context = current_context()
        if reload:
            self.library_cache.pop(part, None)
        if part in self.library_cache:
            self.library_cache.move_to_end(part)
            self.display_library_preview(part, self.library_cache[part])
            return
        self.preview_caption.setText(part + ' · ' + (ui_text('符号') if self.preview_mode == 'symbol' else ui_text('封装')))
        self.symbol_unit_box.hide()
        self.preview_hint.setText(ui_text('正在加载器件预览…'))
        self.preview_stack.setCurrentWidget(self.preview_empty)
        self.fit_button.setEnabled(False)
        self.on_preview_state('loading', ui_text('正在获取商城官方符号和封装 SVG…'))
        if self.library_worker is not None:
            if self.library_worker.part != part or self.library_worker.cancelled.is_set() or reload:
                self.library_pending = part
                self.library_worker.cancelled.set()
            return
        self.start_library_preview(part)

    def start_library_preview(self, part):
        with log_context(getattr(self, 'preview_diagnostic_context', {})):
            worker = LibraryPreviewWorker(part, self)
        self.library_worker = worker
        worker.loaded.connect(self.library_preview_loaded)
        worker.failed.connect(self.library_preview_failed)
        worker.finished.connect(self.library_preview_finished)
        worker.start()

    def library_preview_loaded(self, part, preview):
        if self.library_worker and self.library_worker.cancelled.is_set():
            return
        self.library_cache[part] = preview
        self.library_cache.move_to_end(part)
        while len(self.library_cache) > 32:
            self.library_cache.popitem(last=False)
        if self.preview_mode in ('symbol', 'footprint') and part == self.current_preview:
            self.display_library_preview(part, preview)

    def library_preview_failed(self, part, message):
        if self.library_worker and self.library_worker.cancelled.is_set():
            return
        if self.preview_mode in ('symbol', 'footprint') and part == self.current_preview:
            self.preview_hint.setText(ui_text('预览加载失败\n可点击「重新加载」重试'))
            self.on_preview_state('error', message)

    def library_preview_finished(self):
        worker, self.library_worker = self.library_worker, None
        if worker:
            worker.deleteLater()
        pending, self.library_pending = self.library_pending, None
        if self.close_when_finished:
            self.close()
        elif pending and self.preview_mode in ('symbol', 'footprint') and pending == self.current_preview:
            if pending in self.library_cache:
                self.display_library_preview(pending, self.library_cache[pending])
            else:
                self.start_library_preview(pending)

    def display_library_preview(self, part, preview):
        if self.preview_mode == 'symbol':
            if self.symbol_unit_part != part or self.symbol_unit_box.count() != len(preview.symbols):
                self.symbol_unit_box.blockSignals(True)
                self.symbol_unit_box.clear()
                self.symbol_unit_box.addItems([ui_message('单元 {0}', index + 1) for index in range(len(preview.symbols))])
                self.symbol_unit_box.blockSignals(False)
                self.symbol_unit_part = part
            self.symbol_unit_box.setVisible(len(preview.symbols) > 1)
            document = preview.symbols[max(0, self.symbol_unit_box.currentIndex())]
            view, label = self.symbol_view, ui_text('符号')
        else:
            self.symbol_unit_box.hide()
            document = preview.footprint
            view, label = self.footprint_view, ui_text('封装')
        self.preview_caption.setText(part + ' · ' + label + (' · ' + document.name if document.name else ''))
        with log_context(getattr(self, 'preview_diagnostic_context', {})):
            displayed = not document.error and view.show_document(document)
        if not displayed:
            self.fit_button.setEnabled(False)
            self.preview_hint.setText(document.error or ui_text('此器件预览暂不可用'))
            self.preview_stack.setCurrentWidget(self.preview_empty)
            self.on_preview_state('error', document.error or ui_text('预览图形无法显示'))
            return
        self.preview_stack.setCurrentWidget(view)
        self.on_vector_preview_state(view, *view.load_state)

    def on_vector_preview_state(self, view, status, message):
        if self.preview_mode not in ('symbol', 'footprint') or self.preview_stack.currentWidget() is not view:
            return
        self.fit_button.setEnabled(status == 'ready')
        if status == 'ready':
            label, unit = (ui_text('符号'), ui_text('引脚')) if self.preview_mode == 'symbol' else (ui_text('封装'), ui_text('焊盘'))
            message = ui_message('商城官方 SVG · {0} · {1} 个{2} · 滚轮缩放，拖动平移', label, view.document.count, unit)
        self.on_preview_state(status, message)

    def show_symbol_unit(self, index):
        if self.preview_mode == 'symbol' and self.current_preview in self.library_cache:
            self.display_library_preview(self.current_preview, self.library_cache[self.current_preview])

    @traced('preview.fit')
    def fit_preview(self):
        if self.preview_mode == '3d':
            self.web.page().runJavaScript('window.fitModel && window.fitModel()')
        elif self.preview_mode == 'symbol':
            self.symbol_view.fit_content()
        elif self.preview_mode == 'footprint':
            self.footprint_view.fit_content()
        elif self.preview_mode == 'photo':
            self.product_preview.view.fit_photo()

    def product_preview_state(self, state, message):
        if self.preview_mode == 'photo' and self.product_preview.part == self.current_preview:
            self.fit_button.setEnabled(state == 'ready')
            self.on_preview_state(state, message)

    def on_preview_state(self, status, message):
        with log_context(getattr(self, 'preview_diagnostic_context', {})):
            log_event('ERROR' if status == 'error' else 'DEBUG', 'preview.display_state',
                      viewer=self.preview_mode, part=safe_part(self.current_preview),
                      state=status if status in ('empty', 'loading', 'ready', 'error') else 'unknown',
                      revision=self.web_revision)
        self.preview_state = status
        self.preview_status.setText(message)
        self.preview_status.setProperty('state', status)
        self.preview_status.style().unpolish(self.preview_status)
        self.preview_status.style().polish(self.preview_status)
        self.preview_changed.emit(status)

    @traced('navigation.store')
    def open_store(self):
        part = self.current_preview or self.selected_part()
        if part:
            result = self.results.get(part)
            url = storefront_url(part, result.store_url if result else '')
            opened = QDesktopServices.openUrl(QUrl(url))
            log_event('INFO' if opened else 'WARNING', 'navigation.store_result', opened=opened, part=safe_part(part))

    @traced('navigation.export_folder')
    def open_part_folder(self):
        result = self.results.get(self.selected_part())
        if result and result.folder:
            opened = QDesktopServices.openUrl(QUrl.fromLocalFile(result.folder))
            log_event('INFO' if opened else 'WARNING', 'navigation.export_folder_result', opened=opened)

    def show_help(self):
        previous = getattr(self, 'help_page', None)
        if previous is not None and not previous._page_finished:
            previous.show()
            return
        if previous is not None:
            previous.deleteLater()
        message = HelpDialog(self)
        message.setWindowTitle(ui_text('使用说明与来源'))
        message.setText(ui_text('<b>LCSC3D</b><br>版本：') + VERSION + ui_text('<br><br>'
            '1. 输入 C 开头的立创编号，支持换行、空格和逗号。<br>'
            '2. 载入列表后自动查询型号，勾选需要下载的器件；可使用「全选」「反选」「删除已勾选器件」。删除只移除列表记录，保留已下载文件。<br>'
            '3. 选择保存目录和 3D 格式，点击「开始下载」，仅下载勾选的器件。<br>'
            '4. 载入列表后自动预览首个器件，单击其他器件即可切换预览。<br><br>'
            '立创商城：按 C 编号、型号或关键词搜索，每页 50 个，翻页读取；勾选跨页保留，新搜索清空。<br>'
            '搜索结果显示现货库存与人民币单价，每个商品独立选择价格梯度，默认 1+ 或最低起订量；翻页保留选择。<br>'
            '商品介绍和参数自动换行，可在右侧文字详情区滚动阅读完整内容。<br>'
            '顶部「账号登录」支持微信扫码、账号密码和手机验证码；服务端图片验证也在原生界面完成。<br>'
            '账号收藏：登录后获取收藏，搜索商品可收藏到账号，也可取消当前元件的收藏。'
            '搜索结果和账号收藏均可勾选加入下载列表，保留已有勾选与下载结果。'
            '商城元件默认不勾选；预览当前高亮行会切换到工作台，返回商城后保留当前页和选择。'
            '默认记住登录，重启后自动恢复；取消勾选时只保留本次登录。「退出登录」可清除已保存会话。<br><br>'
            '左侧「设置」可分别选择商城与检查更新是否使用系统代理，并调整日志等级；默认 Debug。「打开日志」可打开日志目录。<br><br>'
            '可保存官方 STEP、OBJ，并导出原生 AD SchLib 符号库、PcbLib 封装库；不导出 JSON 或 SVG。<br>'
            'SchLib / PcbLib 可分别勾选合并并自定义库名，合并文件保存在所选目录根部；默认逐器件导出。<br>'
            'Lib「合并」「追加」互斥，勾选后显示配置。未勾选「独立导出器件」时，3D 文件集中到 SchLib 旁的“库名_3D”文件夹；仅有 PcbLib 时跟随其名称。<br>'
            '「追加」支持只选一种已有库，保存目录由库或工程决定；可同时将所选库加入 PCB 工程。下载列表为空时点击「导入已有库」直接加入工程。<br>'
            '相同 footprint 自动复用并同步符号引用；同名不同内容加序号区分。Lib 文件和库内名称不自动追加器件编号。<br>'
            'AD 库保留引脚、焊盘和孔数据，遇到不支持的图元会提示失败；PcbLib 不内嵌 3D 模型。<br>'
            '未合并的文件单独保存到“器件名_编号”目录，AD 库文件按元件型号命名；下载时覆盖已有同名文件。<br>'
            '3D 预览由 LCSC3D 在本地渲染官方模型；拖动旋转，滚轮缩放，右键拖动平移。<br><br>'
            '右侧上方可切换「3D 模型 / 符号 / 封装 / 商品图片」，无需先下载。<br>'
            '商品图片展示商城原图，可切换多张图片、缩放与拖动。<br>'
            '符号和封装直接加载商城使用的官方 SVG，支持滚轮缩放、拖动平移及「适应窗口」；多单元符号可选择单元。<br><br>'
            '启动后自动后台检查新版，发现新版时提醒；连接失败或已是最新版时不弹窗。'
            '也可点击「设置 → 关于与支持 → 检查更新」手动查询；便携 EXE 支持下载、SHA-256 校验并重启更新。<br>'
            '模型来源：<a href="https://lceda.cn/">JLCEDA</a> / <a href="https://easyeda.com/">EasyEDA 官方库</a>。<br>'
            '资源下载与本地 3D 预览由本项目实现，软件采用 AGPL-3.0-or-later。<br>'
            '对应源码、构建脚本与第三方说明随交付提供。'))
        self.help_page = message
        message.show()

    def schedule_startup_update_check(self):
        if not self.startup_update_attempted and not self.close_when_finished:
            self.startup_update_timer.start(1500)

    def check_startup_update(self):
        if self.startup_update_attempted or self.close_when_finished:
            return
        self.startup_update_attempted = True
        self.startup_update_timer.stop()
        check = StartupUpdateCheck(VERSION, self, source=self.update_source)
        self.startup_update_check = check
        check.completed.connect(self.startup_update_completed)
        check.start()

    def cancel_startup_update(self):
        self.startup_update_timer.stop()
        self.startup_update_attempted = True
        notice = getattr(self, 'update_notice', None)
        if notice is not None and isValid(notice):
            notice.close()
        self.update_notice = None
        check, self.startup_update_check = self.startup_update_check, None
        if check is not None:
            check.cancel()
            check.deleteLater()
            self.startup_update_outcome = 'cancelled'
            log_event('INFO', 'update.startup_notification_cancelled')

    def startup_update_completed(self, result):
        check, self.startup_update_check = self.startup_update_check, None
        if check is None:
            return
        check.deleteLater()
        self.startup_update_outcome = result['outcome']
        if self.close_when_finished or not self.isVisible() or result['release'] is None:
            return
        if self.update_dialog is not None and self.update_dialog.isVisible():
            return
        release = result['release']
        def show_update():
            self.update_dialog = UpdateDialog(VERSION, self, source=self.update_source, release=release, controller=self)
            self.update_dialog.show()
        self.update_notice = notify(self, ui_text('软件更新'), ui_message('发现新版本 {0}', release.version),
                                    actions=((ui_text('查看更新'), show_update),), persistent=True)
        log_event('INFO', 'update.startup_notification_shown', target_version=release.version)

    def check_updates(self):
        self.cancel_startup_update()
        if self.update_dialog is not None and not self.update_dialog._page_finished:
            self.update_dialog.raise_()
            self.update_dialog.activateWindow()
            return
        if self.update_dialog is not None:
            self.update_dialog.deleteLater()
        owner = self.settings_dialog if self.settings_dialog is not None and self.settings_dialog.isVisible() else self
        self.update_dialog = UpdateDialog(VERSION, owner, source=self.update_source, controller=self)
        self.update_dialog.show()

    def begin_update(self, manifest):
        self.save_settings()
        launch_update(manifest)
        self.close_when_finished = True
        QTimer.singleShot(0, self.close)

    def closeEvent(self, event):
        self.cancel_startup_update()
        self.product_preview.jobs.cancel_all()
        if self.favorites_dialog:
            self.favorites_dialog.shutdown()
            self.favorites_dialog.close()
        store_running = (self.favorites_dialog and self.favorites_dialog.has_jobs()) or any(d.has_jobs() for d in self.retired_stores)
        update_worker = self.update_dialog.worker if self.update_dialog else None
        log_worker = self.settings_dialog.worker if self.settings_dialog else None
        if self.product_preview.jobs.workers or store_running or update_worker is not None or log_worker is not None or self.model_worker is not None or self.library_worker is not None or self.info_worker is not None or self.worker and self.worker.isRunning():
            if update_worker is not None and not self.close_when_finished:
                self.update_dialog.closing = True
                update_worker.cancelled.set()
                update_worker.finished.connect(self.close)
            if log_worker is not None and not self.close_when_finished:
                log_worker.finished.connect(self.close)
            self.close_when_finished = True
            self.info_pending = None
            self.library_pending = None
            self.model_pending = None
            for worker in (self.info_worker, self.library_worker, self.model_worker):
                if worker is not None:
                    worker.cancelled.set()
            self.setEnabled(False)
            if self.worker and self.worker.isRunning():
                self.stop_batch()
            self.run_status.setText(ui_text('正在关闭，等待后台任务结束…'))
            event.ignore()
            return
        self.save_settings()
        log_event('INFO', 'application.closed')
        event.accept()


def main():
    configure_runtime_paths()
    parser = argparse.ArgumentParser()
    parser.add_argument('--self-test', metavar='FOLDER', help=argparse.SUPPRESS)
    parser.add_argument('--self-test-ad', metavar='FOLDER', help=argparse.SUPPRESS)
    parser.add_argument('--self-test-ad-parts', default='C2765186', help=argparse.SUPPRESS)
    parser.add_argument('--self-test-ad-merge', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--self-test-ad-integrate', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--self-test-store', '--self-test-favorites', dest='self_test_favorites', metavar='FOLDER', help=argparse.SUPPRESS)
    parser.add_argument('--self-test-store-live', metavar='FOLDER', help=argparse.SUPPRESS)
    parser.add_argument('--self-test-settings', metavar='FOLDER', help=argparse.SUPPRESS)
    parser.add_argument('--self-test-export', metavar='FOLDER', help=argparse.SUPPRESS)
    parser.add_argument('--capture-docs', metavar='FOLDER', help=argparse.SUPPRESS)
    parser.add_argument('--self-test-material', metavar='FOLDER', help=argparse.SUPPRESS)
    parser.add_argument('--update-ack', metavar='PLAN', help=argparse.SUPPRESS)
    parser.add_argument('--local-update-source', metavar='URL', help=ui_text('仅测试：本机 HTTP 更新源（http://127.0.0.1:端口）'))
    parser.add_argument('--local-update-current-version', metavar='VERSION', help=ui_text('仅测试：模拟版本比较的当前版本'))
    parser.add_argument('--self-test-local-update', metavar='FOLDER', help=argparse.SUPPRESS)
    parser.add_argument('--self-test-startup-update', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    from updater import LocalUpdateSource, UpdateError
    if (args.local_update_current_version or args.self_test_local_update) and not args.local_update_source:
        parser.error(ui_text('本地更新测试参数必须同时提供 --local-update-source'))
    if args.self_test_startup_update and not args.self_test_local_update:
        parser.error('--self-test-startup-update requires --self-test-local-update')
    try:
        update_source = LocalUpdateSource(args.local_update_source, args.local_update_current_version) if args.local_update_source else None
    except UpdateError as exc:
        parser.error(str(exc))
    ad_test_parts, ad_invalid_parts, _ = parse_part_numbers(args.self_test_ad_parts)
    if args.self_test_ad and (not ad_test_parts or ad_invalid_parts):
        parser.error('--self-test-ad-parts requires valid C numbers')
    if args.self_test_ad_integrate and not args.self_test_ad:
        parser.error('--self-test-ad-integrate requires --self-test-ad')
    QApplication.setAttribute(Qt.AA_ShareOpenGLContexts)
    app = QApplication([sys.argv[0]])
    app.setApplicationName('LCSC3D')
    app.setApplicationVersion(VERSION)
    app.setStyle('Fusion')
    app.setFont(QFont('Microsoft YaHei UI', 9))
    app.setStyleSheet(STYLES)
    logging.getLogger().setLevel(logging.ERROR)
    test_destination = next((value for value in (args.self_test, args.self_test_ad, args.self_test_favorites,
                                                  args.self_test_store_live, args.self_test_settings,
                                                  args.self_test_export, args.capture_docs, args.self_test_material) if value), None)
    configure_logging('DEBUG' if test_destination else initial_log_level(SETTINGS_PATH), version=VERSION,
                      directory=Path(test_destination) / 'logs' if test_destination else None)
    install_exception_hooks()
    log_runtime()
    from app_logging import install_qt_logging, start_crash_capture
    install_qt_logging()
    start_crash_capture()
    window = MainWindow(settings_enabled=not bool(test_destination), update_source=update_source)
    log_event('INFO', 'application.started', **window.preferences.to_mapping())
    window.show()
    if args.self_test_local_update:
        from update_selftest import start
        start(window, args.self_test_local_update, startup=args.self_test_startup_update)
    if not test_destination and (not args.self_test_local_update or args.self_test_startup_update):
        window.schedule_startup_update_check()
    if args.self_test_settings:
        from settings_selftest import start
        start(window, args.self_test_settings)
        return app.exec()
    if args.self_test_material:
        from material_selftest import start
        start(window, args.self_test_material)
        return app.exec()
    if args.self_test_export:
        from export_selftest import start
        start(window, args.self_test_export)
        return app.exec()
    if args.capture_docs:
        from docs_capture import start
        start(window, args.capture_docs)
        return app.exec()
    if args.self_test_favorites:
        from favorites_selftest import start
        start(window, args.self_test_favorites)
        return app.exec()
    if args.self_test_store_live:
        from favorites_selftest import start_live
        start_live(window, args.self_test_store_live)
        return app.exec()
    if args.update_ack:
        QTimer.singleShot(250, lambda: acknowledge_update(args.update_ack))
    if getattr(sys, 'frozen', False) and not (args.self_test or args.self_test_ad or update_source is not None):
        QTimer.singleShot(8000, lambda: threading.Thread(target=cleanup_updates, args=(updates_directory(),), daemon=True).start())
    if args.self_test_ad:
        destination = Path(args.self_test_ad).resolve()
        destination.mkdir(parents=True, exist_ok=True)
        window.path_input.setText(str(destination / ui_text('AD 元件库')))
        window.step_box.setChecked(False)
        window.obj_box.setChecked(False)
        window.schlib_box.setChecked(True)
        window.pcblib_box.setChecked(True)
        if args.self_test_ad_merge or args.self_test_ad_integrate:
            window.lib_merge_box.setChecked(True)
            window.merge_schlib_box.setChecked(True)
            window.merge_pcblib_box.setChecked(True)
            window.schlib_name_input.setText(ui_text('项目符号.SchLib'))
            window.pcblib_name_input.setText(ui_text('项目封装'))
        if args.self_test_ad_integrate:
            window.keep_schlib_box.setChecked(True)
            window.keep_pcblib_box.setChecked(True)
            window.export_targets = {'schlib_target': str(destination/ui_text('已有库.SchLib')),
                                     'pcblib_target': str(destination/ui_text('已有库.PcbLib')),
                                     'project_path': str(destination/ui_text('测试工程.PrjPcb')),
                                     'keep_individual': True, 'import_existing_to_project': True}
            window.lib_append_box.setChecked(True)
            window.update_merge_controls()
        window.set_preview_mode('symbol')
        window.input.setPlainText('\n'.join(ad_test_parts))
        window.load_queue()
        state = {'done': False, 'batch_done': False}

        def finish_ad_test(force=False):
            if state['done'] or not (force or state['batch_done'] and window.preview_state in ('ready', 'error')):
                return
            state['done'] = True
            results = list(window.results.values())
            ok = (set(window.results) == set(ad_test_parts)
                  and all(result.status == ui_text('成功')
                          and {Path(file).suffix for file in result.files} == {'.SchLib', '.PcbLib'}
                          for result in results))
            window.grab().save(str(destination / ui_text('AD导出界面.png')))
            (destination / 'verification.json').write_text(json.dumps({
                'version': VERSION, 'frozen': bool(getattr(sys, 'frozen', False)), 'success': ok,
                'parts': ad_test_parts,
                'merged': args.self_test_ad_merge,
                'integrated': args.self_test_ad_integrate,
                'export_targets': window.export_targets,
                'formats': {'step': window.step_box.isChecked(), 'obj': window.obj_box.isChecked(),
                            'schlib': window.schlib_box.isChecked(), 'pcblib': window.pcblib_box.isChecked()},
                'results': [vars(result) for result in results], 'preview': window.preview_state,
            }, ensure_ascii=False, indent=2), encoding='utf-8')
            if window.worker:
                window.worker.cancelled.set()
            window.close()
            QTimer.singleShot(1000, lambda: app.exit(0 if ok else 1))

        def ad_batch_done(results):
            state['batch_done'] = True
            QTimer.singleShot(300, finish_ad_test)

        def start_ad_test():
            window.start_batch()
            if window.worker:
                window.worker.completed.connect(ad_batch_done)

        window.preview_changed.connect(lambda status: finish_ad_test())
        QTimer.singleShot(500, start_ad_test)
        QTimer.singleShot(60000, lambda: finish_ad_test(True))
        return app.exec()
    if args.self_test:
        from PySide6.QtCore import QEvent, QObject

        class PreviewWindowProbe(QObject):
            def __init__(self):
                super().__init__(window)
                self.initial_id = int(window.winId())
                self.events = []
                window.installEventFilter(self)

            def eventFilter(self, watched, event):
                if event.type() in (QEvent.Hide, QEvent.WinIdChange):
                    self.events.append(event.type().name)
                return False

        preview_probe = PreviewWindowProbe()
        destination = Path(args.self_test).resolve()
        destination.mkdir(parents=True, exist_ok=True)
        window.path_input.setText(str(destination / ui_text('批量下载测试')))
        window.input.setPlainText('C2040\nc20197, C2040\nC163691\nC999999999999')
        window.obj_box.setChecked(True)
        state = {'batch': False, 'preview': False, 'initial_3d_done': False, 'library': False, 'library_started': False,
                 'capture_pending': False, 'step': 0, 'done': False,
                 'titles_before_download': {},
                 'second_3d_started': False, 'second_3d_done': False, 'second_3d_ready': False}
        preview_timings = {}
        preview_started = time.perf_counter()
        library_steps = [('symbol', 0), ('footprint', 0), ('symbol', 1), ('footprint', 1)]

        def finish_if_ready(force=False):
            if state['done'] or not (force or state['batch'] and state['preview'] and state['library']):
                return
            state['done'] = True
            window.grab().save(str(destination / ui_text('软件界面.png')))
            report = {
                'application_name': app.applicationName(), 'window_title': window.windowTitle(),
                'automatic_preview': not any(button.text() == ui_text('在线预览') for button in window.findChildren(QPushButton))
                                     and window.current_preview == window.selected_part(),
                'viewer_page_loads': window.viewer_page_loads,
                'preview_timings': preview_timings,
                'second_3d_ready': state['second_3d_ready'],
                'version': VERSION, 'frozen': bool(getattr(sys, 'frozen', False)),
                'ids': window.ids, 'preview': window.preview_state,
                'titles_before_download': state['titles_before_download'],
                'download_selection': {
                    'checked_ids': [window.ids[row] for row in window.checked_rows()],
                    'unchecked_ids': [part for row, part in enumerate(window.ids) if row not in window.checked_rows()],
                    'selected_only': set(window.results) == set(window.batch_ids),
                },
                'preview_message': window.preview_status.text(),
                'preview_window': {
                    'hwnd_preserved': preview_probe.initial_id == int(window.winId()),
                    'events': preview_probe.events,
                },
                'results': [vars(window.results[part]) for part in window.ids if part in window.results],
                'csv_files': [str(path) for path in Path(window.path_input.text()).rglob('*.csv')],
                'non_model_exports': [str(path) for path in Path(window.path_input.text()).rglob('*')
                                      if path.is_file() and path.suffix.lower() not in ('.step', '.obj')],
                'library_previews': {
                    part: {'symbol_pins': [unit.count for unit in preview.symbols],
                           'footprint_pads': preview.footprint.count,
                           'source_url': preview.source_url,
                           'errors': [unit.error for unit in preview.symbols if unit.error]
                                     + ([preview.footprint.error] if preview.footprint.error else [])}
                    for part, preview in window.library_cache.items()
                },
            }
            (destination / 'verification.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
            if window.worker and window.worker.isRunning():
                window.worker.cancelled.set()
                window.worker.wait(45000)
            if window.library_worker is not None:
                window.library_pending = None
                window.library_worker.cancelled.set()
                window.library_worker.wait(45000)
            if window.info_worker is not None:
                window.info_worker.cancelled.set()
                window.info_worker.wait(45000)
            if window.model_worker is not None:
                window.model_pending = None
                window.model_worker.cancelled.set()
                window.model_worker.wait(45000)
            passed = (window.preview_state == 'ready'
                      and state['second_3d_ready']
                      and state['library']
                      and report['preview_window']['hwnd_preserved']
                      and not preview_probe.events
                      and not report['csv_files']
                      and not report['non_model_exports']
                      and report['titles_before_download'].get('C2040') == 'RP2040'
                      and report['titles_before_download'].get('C20197') == '4D03WGJ0102T5E'
                      and report['titles_before_download'].get('C163691', '') not in
                          ('', '—', ui_text('查询中…'), ui_text('查询失败'), ui_text('未提供型号'))
                      and report['download_selection'] == {
                          'checked_ids': ['C2040', 'C20197', 'C999999999999'],
                          'unchecked_ids': ['C163691'], 'selected_only': True}
                      and sum(result.status == ui_text('成功') for result in window.results.values()) == 2
                      and all({Path(file).suffix for file in result.files} == {'.step', '.obj'}
                              for result in window.results.values() if result.part in ('C2040', 'C20197')))
            app.exit(0 if passed else 1)

        def batch_done():
            state['batch'] = True
            start_library_checks()

        def start_library_checks():
            nonlocal preview_started
            if state['batch'] and state['initial_3d_done'] and not state['second_3d_started']:
                state['second_3d_started'] = True
                preview_started = time.perf_counter()
                window.table.selectRow(1)
            elif state['batch'] and state['second_3d_done'] and not state['library_started']:
                state['library_started'] = True
                advance_library_check()

        def advance_library_check():
            if state['step'] == len(library_steps):
                state['library'] = True
                window.table.selectRow(0)
                window.set_preview_mode('3d')
                QTimer.singleShot(2500, lambda: finish_if_ready(True))
                return
            mode, row = library_steps[state['step']]
            window.table.selectRow(row)
            window.set_preview_mode(mode)

        def capture_library_check():
            mode, row = library_steps[state['step']]
            name = (ui_text('符号') if mode == 'symbol' else ui_text('封装')) + '_'+ window.ids[row] + '.png'
            window.grab().save(str(destination / name))
            state['capture_pending'] = False
            state['step'] += 1
            QTimer.singleShot(100, advance_library_check)

        def preview_done(status):
            if state['done']:
                return
            if window.preview_mode == '3d':
                if status in ('ready', 'error'):
                    if not state['initial_3d_done']:
                        state['initial_3d_done'] = True
                        state['preview'] = status == 'ready'
                        preview_timings['first_model_seconds'] = round(time.perf_counter() - preview_started, 3)
                        start_library_checks()
                    elif state['second_3d_started'] and not state['second_3d_done'] and window.current_3d == window.ids[1]:
                        state['second_3d_done'] = True
                        state['second_3d_ready'] = status == 'ready'
                        preview_timings['switch_model_seconds'] = round(time.perf_counter() - preview_started, 3)
                        start_library_checks()
                return
            if status == 'ready' and state['library_started'] and state['step'] < len(library_steps) and not state['capture_pending']:
                mode, row = library_steps[state['step']]
                if window.preview_mode == mode and window.current_preview == window.ids[row]:
                    state['capture_pending'] = True
                    QTimer.singleShot(400, capture_library_check)

        window.batch_done.connect(batch_done)
        window.preview_changed.connect(preview_done)
        window.load_queue()
        assert window.checked_rows() == [0, 1, 2, 3]
        window.invert_selection_button.click()
        assert not window.checked_rows()
        window.select_all_button.click()
        assert window.checked_rows() == [0, 1, 2, 3]
        window.table.item(2, DOWNLOAD_COLUMN).setCheckState(Qt.Unchecked)

        def start_download_check():
            if state['done']:
                return
            if window.info_worker is not None:
                QTimer.singleShot(50, start_download_check)
                return
            state['titles_before_download'] = {
                part: window.table.item(row, MODEL_COLUMN).text() for row, part in enumerate(window.ids)}
            window.grab().save(str(destination / ui_text('型号查询.png')))
            window.start_batch()

        QTimer.singleShot(800, start_download_check)
        QTimer.singleShot(110000, lambda: finish_if_ready(True))
    return app.exec()


if __name__ == '__main__':
    sys.exit(main())
