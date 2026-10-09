"""LCSC3D. Copyright (C) 2026. SPDX-License-Identifier: AGPL-3.0-or-later."""
from __future__ import annotations

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

from PySide6.QtCore import Qt, QThread, QTimer, QUrl, Signal, QRectF
from PySide6.QtGui import QColor, QDesktopServices, QFont, QIcon, QPixmap, QPainter
from PySide6.QtWidgets import (
    QApplication, QButtonGroup, QCheckBox, QComboBox, QFileDialog, QFrame, QHBoxLayout, QHeaderView,
    QLabel, QLineEdit, QMainWindow, QMenu, QMessageBox, QPlainTextEdit, QProgressBar,
    QPushButton, QSizePolicy, QSplitter, QSplitterHandle, QStackedWidget, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget, QAbstractItemView,
)
from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile, QWebEngineSettings
from PySide6.QtWebEngineWidgets import QWebEngineView

from backend import Cancelled, NetworkApi, Options, Result, download_batch, get_component_metadata, parse_part_numbers
from model3d import model_reference, read_obj
from library_preview import build_library_preview, VectorPreviewView, SYMBOL_BACKGROUND, FOOTPRINT_BACKGROUND
from update_ui import UpdateDialog, StartupUpdateCheck
from updater import acknowledge_update, cleanup_updates, launch_update
from favorites import FavoritesDialog, normalize_items
from app_settings import Preferences, set_preferences, write_settings, read_settings, initial_log_level
from app_logging import (configure_logging, log_event, set_log_level, record_error, traced,
                         new_context, current_context, log_context, contextual, submit_logged,
                         safe_part, install_exception_hooks, log_runtime)
from settings_ui import SettingsDialog
from app_paths import data_directory, configure_runtime_paths, updates_directory

VERSION = '2.1.1'
DOWNLOAD_COLUMN, PART_COLUMN, MODEL_COLUMN, RESULT_COLUMN = range(4)
ROOT = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent))
APP_DIR = Path(sys.executable).parent if getattr(sys, 'frozen', False) else Path(__file__).resolve().parent
SETTINGS_PATH = data_directory() / 'LCSC3D-settings.json'
LEGACY_SETTINGS_PATH = APP_DIR / 'LCSC3D-settings.json'

STYLES = '''
QWidget { font-family:"Microsoft YaHei UI"; font-size:13px; color:#23354a; }
QMainWindow, QWidget#canvas { background:#f1f5f8; }
QFrame#card { background:white; border:1px solid #dee5eb; border-radius:12px; }
QLabel#title { font-size:25px; font-weight:700; color:#152c43; }
QLabel#section { font-size:16px; font-weight:700; color:#20364c; }
QLabel#muted { color:#788898; font-size:12px; }
QLabel#badge { color:#187a70; background:#e0f3ee; padding:5px 10px; border-radius:6px; font-weight:600; }
QLineEdit, QPlainTextEdit { background:#fafcfd; border:1px solid #d8e1e8; border-radius:7px; padding:9px; selection-background-color:#cdebe4; }
QLineEdit:focus, QPlainTextEdit:focus { border:1px solid #198f81; }
QPushButton { background:#fff; border:1px solid #cfd9e2; border-radius:7px; padding:8px 13px; color:#32495f; }
QPushButton:hover { background:#f0f6f8; border-color:#99b4c3; }
QPushButton:pressed { background:#e7f0f4; }
QPushButton:disabled { color:#a0acb8; border-color:#e0e6eb; background:#f6f8fa; }
QPushButton#primary { background:#168878; color:white; border:1px solid #168878; font-weight:600; }
QPushButton#primary:hover { background:#117767; }
QPushButton#primary:disabled { background:#b4d1c9; border-color:#b4d1c9; }
QPushButton#previewMode:checked { background:#e0f3ee; color:#117767; border-color:#168878; font-weight:600; }
QComboBox { border:1px solid #cfd9e2; border-radius:5px; padding:5px; background:white; }
QGroupBox { background:white; border:1px solid #dee5eb; border-radius:9px; margin-top:10px; font-weight:600; }
QGroupBox::title { subcontrol-origin:margin; left:14px; padding:0 5px; }
QTabWidget::pane { background:#fff; border:1px solid #dee5eb; border-radius:7px; }
QTabBar::tab { background:#f4f7fa; padding:9px 18px; border:1px solid #dee5eb; border-bottom:0; }
QTabBar::tab:selected { background:#e0f3ee; color:#117767; font-weight:600; }
QCheckBox { spacing:6px; }
QCheckBox::indicator { width:16px; height:16px; }
QTableWidget { background:#fff; border:1px solid #e2e8ed; border-radius:7px; gridline-color:#eef2f5; outline:0; selection-background-color:#e5f3ee; selection-color:#244538; }
QTableWidget::item { padding:5px; border-bottom:1px solid #eef2f5; }
QHeaderView::section { background:#f4f7fa; color:#728296; border:0; border-bottom:1px solid #e2e8ed; padding:9px; font-weight:600; }
QProgressBar { border:0; background:#e5edf1; border-radius:4px; height:8px; text-align:center; }
QProgressBar::chunk { background:#168878; border-radius:4px; }
QScrollBar:vertical { background:#f7f9fb; width:9px; border-radius:4px; }
QScrollBar::handle:vertical { background:#cbd6df; border-radius:4px; min-height:26px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height:0; }
'''


def label(text, role=None):
    widget = QLabel(text)
    if role:
        widget.setObjectName(role)
    return widget


def card():
    widget = QFrame()
    widget.setObjectName('card')
    layout = QVBoxLayout(widget)
    layout.setContentsMargins(18, 16, 18, 16)
    layout.setSpacing(12)
    return widget, layout


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
            self.state.emit(-1, 'error', '显卡未能初始化在线预览，可打开商城页面查看')
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
                raise ValueError('官方库没有关联的 3D 模型')
            raw = api.get_raw_3d_model_obj(model.uuid)
            if not raw:
                raise ValueError('官方库没有可预览的 OBJ 模型')
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


class ColumnHandle(QSplitterHandle):
    def __init__(self, orientation, parent):
        super().__init__(orientation, parent)
        self.setCursor(Qt.SplitHCursor)
        self.setToolTip('拖动以调整左右区域宽度')

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.fillRect(self.rect(), QColor('#f1f5f8'))
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor('#8ebcb2' if self.underMouse() else '#c5d5df'))
        grip = QRectF((self.width() - 4) / 2, (self.height() - 42) / 2, 4, 42)
        painter.drawRoundedRect(grip, 2, 2)

    def enterEvent(self, event):
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self.update()
        super().leaveEvent(event)


class ColumnSplitter(QSplitter):
    def __init__(self):
        super().__init__(Qt.Horizontal)
        self.setHandleWidth(24)

    def createHandle(self):
        return ColumnHandle(self.orientation(), self)


class MainWindow(QMainWindow):
    batch_done = Signal()
    preview_changed = Signal(str)

    def __init__(self, settings_enabled=True, *, update_source=None):
        super().__init__()
        self.update_source = update_source
        self.setWindowTitle('LCSC3D（本地更新测试）' if update_source is not None else 'LCSC3D')
        self.resize(1240, 850)
        self.setMinimumSize(1060, 740)
        self.setWindowIcon(QIcon(str(ROOT / 'assets' / 'app.ico')))
        self.settings_enabled = settings_enabled
        self.preferences = Preferences()
        self.settings_dialog = None
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
        self.web_state = ('empty', '选择器件后获取官方模型并在本地显示')
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
        self.favorites_dialog = None
        self.account_menu = None
        self._setup_ui()
        self._setup_preview()
        self.setMinimumHeight(max(self.minimumHeight(), self.minimumSizeHint().height()))
        self._restore_settings()
        if self.settings_enabled:
            QTimer.singleShot(0, self.ensure_store)

    def _setup_ui(self):
        canvas = QWidget()
        canvas.setObjectName('canvas')
        self.setCentralWidget(canvas)
        layout = QVBoxLayout(canvas)
        layout.setContentsMargins(24, 20, 24, 16)
        layout.setSpacing(16)

        heading = QHBoxLayout()
        logo = label('')
        logo.setPixmap(QPixmap(str(ROOT / 'assets' / 'app.png')).scaled(48, 48, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        heading.addWidget(logo)
        title_col = QVBoxLayout()
        title_col.setSpacing(3)
        title_col.addWidget(label('LCSC3D', 'title'))
        title_col.addWidget(label('批量下载 3D 模型，导出 AD 符号与封装库', 'muted'))
        heading.addLayout(title_col)
        heading.addStretch()
        heading.addWidget(label('版本 ' + VERSION, 'badge'))
        self.account_button = QPushButton('账号登录')
        self.account_button.setToolTip('登录立创商城账号，支持记住登录')
        self.account_button.clicked.connect(self.open_account)
        heading.addWidget(self.account_button)
        self.update_button = QPushButton('检查更新')
        self.update_button.clicked.connect(self.check_updates)
        heading.addWidget(self.update_button)
        self.settings_button = QPushButton('设置')
        self.settings_button.clicked.connect(self.open_settings)
        heading.addWidget(self.settings_button)
        about = QPushButton('使用说明')
        about.clicked.connect(self.show_help)
        heading.addWidget(about)
        layout.addLayout(heading)

        splitter = ColumnSplitter()
        left_column = QWidget()
        left_layout = QVBoxLayout(left_column)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(14)

        input_card, input_layout = card()
        input_layout.setSpacing(8)
        input_head = QHBoxLayout()
        input_head.addWidget(label('1  输入器件编号', 'section'))
        input_head.addStretch()
        sample = QPushButton('填入示例')
        sample.clicked.connect(lambda: self.input.setPlainText('C2040\nC20197\nC163691'))
        self.sample_button = sample
        input_head.addWidget(sample)
        clear = QPushButton('清空')
        clear.clicked.connect(lambda: self.input.clear())
        self.clear_button = clear
        input_head.addWidget(clear)
        input_layout.addLayout(input_head)
        self.input = QPlainTextEdit()
        self.input.setPlaceholderText('例如：C2040, C20197\n支持换行、空格、中英文逗号分隔；重复编号自动合并')
        self.input.setFixedHeight(76)
        self.input.textChanged.connect(self.input_changed)
        input_layout.addWidget(self.input)
        count_row = QHBoxLayout()
        self.input_info = label('输入立创商城 C 开头的器件编号', 'muted')
        self.input_info.setWordWrap(True)
        count_row.addWidget(self.input_info, 1)
        self.market_button = QPushButton('立创商城')
        self.market_button.setToolTip('搜索商品、查看原图、登录及管理账号收藏')
        self.market_button.clicked.connect(self.open_favorites)
        count_row.addWidget(self.market_button)
        self.queue_button = QPushButton('载入列表')
        self.queue_button.clicked.connect(self.load_queue)
        count_row.addWidget(self.queue_button)
        input_layout.addLayout(count_row)
        left_layout.addWidget(input_card)

        output_card, output_layout = card()
        output_layout.setSpacing(8)
        path_row = QHBoxLayout()
        path_row.addWidget(label('2  保存到', 'section'))
        self.path_input = QLineEdit()
        self.path_input.setPlaceholderText('选择资源保存目录')
        path_row.addWidget(self.path_input, 1)
        self.browse_button = QPushButton('选择文件夹…')
        self.browse_button.clicked.connect(self.choose_folder)
        path_row.addWidget(self.browse_button)
        open_button = QPushButton('打开目录')
        open_button.clicked.connect(self.open_output)
        path_row.addWidget(open_button)
        output_layout.addLayout(path_row)
        option_row = QHBoxLayout()
        option_row.addWidget(label('3D 模型', 'muted'))
        self.step_box = QCheckBox('STEP')
        self.step_box.setChecked(True)
        self.step_box.setToolTip('原始 STEP 文件，适用于 SolidWorks、FreeCAD 等 CAD 软件')
        self.obj_box = QCheckBox('OBJ')
        self.obj_box.setToolTip('下载官方 OBJ 模型文本')
        for widget in (self.step_box, self.obj_box):
            option_row.addWidget(widget)
        option_row.addSpacing(12)
        option_row.addWidget(label('AD 元件库', 'muted'))
        self.schlib_box = QCheckBox('SchLib')
        self.schlib_box.setToolTip('导出原生 AD 符号库，保留引脚编号及封装引用')
        self.pcblib_box = QCheckBox('PcbLib')
        self.pcblib_box.setToolTip('导出原生 AD 封装库，保留焊盘、钻孔与槽孔；不内嵌 3D 模型')
        for widget in (self.schlib_box, self.pcblib_box):
            option_row.addWidget(widget)
        option_row.addStretch()
        self.stop_button = QPushButton('停止')
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self.stop_batch)
        self.start_button = QPushButton('开始下载')
        self.start_button.setObjectName('primary')
        self.start_button.clicked.connect(self.start_batch)
        output_layout.addLayout(option_row)
        left_layout.addWidget(output_card)

        list_card, list_layout = card()
        list_layout.setSpacing(8)
        list_head = QHBoxLayout()
        list_head.addWidget(label('下载列表', 'section'))
        list_head.addStretch()
        self.summary = label('0 个器件', 'muted')
        list_head.addWidget(self.summary)
        list_head.addWidget(self.stop_button)
        list_head.addWidget(self.start_button)
        list_layout.addLayout(list_head)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(['下载', '器件编号', '型号 / 模型', '结果'])
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setShowGrid(False)
        self.table.setWordWrap(False)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(40)
        self.table.setMinimumHeight(112)
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
        self.run_status = label('准备就绪', 'muted')
        self.run_status.setWordWrap(True)
        list_layout.addWidget(self.run_status)
        self.detail = label('选择器件自动预览，并查看结果详情', 'muted')
        self.detail.setWordWrap(True)
        self.detail.setMinimumHeight(26)
        list_layout.addWidget(self.detail)
        list_actions = QHBoxLayout()
        self.select_all_button = QPushButton('全选')
        self.select_all_button.setEnabled(False)
        self.select_all_button.clicked.connect(self.select_all_downloads)
        list_actions.addWidget(self.select_all_button)
        self.invert_selection_button = QPushButton('反选')
        self.invert_selection_button.setEnabled(False)
        self.invert_selection_button.clicked.connect(self.invert_download_selection)
        list_actions.addWidget(self.invert_selection_button)
        self.remove_checked_button = QPushButton('删除已勾选器件')
        self.remove_checked_button.setEnabled(False)
        self.remove_checked_button.setToolTip('从下载列表及输入框移除勾选器件，保留已下载文件')
        self.remove_checked_button.clicked.connect(self.remove_checked_downloads)
        list_actions.addWidget(self.remove_checked_button)
        self.selection_summary = label('已勾选 0 / 0', 'muted')
        list_actions.addWidget(self.selection_summary)
        list_actions.addStretch()
        self.part_folder_button = QPushButton('打开器件目录')
        self.part_folder_button.clicked.connect(self.open_part_folder)
        self.part_folder_button.setEnabled(False)
        list_actions.addWidget(self.part_folder_button)
        list_layout.addLayout(list_actions)
        left_layout.addWidget(list_card, 1)
        splitter.addWidget(left_column)

        preview_card, preview_layout = card()
        preview_head = QHBoxLayout()
        preview_head.addWidget(label('器件预览', 'section'))
        preview_head.addStretch()
        preview_layout.addLayout(preview_head)
        preview_modes = QHBoxLayout()
        self.preview_mode_group = QButtonGroup(self)
        self.preview_mode_buttons = {}
        for mode, text in (('3d', '3D 模型'), ('symbol', '符号'), ('footprint', '封装')):
            button = QPushButton(text)
            button.setObjectName('previewMode')
            button.setCheckable(True)
            button.setChecked(mode == self.preview_mode)
            button.clicked.connect(lambda checked=False, choice=mode: self.set_preview_mode(choice))
            self.preview_mode_group.addButton(button)
            self.preview_mode_buttons[mode] = button
            preview_modes.addWidget(button)
        preview_modes.addStretch()
        self.symbol_unit_box = QComboBox()
        self.symbol_unit_box.setToolTip('选择符号单元')
        self.symbol_unit_box.currentIndexChanged.connect(self.show_symbol_unit)
        self.symbol_unit_box.hide()
        preview_modes.addWidget(self.symbol_unit_box)
        preview_layout.addLayout(preview_modes)
        self.preview_caption = label('选择左侧列表中的器件', 'muted')
        self.preview_caption.setWordWrap(True)
        preview_layout.addWidget(self.preview_caption)
        self.preview_stack = QStackedWidget()
        empty = QFrame()
        empty.setStyleSheet('QFrame { background:#f3f6fa; border-radius:8px; }')
        empty_layout = QVBoxLayout(empty)
        empty_layout.addStretch()
        cube = label('◇')
        cube.setAlignment(Qt.AlignCenter)
        cube.setStyleSheet('font-size:64px;color:#bccbd5;')
        empty_layout.addWidget(cube)
        hint = label('载入列表后自动预览所选器件\n鼠标拖动旋转，滚轮缩放')
        hint.setAlignment(Qt.AlignCenter)
        hint.setStyleSheet('color:#7a8d9d;line-height:1.8;')
        empty_layout.addWidget(hint)
        empty_layout.addStretch()
        self.preview_stack.addWidget(empty)
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
        self.preview_legend = label('<span style="color:#ff0000">■ 顶层焊盘</span>　'
                                    '<span style="color:#0000ff">■ 底层焊盘</span>　'
                                    '<span style="color:#cc9900">■ 丝印</span>　'
                                    '<span style="color:#c0c0c0">■ 多层</span>', 'muted')
        self.preview_legend.setWordWrap(True)
        self.preview_legend.hide()
        preview_layout.addWidget(self.preview_legend)
        self.preview_status = label('选择器件后获取官方模型并在本地显示', 'muted')
        self.preview_status.setWordWrap(True)
        preview_layout.addWidget(self.preview_status)
        preview_actions = QHBoxLayout()
        self.reload_button = QPushButton('重新加载')
        self.reload_button.setEnabled(False)
        self.reload_button.clicked.connect(lambda: self.show_preview(self.current_preview, reload=True))
        preview_actions.addWidget(self.reload_button)
        self.fit_button = QPushButton('适应窗口')
        self.fit_button.clicked.connect(self.fit_preview)
        self.fit_button.hide()
        preview_actions.addWidget(self.fit_button)
        preview_actions.addStretch()
        self.store_button = QPushButton('打开商城页面')
        self.store_button.setEnabled(False)
        self.store_button.clicked.connect(self.open_store)
        preview_actions.addWidget(self.store_button)
        preview_layout.addLayout(preview_actions)
        splitter.addWidget(preview_card)
        splitter.setSizes([670, 470])
        splitter.setChildrenCollapsible(False)
        layout.addWidget(splitter, 1)

        footer = QHBoxLayout()
        footer.addWidget(label('官方资源下载 · AGPL-3.0', 'muted'))
        footer.addStretch()
        source = label('数据来源：<a style="color:#638397" href="https://lceda.cn/">JLCEDA</a> / <a style="color:#638397" href="https://easyeda.com/">EasyEDA 官方库</a>', 'muted')
        source.setOpenExternalLinks(True)
        footer.addWidget(source)
        layout.addLayout(footer)

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
        # Migrate removed library/WRL-only selections to the default model format.
        if not any(box.isChecked() for box in (self.step_box, self.obj_box, self.schlib_box, self.pcblib_box)):
            self.step_box.setChecked(True)
        self.preferences = Preferences.from_mapping(settings)
        set_preferences(self.preferences)
        set_log_level(self.preferences.log_level)

    def save_settings(self, preferences=None):
        if not self.settings_enabled:
            return True
        preferences = preferences or self.preferences
        try:
            write_settings(SETTINGS_PATH, {'destination': self.path_input.text(), 'step': self.step_box.isChecked(),
                'obj': self.obj_box.isChecked(), 'schlib': self.schlib_box.isChecked(),
                'pcblib': self.pcblib_box.isChecked(), **preferences.to_mapping()})
            return True
        except OSError as exc:
            record_error(exc, 'settings.save_failed', level='WARNING')
            return False

    @traced('settings.apply')
    def apply_preferences(self, preferences):
        if not self.save_settings(preferences):
            return False
        self.preferences = preferences
        set_preferences(preferences)
        set_log_level(preferences.log_level)
        log_event('INFO', 'settings.saved', **preferences.to_mapping())
        return True

    @traced('settings.open')
    def open_settings(self):
        if self.settings_dialog is not None and self.settings_dialog.isVisible():
            self.settings_dialog.raise_()
            self.settings_dialog.activateWindow()
            return
        if self.settings_dialog is not None:
            self.settings_dialog.deleteLater()
        self.settings_dialog = SettingsDialog(self.preferences, self.apply_preferences, self)
        self.settings_dialog.show()

    def input_changed(self):
        ids, invalid, duplicates = parse_part_numbers(self.input.toPlainText())
        text = f'已识别 {len(ids)} 个器件'
        if duplicates:
            text += f' · 合并 {duplicates} 个重复编号'
        if invalid:
            text += ' · 请修正：' + ', '.join(invalid[:4]) + ('…' if len(invalid) > 4 else '')
        self.input_info.setText(text)

    @traced('queue.load', level='INFO')
    def load_queue(self):
        if self.batch_running:
            return False
        ids, invalid, duplicates = parse_part_numbers(self.input.toPlainText())
        if invalid:
            log_event('WARNING', 'queue.input_rejected', reason='invalid_part_numbers', count=len(invalid))
            self.run_status.setText('请先修正输入中无法识别的内容：' + ', '.join(invalid[:6]))
            return False
        if not ids:
            log_event('DEBUG', 'queue.input_rejected', reason='empty')
            self.run_status.setText('请先输入 C 开头的立创器件编号')
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
                check.setToolTip('勾选后下载此器件；单击器件行可预览')
                self.table.setItem(row, DOWNLOAD_COLUMN, check)
                self.table.setItem(row, PART_COLUMN, QTableWidgetItem(part))
                info = self.component_info.get(part, {})
                model_item = QTableWidgetItem(info.get('title') or info.get('model') or '查询中…')
                model_item.setToolTip(self.component_tooltip(info))
                self.table.setItem(row, MODEL_COLUMN, model_item)
                self.table.setItem(row, RESULT_COLUMN, QTableWidgetItem('等待下载'))
        finally:
            self.table.blockSignals(False)
        self.summary.setText(f'{len(ids)} 个器件')
        log_event('INFO', 'queue.loaded', count=len(ids), duplicate_count=duplicates)
        self.download_selection_changed()
        self.progress_bar.setRange(0, len(ids))
        self.progress_bar.setValue(0)
        self.run_status.setText('列表已载入，请勾选需要下载的器件；单击器件行自动预览')
        self.table.selectRow(0)
        self.request_component_info()
        return True

    def bind_store(self, dialog):
        self.favorites_dialog = dialog
        dialog.import_requested.connect(self.import_favorites)
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
        dialog = self.favorites_dialog
        account = dialog.client.account if dialog else None
        restoring = bool(dialog and dialog.restoring)
        self.account_button.setEnabled(not restoring)
        name = account['name'] if account else ''
        self.account_button.setText('恢复登录…' if restoring else '账号：' + name[:12] if account else '账号登录')
        self.account_button.setToolTip('已登录：' + name + '，点击管理登录状态' if account else '登录立创商城账号，支持记住登录')
        if self.account_menu:
            self.account_menu.close()

    @traced('login.open')
    def open_account(self):
        dialog = self.ensure_store()
        if dialog.client.account:
            self.account_menu = QMenu(self)
            self.account_menu.addAction('已登录：' + dialog.client.account['name']).setEnabled(False)
            self.account_menu.addAction('退出登录', dialog.clear_session)
            self.account_menu.popup(self.account_button.mapToGlobal(self.account_button.rect().bottomLeft()))
        else:
            dialog.open_login()

    def preview_store_product(self, part):
        self.show_preview(part)
        if self.isMinimized():
            self.setWindowState(self.windowState() & ~Qt.WindowMinimized)
        self.show()
        self.raise_()
        self.activateWindow()
        if sys.platform == 'win32':
            # Preview is an explicit user action in another window of this app.
            # Ask Windows to activate the restored native main window as well.
            import ctypes
            activate = ctypes.windll.user32.SetForegroundWindow
            activate.argtypes, activate.restype = [ctypes.c_void_p], ctypes.c_int
            activate(int(self.winId()))

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
        if self.close_when_finished:
            self.close()

    @traced('queue.import', lambda self, items: {'count': len(items)}, level='INFO')
    def import_favorites(self, items):
        """Append catalog/favorite products and preserve existing queue state."""
        def status(message):
            self.run_status.setText(message)
            if self.favorites_dialog:
                self.favorites_dialog.status.setText(message)

        if self.batch_running or self.close_when_finished:
            status('请等待当前下载任务完成后再导入元件。')
            return 0
        items = normalize_items(items)
        if not items:
            status('没有可导入的有效 C 编号。')
            return 0
        pending, invalid, _ = parse_part_numbers(self.input.toPlainText())
        if invalid:
            status('请先修正主窗口输入框中的无效内容，再导入元件：' + ', '.join(invalid[:4]))
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
                check.setToolTip('勾选后下载此器件；单击器件行可预览')
                self.table.setItem(row, DOWNLOAD_COLUMN, check)
                self.table.setItem(row, PART_COLUMN, QTableWidgetItem(part))
                if part in titles:
                    self.component_info[part] = {'title': titles[part]}
                self.table.setItem(row, MODEL_COLUMN, QTableWidgetItem(titles.get(part) or '查询中…'))
                self.table.setItem(row, RESULT_COLUMN, QTableWidgetItem('等待下载'))
        finally:
            self.table.blockSignals(False)
        self.ids = merged
        self.info_rows = {part: row for row, part in enumerate(self.ids)}
        self.input.setPlainText('\n'.join(self.ids))
        self.summary.setText(f'{len(self.ids)} 个器件')
        self.download_selection_changed()
        if new_parts:
            self.info_revision += 1
            self.request_component_info()
            if self.table.currentRow() < 0:
                self.table.selectRow(0)
        added = sum(item['part'] not in existing for item in items)
        status(f'元件导入完成：新增 {added} 个，跳过 {len(items) - added} 个已有元件；下载列表共 {len(self.ids)} 个。')
        return added

    @staticmethod
    def component_tooltip(info):
        return '\n'.join(f'{label}：{info[key]}' for key, label in (('title', '型号'), ('model', '模型')) if info.get(key))

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
                item.setText('查询失败')
                item.setToolTip(error + '\n点击「载入列表」重试；仍可勾选下载')
            return
        display_info = {'title': result.title or info.get('title', ''),
                        'model': result.model or info.get('model', '')} if result else info
        self.component_info[part] = display_info
        item.setText(display_info.get('title') or display_info.get('model') or '未提供型号')
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
        self.selection_summary.setText(f'已勾选 {len(self.checked_rows())} / {len(self.ids)}')
        enabled = bool(self.ids) and not self.batch_running
        self.select_all_button.setEnabled(enabled)
        self.invert_selection_button.setEnabled(enabled)
        self.remove_checked_button.setEnabled(enabled and bool(self.checked_rows()) and not self.close_when_finished)

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
            self.detail.setText('选择器件自动预览，并查看结果详情')
            self.detail.setToolTip('')
            self.part_folder_button.setEnabled(False)
        self.summary.setText(f'{len(self.ids)} 个器件')
        self.progress_bar.setRange(0, max(1, len(self.ids)))
        self.progress_bar.setValue(sum(part in self.results for part in self.ids))
        self.download_selection_changed()
        self.run_status.setText(f'已从下载列表删除 {len(removed)} 个器件，剩余 {len(self.ids)} 个。已下载文件保留。')
        log_event('INFO', 'queue.deleted', count=len(removed), remaining=len(self.ids))
        if self.ids:
            self.request_component_info()
        return len(removed)

    def clear_preview(self):
        self.current_preview = self.current_3d = ''
        self.web_revision += 1
        self.model_pending = self.library_pending = None
        for worker in (self.model_worker, self.library_worker):
            if worker:
                worker.cancelled.set()
        self.web_model, self.web_error = None, ''
        self.web_state = ('empty', '选择器件后加载预览')
        self.preview_stack.setCurrentWidget(self.preview_empty)
        self.preview_caption.setText('选择左侧列表中的器件')
        self.preview_hint.setText('载入列表后自动预览所选器件\n鼠标拖动旋转，滚轮缩放')
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
        folder = QFileDialog.getExistingDirectory(self, '选择模型保存目录', self.path_input.text())
        if folder:
            self.path_input.setText(folder)
            self.save_settings()
            log_event('INFO', 'settings.export_folder_changed')
        else:
            log_event('DEBUG', 'settings.export_folder_cancelled')

    def open_output(self):
        path = Path(self.path_input.text().strip())
        if path.is_dir():
            opened = QDesktopServices.openUrl(QUrl.fromLocalFile(str(path.resolve())))
            log_event('INFO' if opened else 'WARNING', 'navigation.output_folder', opened=opened)
        else:
            log_event('WARNING', 'navigation.output_folder', opened=False, reason='not_created')
            self.run_status.setText('目录尚未创建，开始下载时会自动创建')

    def set_running(self, running):
        self.batch_running = running
        if self.favorites_dialog:
            self.favorites_dialog.set_import_enabled(not running)
        for widget in (self.input, self.sample_button, self.clear_button, self.path_input, self.browse_button, self.queue_button, self.start_button, self.step_box, self.obj_box, self.schlib_box, self.pcblib_box):
            widget.setEnabled(not running)
        self.stop_button.setEnabled(running)
        self.table.blockSignals(True)
        try:
            for row in range(len(self.ids)):
                item = self.table.item(row, DOWNLOAD_COLUMN)
                item.setFlags(item.flags() & ~Qt.ItemIsEnabled if running else item.flags() | Qt.ItemIsEnabled)
        finally:
            self.table.blockSignals(False)
        self.download_selection_changed()

    def start_batch(self):
        if self.batch_running or self.worker and self.worker.isRunning():
            return
        formats = tuple(name for name, box in [('STEP', self.step_box), ('OBJ', self.obj_box),
                        ('SCHLIB', self.schlib_box), ('PCBLIB', self.pcblib_box)] if box.isChecked())
        if not formats:
            self.run_status.setText('请至少选择一种模型或 AD 元件库格式')
            return
        if not self.path_input.text().strip():
            self.run_status.setText('请选择保存目录')
            return
        ids, invalid, _ = parse_part_numbers(self.input.toPlainText())
        if invalid or not ids or ids != self.ids:
            if not self.load_queue():
                return
        rows = self.checked_rows()
        if not rows:
            self.run_status.setText('请至少勾选一个要下载的器件')
            return
        destination = Path(self.path_input.text().strip()).expanduser().resolve()
        try:
            destination.mkdir(parents=True, exist_ok=True)
            if not os.access(destination, os.W_OK):
                raise PermissionError('下载目录不可写')
        except OSError as exc:
            record_error(exc, 'download.destination_unwritable')
            self.run_status.setText('保存目录不可写：' + str(exc))
            return
        self.batch_ids = [self.ids[row] for row in rows]
        for row in rows:
            self.results.pop(self.ids[row], None)
            item = self.table.item(row, RESULT_COLUMN)
            item.setText('等待下载')
            item.setToolTip('')
            item.setForeground(QColor('#23354a'))
        self.progress_bar.setRange(0, len(rows))
        self.progress_bar.setValue(0)
        self.summary.setText(f'{len(self.ids)} 个器件')
        self.path_input.setText(str(destination))
        self.save_settings()
        self.set_running(True)
        self.run_status.setText(f'开始下载，共勾选 {len(rows)} 个器件…')
        worker = BatchWorker(self.batch_ids[:], Options(destination, formats), self, rows=rows)
        self.worker = worker
        worker.phase.connect(self.update_phase)
        worker.result.connect(self.update_result)
        worker.completed.connect(self.complete_batch)
        worker.finished.connect(self.worker_finished)
        worker.start()

    def update_phase(self, row, status, title):
        if self.ids[row] in self.results:
            return
        self.table.item(row, RESULT_COLUMN).setText(status)
        if title:
            self.table.item(row, MODEL_COLUMN).setText(title)
        batch_ids = self.batch_ids or self.ids
        completed = sum(part in self.results for part in batch_ids)
        self.run_status.setText(f'已完成 {completed} / {len(batch_ids)} · {self.ids[row]} · {status}')

    def update_result(self, row, result):
        self.results[result.part] = result
        item = self.table.item(row, RESULT_COLUMN)
        item.setText(result.status)
        item.setToolTip(result.message)
        colors = {'成功': '#168878', '部分完成': '#b47718', '失败': '#c15353', '无模型': '#b47718', '已取消': '#8695a3'}
        item.setForeground(QColor(colors.get(result.status, '#23354a')))
        info = self.component_info.get(result.part, {})
        title = result.title or info.get('title', '')
        model = result.model or info.get('model', '')
        model_item = self.table.item(row, MODEL_COLUMN)
        if title or model:
            self.component_info[result.part] = {'title': title, 'model': model}
            model_item.setText(title or model)
            model_item.setToolTip(self.component_tooltip({'title': title, 'model': model}))
        self.progress_bar.setValue(sum(part in self.results for part in (self.batch_ids or self.ids)))
        self.selection_changed()

    def complete_batch(self, results):
        ok = sum(r.status == '成功' for r in results)
        partial = sum(r.status == '部分完成' for r in results)
        failed = sum(r.status in ('失败', '无模型') for r in results)
        cancelled = sum(r.status == '已取消' for r in results)
        self.run_status.setText(f'完成 · 成功 {ok} · 部分完成 {partial} · 失败或无模型 {failed} · 取消 {cancelled}')
        self.summary.setText(f'{ok} / {len(results)} 成功')
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
            self.run_status.setText('正在停止，等待当前网络请求结束…')

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
            self.detail.setText(part + ' · ' + result.message)
            self.detail.setToolTip(result.message)
        elif part:
            self.detail.setText(part + ' · 右侧自动预览，可切换 3D 模型、符号和封装')
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
            self.preview_hint.setText('选择左侧列表中的器件\n即可查看' + {'3d': '3D 模型', 'symbol': '符号', 'footprint': '封装'}[mode])
            self.preview_stack.setCurrentWidget(self.preview_empty)
            self.on_preview_state('empty', '选择器件后加载预览')

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
        if self.preview_mode != '3d':
            self.show_library_preview(part, reload)
            return
        self.library_pending = None
        self.symbol_unit_box.hide()
        self.preview_caption.setText(part + ' · 3D 模型')
        self.preview_stack.setCurrentWidget(self.web)
        if part == self.current_3d and self.viewer_started and not reload:
            self.fit_button.setEnabled(self.web_state[0] == 'ready')
            self.on_preview_state(*self.web_state)
            return
        self.current_3d = part
        self.web_revision += 1
        self.web_model = None
        self.web_error = ''
        self.web_state = ('loading', '正在获取官方 3D 模型…')
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
        self.web.page().runJavaScript(f'window.loadLcscPart && window.loadLcscPart({args})')
        if self.web_model is not None:
            payload = json.dumps(self.web_model, separators=(',', ':'))
            self.web.page().runJavaScript(f'window.showMesh && window.showMesh({payload},{args})')
        elif self.web_error:
            message = json.dumps(self.web_error)
            self.web.page().runJavaScript(f'window.modelError && window.modelError({args},{message})')

    def on_viewer_page_loaded(self, ok):
        self.viewer_ready = ok
        if ok and self.viewer_started:
            self.update_viewer_part()
        elif self.viewer_started:
            self.on_web_preview_state(self.web_revision, 'error', '3D 画布加载失败，请重新加载')

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
        self.web.page().runJavaScript(f'window.modelError && window.modelError({args})')

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
        self.on_web_preview_state(self.web_revision, 'error', '3D 查看器已停止，请重新加载')

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
        self.preview_caption.setText(part + ' · ' + ('符号' if self.preview_mode == 'symbol' else '封装'))
        self.symbol_unit_box.hide()
        self.preview_hint.setText('正在加载器件预览…')
        self.preview_stack.setCurrentWidget(self.preview_empty)
        self.fit_button.setEnabled(False)
        self.on_preview_state('loading', '正在获取商城官方符号和封装 SVG…')
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
        if self.preview_mode != '3d' and part == self.current_preview:
            self.display_library_preview(part, preview)

    def library_preview_failed(self, part, message):
        if self.library_worker and self.library_worker.cancelled.is_set():
            return
        if self.preview_mode != '3d' and part == self.current_preview:
            self.preview_hint.setText('预览加载失败\n可点击「重新加载」重试')
            self.on_preview_state('error', message)

    def library_preview_finished(self):
        worker, self.library_worker = self.library_worker, None
        if worker:
            worker.deleteLater()
        pending, self.library_pending = self.library_pending, None
        if self.close_when_finished:
            self.close()
        elif pending and self.preview_mode != '3d' and pending == self.current_preview:
            if pending in self.library_cache:
                self.display_library_preview(pending, self.library_cache[pending])
            else:
                self.start_library_preview(pending)

    def display_library_preview(self, part, preview):
        if self.preview_mode == 'symbol':
            if self.symbol_unit_part != part or self.symbol_unit_box.count() != len(preview.symbols):
                self.symbol_unit_box.blockSignals(True)
                self.symbol_unit_box.clear()
                self.symbol_unit_box.addItems([f'单元 {index + 1}' for index in range(len(preview.symbols))])
                self.symbol_unit_box.blockSignals(False)
                self.symbol_unit_part = part
            self.symbol_unit_box.setVisible(len(preview.symbols) > 1)
            document = preview.symbols[max(0, self.symbol_unit_box.currentIndex())]
            view, label = self.symbol_view, '符号'
        else:
            self.symbol_unit_box.hide()
            document = preview.footprint
            view, label = self.footprint_view, '封装'
        self.preview_caption.setText(part + ' · ' + label + (' · ' + document.name if document.name else ''))
        with log_context(getattr(self, 'preview_diagnostic_context', {})):
            displayed = not document.error and view.show_document(document)
        if not displayed:
            self.fit_button.setEnabled(False)
            self.preview_hint.setText(document.error or '此器件预览暂不可用')
            self.preview_stack.setCurrentWidget(self.preview_empty)
            self.on_preview_state('error', document.error or '预览图形无法显示')
            return
        self.preview_stack.setCurrentWidget(view)
        self.on_vector_preview_state(view, *view.load_state)

    def on_vector_preview_state(self, view, status, message):
        if self.preview_mode == '3d' or self.preview_stack.currentWidget() is not view:
            return
        self.fit_button.setEnabled(status == 'ready')
        if status == 'ready':
            label, unit = ('符号', '引脚') if self.preview_mode == 'symbol' else ('封装', '焊盘')
            message = f'商城官方 SVG · {label} · {view.document.count} 个{unit} · 滚轮缩放，拖动平移'
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

    def on_preview_state(self, status, message):
        with log_context(getattr(self, 'preview_diagnostic_context', {})):
            log_event('ERROR' if status == 'error' else 'DEBUG', 'preview.display_state',
                      viewer=self.preview_mode, part=safe_part(self.current_preview),
                      state=status if status in ('empty', 'loading', 'ready', 'error') else 'unknown',
                      revision=self.web_revision)
        self.preview_state = status
        self.preview_status.setText(message)
        self.preview_status.setStyleSheet('color:#b45745;' if status == 'error' else 'color:#168878;' if status == 'ready' else 'color:#788898;')
        self.preview_changed.emit(status)

    @traced('navigation.store')
    def open_store(self):
        part = self.current_preview or self.selected_part()
        if part:
            result = self.results.get(part)
            url = result.store_url if result else f'https://so.szlcsc.com/global.html?k={part}'
            opened = QDesktopServices.openUrl(QUrl(url))
            log_event('INFO' if opened else 'WARNING', 'navigation.store_result', opened=opened, part=safe_part(part))

    @traced('navigation.export_folder')
    def open_part_folder(self):
        result = self.results.get(self.selected_part())
        if result and result.folder:
            opened = QDesktopServices.openUrl(QUrl.fromLocalFile(result.folder))
            log_event('INFO' if opened else 'WARNING', 'navigation.export_folder_result', opened=opened)

    def show_help(self):
        message = QMessageBox(self)
        message.setWindowTitle('使用说明与来源')
        message.setTextFormat(Qt.RichText)
        message.setText('<b>LCSC3D</b><br>版本：' + VERSION + '<br><br>'
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
            '商城元件默认不勾选；预览当前高亮行会将主窗口置于前台，商城窗口保持打开。'
            '默认记住登录，重启后自动恢复；取消勾选时只保留本次登录。「退出登录」可清除已保存会话。<br><br>'
            '顶部「设置」可分别选择商城与检查更新是否使用系统代理，并调整日志等级；默认 Debug。「打开日志」可打开日志目录。<br><br>'
            '可保存官方 STEP、OBJ，并导出原生 AD SchLib 符号库、PcbLib 封装库；不导出 JSON 或 SVG。<br>'
            'AD 库保留引脚、焊盘和孔数据，遇到不支持的图元会提示失败；PcbLib 不内嵌 3D 模型。<br>'
            '每个器件单独保存到“器件名_编号”目录，AD 库文件按元件型号命名；下载时覆盖已有同名文件。<br>'
            '3D 预览由 LCSC3D 在本地渲染官方模型；拖动旋转，滚轮缩放，右键拖动平移。<br><br>'
            '右侧上方可切换「3D 模型 / 符号 / 封装」，无需先下载。<br>'
            '符号和封装直接加载商城使用的官方 SVG，支持滚轮缩放、拖动平移及「适应窗口」；多单元符号可选择单元。<br><br>'
            '启动后自动后台检查新版，发现新版时提醒；连接失败或已是最新版时不弹窗。'
            '也可点击顶部「检查更新」手动查询；便携 EXE 支持下载、SHA-256 校验并重启更新。<br>'
            '模型来源：<a href="https://lceda.cn/">JLCEDA</a> / <a href="https://easyeda.com/">EasyEDA 官方库</a>。<br>'
            '资源下载与本地 3D 预览由本项目实现，软件采用 AGPL-3.0-or-later。<br>'
            '对应源码、构建脚本与第三方说明随交付提供。')
        message.exec()

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
        self.update_dialog = UpdateDialog(VERSION, self, source=self.update_source, release=result['release'])
        self.update_dialog.show()
        log_event('INFO', 'update.startup_notification_shown', target_version=result['release'].version)

    def check_updates(self):
        self.cancel_startup_update()
        if self.update_dialog is not None and self.update_dialog.isVisible():
            self.update_dialog.raise_()
            self.update_dialog.activateWindow()
            return
        self.update_dialog = UpdateDialog(VERSION, self, source=self.update_source)
        self.update_dialog.show()

    def begin_update(self, manifest):
        self.save_settings()
        launch_update(manifest)
        self.close_when_finished = True
        QTimer.singleShot(0, self.close)

    def closeEvent(self, event):
        self.cancel_startup_update()
        if self.favorites_dialog:
            self.favorites_dialog.shutdown()
            self.favorites_dialog.close()
        store_running = self.favorites_dialog and self.favorites_dialog.has_jobs()
        update_worker = self.update_dialog.worker if self.update_dialog else None
        log_worker = self.settings_dialog.worker if self.settings_dialog else None
        if store_running or update_worker is not None or log_worker is not None or self.model_worker is not None or self.library_worker is not None or self.info_worker is not None or self.worker and self.worker.isRunning():
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
            self.run_status.setText('正在关闭，等待后台任务结束…')
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
    parser.add_argument('--self-test-store', '--self-test-favorites', dest='self_test_favorites', metavar='FOLDER', help=argparse.SUPPRESS)
    parser.add_argument('--self-test-store-live', metavar='FOLDER', help=argparse.SUPPRESS)
    parser.add_argument('--self-test-settings', metavar='FOLDER', help=argparse.SUPPRESS)
    parser.add_argument('--capture-docs', metavar='FOLDER', help=argparse.SUPPRESS)
    parser.add_argument('--update-ack', metavar='PLAN', help=argparse.SUPPRESS)
    parser.add_argument('--local-update-source', metavar='URL', help='仅测试：本机 HTTP 更新源（http://127.0.0.1:端口）')
    parser.add_argument('--local-update-current-version', metavar='VERSION', help='仅测试：模拟版本比较的当前版本')
    parser.add_argument('--self-test-local-update', metavar='FOLDER', help=argparse.SUPPRESS)
    parser.add_argument('--self-test-startup-update', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    from updater import LocalUpdateSource, UpdateError
    if (args.local_update_current_version or args.self_test_local_update) and not args.local_update_source:
        parser.error('本地更新测试参数必须同时提供 --local-update-source')
    if args.self_test_startup_update and not args.self_test_local_update:
        parser.error('--self-test-startup-update requires --self-test-local-update')
    try:
        update_source = LocalUpdateSource(args.local_update_source, args.local_update_current_version) if args.local_update_source else None
    except UpdateError as exc:
        parser.error(str(exc))
    ad_test_parts, ad_invalid_parts, _ = parse_part_numbers(args.self_test_ad_parts)
    if args.self_test_ad and (not ad_test_parts or ad_invalid_parts):
        parser.error('--self-test-ad-parts requires valid C numbers')
    QApplication.setAttribute(Qt.AA_ShareOpenGLContexts)
    app = QApplication([sys.argv[0]])
    app.setApplicationName('LCSC3D')
    app.setApplicationVersion(VERSION)
    app.setStyle('Fusion')
    app.setFont(QFont('Microsoft YaHei UI', 9))
    app.setStyleSheet(STYLES)
    logging.getLogger().setLevel(logging.ERROR)
    test_destination = next((value for value in (args.self_test, args.self_test_ad, args.self_test_favorites,
                                                  args.self_test_store_live, args.self_test_settings, args.capture_docs) if value), None)
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
        window.path_input.setText(str(destination / 'AD 元件库'))
        window.step_box.setChecked(False)
        window.obj_box.setChecked(False)
        window.schlib_box.setChecked(True)
        window.pcblib_box.setChecked(True)
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
                  and all(result.status == '成功'
                          and {Path(file).suffix for file in result.files} == {'.SchLib', '.PcbLib'}
                          for result in results))
            window.grab().save(str(destination / 'AD导出界面.png'))
            (destination / 'verification.json').write_text(json.dumps({
                'version': VERSION, 'frozen': bool(getattr(sys, 'frozen', False)), 'success': ok,
                'parts': ad_test_parts,
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
        window.path_input.setText(str(destination / '批量下载测试'))
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
            window.grab().save(str(destination / '软件界面.png'))
            report = {
                'application_name': app.applicationName(), 'window_title': window.windowTitle(),
                'automatic_preview': not any(button.text() == '在线预览' for button in window.findChildren(QPushButton))
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
                          ('', '—', '查询中…', '查询失败', '未提供型号')
                      and report['download_selection'] == {
                          'checked_ids': ['C2040', 'C20197', 'C999999999999'],
                          'unchecked_ids': ['C163691'], 'selected_only': True}
                      and sum(result.status == '成功' for result in window.results.values()) == 2
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
            name = ('符号' if mode == 'symbol' else '封装') + '_'+ window.ids[row] + '.png'
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
            window.grab().save(str(destination / '型号查询.png'))
            window.start_batch()

        QTimer.singleShot(800, start_download_check)
        QTimer.singleShot(110000, lambda: finish_if_ready(True))
    return app.exec()


if __name__ == '__main__':
    sys.exit(main())
