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

from PySide6.QtCore import Qt, QThread, QTimer, QUrl, Signal, QStandardPaths, QRectF
from PySide6.QtGui import QColor, QDesktopServices, QFont, QIcon, QPixmap, QPainter
from PySide6.QtWidgets import (
    QApplication, QButtonGroup, QCheckBox, QComboBox, QFileDialog, QFrame, QHBoxLayout, QHeaderView,
    QLabel, QLineEdit, QMainWindow, QMessageBox, QPlainTextEdit, QProgressBar,
    QPushButton, QSizePolicy, QSplitter, QSplitterHandle, QStackedWidget, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget, QAbstractItemView,
)
from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile, QWebEngineSettings
from PySide6.QtWebEngineWidgets import QWebEngineView

from backend import Cancelled, NetworkApi, Options, Result, download_batch, get_component_metadata, parse_part_numbers
from library_preview import build_library_preview, VectorPreviewView, SYMBOL_BACKGROUND, FOOTPRINT_BACKGROUND

VERSION = '1.6.1'
DOWNLOAD_COLUMN, PART_COLUMN, MODEL_COLUMN, RESULT_COLUMN = range(4)
ROOT = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent))
APP_DIR = Path(sys.executable).parent if getattr(sys, 'frozen', False) else Path(__file__).resolve().parent
SETTINGS_PATH = APP_DIR / 'LCSC3D-settings.json'

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

    def run(self):
        results = download_batch(self.ids, self.options, self.cancelled,
                                 lambda index, status, title: self.phase.emit(self.rows[index], status, title),
                                 lambda index, result: self.result.emit(self.rows[index], result),
                                 api=NetworkApi(self.cancelled, use_cache=not self.options.overwrite))
        self.completed.emit(results)


class ComponentInfoWorker(QThread):
    loaded = Signal(int, str, object, str)

    def __init__(self, ids, revision, parent=None):
        super().__init__(parent)
        self.ids, self.revision = ids, revision
        self.cancelled = threading.Event()

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
                        self.loaded.emit(self.revision, part, {}, str(exc))

        with ThreadPoolExecutor(max_workers=3, thread_name_prefix='component-info') as pool:
            tasks = [pool.submit(query_parts) for _ in range(min(3, len(self.ids)))]
            for task in tasks:
                task.result()


class PreviewPage(QWebEnginePage):
    state = Signal(int, str, str)

    def javaScriptConsoleMessage(self, level, message, line, source):
        if message.startswith('LCSC3D_STATE:'):
            try:
                info = json.loads(message[len('LCSC3D_STATE:'):])
                self.state.emit(int(info['token']), info['status'], info['message'])
            except (ValueError, KeyError):
                pass
        elif 'faild to initial webgl' in message.lower():
            self.state.emit(-1, 'error', '显卡未能初始化在线预览，可打开商城页面查看')


class LibraryPreviewWorker(QThread):
    loaded = Signal(str, object)
    failed = Signal(str, str)

    def __init__(self, part, parent=None):
        super().__init__(parent)
        self.part = part
        self.cancelled = threading.Event()

    def run(self):
        try:
            api = NetworkApi(self.cancelled)
            data = api.get_svg_data_of_component(self.part)
            api.check_cancelled()
            preview = build_library_preview(data, self.part)
            api.check_cancelled()
            self.loaded.emit(self.part, preview)
        except Cancelled:
            pass
        except Exception as exc:
            self.failed.emit(self.part, str(exc))


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

    def __init__(self, settings_enabled=True):
        super().__init__()
        self.setWindowTitle('LCSC3D')
        self.resize(1240, 850)
        self.setMinimumSize(1060, 740)
        self.setWindowIcon(QIcon(str(ROOT / 'assets' / 'app.ico')))
        self.settings_enabled = settings_enabled
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
        self.web_state = ('empty', '预览需要联网，模型由商城官方查看器加载')
        self.library_cache = OrderedDict()
        self.library_worker = None
        self.library_pending = None
        self.symbol_unit_part = ''
        self.web = None
        self.web_profile = QWebEngineProfile(self)
        self.web_profile.setHttpCacheMaximumSize(8 * 1024 * 1024)
        self.close_when_finished = False
        self._setup_ui()
        self._setup_preview()
        self.setMinimumHeight(max(self.minimumHeight(), self.minimumSizeHint().height()))
        self._restore_settings()

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
        title_col.addWidget(label('批量下载 3D 模型并在线预览', 'muted'))
        heading.addLayout(title_col)
        heading.addStretch()
        heading.addWidget(label('版本 ' + VERSION, 'badge'))
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
        self.path_input.setPlaceholderText('选择模型与元件库保存目录')
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
        self.wrl_box = QCheckBox('WRL')
        self.wrl_box.setToolTip('使用 easyeda2kicad 转换，适用于 KiCad 3D 查看')
        self.obj_box = QCheckBox('OBJ')
        self.obj_box.setToolTip('下载官方 OBJ 模型文本')
        for widget in (self.step_box, self.wrl_box, self.obj_box):
            option_row.addWidget(widget)
        option_row.addStretch()
        self.overwrite_box = QCheckBox('覆盖已有文件')
        self.overwrite_box.setToolTip('默认跳过已存在的完整文件')
        option_row.addWidget(self.overwrite_box)
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
        self.preview_status = label('预览需要联网，模型由商城官方查看器加载', 'muted')
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
        footer.addWidget(label('基于 easyeda2kicad 1.0.1 · AGPL-3.0', 'muted'))
        footer.addStretch()
        source = label('模型来源：<a style="color:#638397" href="https://lceda.cn/">JLCEDA</a> / <a style="color:#638397" href="https://easyeda.com/">EasyEDA 官方库</a>', 'muted')
        source.setOpenExternalLinks(True)
        footer.addWidget(source)
        layout.addLayout(footer)

    def _restore_settings(self):
        default = str(Path(QStandardPaths.writableLocation(QStandardPaths.DocumentsLocation)) / 'LCSC3D')
        self.path_input.setText(default)
        if not self.settings_enabled:
            return
        try:
            settings = json.loads(SETTINGS_PATH.read_text(encoding='utf-8'))
            if not isinstance(settings, dict):
                return
            self.path_input.setText(settings.get('destination') or default)
            self.step_box.setChecked(bool(settings.get('step', True)))
            self.wrl_box.setChecked(bool(settings.get('wrl')))
            self.obj_box.setChecked(bool(settings.get('obj')))
            # Migrate a previous library-only selection to the default 3D format.
            if (settings.get('symbol') or settings.get('footprint')) and not any(
                    box.isChecked() for box in (self.step_box, self.wrl_box, self.obj_box)):
                self.step_box.setChecked(True)
        except (OSError, ValueError):
            pass

    def save_settings(self):
        if not self.settings_enabled:
            return
        try:
            SETTINGS_PATH.write_text(json.dumps({'destination': self.path_input.text(), 'step': self.step_box.isChecked(), 'wrl': self.wrl_box.isChecked(), 'obj': self.obj_box.isChecked()}, ensure_ascii=False, indent=2), encoding='utf-8')
        except OSError:
            pass

    def input_changed(self):
        ids, invalid, duplicates = parse_part_numbers(self.input.toPlainText())
        text = f'已识别 {len(ids)} 个器件'
        if duplicates:
            text += f' · 合并 {duplicates} 个重复编号'
        if invalid:
            text += ' · 请修正：' + ', '.join(invalid[:4]) + ('…' if len(invalid) > 4 else '')
        self.input_info.setText(text)

    def load_queue(self):
        if self.batch_running:
            return False
        ids, invalid, duplicates = parse_part_numbers(self.input.toPlainText())
        if invalid:
            self.run_status.setText('请先修正输入中无法识别的内容：' + ', '.join(invalid[:6]))
            return False
        if not ids:
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
        self.download_selection_changed()
        self.progress_bar.setRange(0, len(ids))
        self.progress_bar.setValue(0)
        self.run_status.setText('列表已载入，请勾选需要下载的器件；单击器件行自动预览')
        self.table.selectRow(0)
        self.request_component_info()
        return True

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

    def select_all_downloads(self):
        self.change_download_selection()

    def invert_download_selection(self):
        self.change_download_selection(invert=True)

    def change_download_selection(self, invert=False):
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

    def choose_folder(self):
        folder = QFileDialog.getExistingDirectory(self, '选择模型保存目录', self.path_input.text())
        if folder:
            self.path_input.setText(folder)
            self.save_settings()

    def open_output(self):
        path = Path(self.path_input.text().strip())
        if path.is_dir():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path.resolve())))
        else:
            self.run_status.setText('目录尚未创建，开始下载时会自动创建')

    def set_running(self, running):
        self.batch_running = running
        for widget in (self.input, self.sample_button, self.clear_button, self.path_input, self.browse_button, self.queue_button, self.start_button, self.step_box, self.wrl_box, self.obj_box, self.overwrite_box):
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
        formats = tuple(name for name, box in [('STEP', self.step_box), ('WRL', self.wrl_box), ('OBJ', self.obj_box)] if box.isChecked())
        if not formats:
            self.run_status.setText('请至少选择一种 3D 格式')
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
            # Use a unique temporary file, never replace an existing file.
            import tempfile
            with tempfile.TemporaryFile(dir=destination):
                pass
        except OSError as exc:
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
        worker = BatchWorker(self.batch_ids[:], Options(destination, formats, self.overwrite_box.isChecked()), self, rows=rows)
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
        colors = {'成功': '#168878', '已存在': '#168878', '部分完成': '#b47718', '失败': '#c15353', '无模型': '#b47718', '已取消': '#8695a3'}
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
        ok = sum(r.status in ('成功', '已存在') for r in results)
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
        self.store_button.setEnabled(bool(part or self.current_preview))
        result = self.results.get(part)
        self.part_folder_button.setEnabled(bool(result and result.folder and Path(result.folder).is_dir()))
        if result:
            self.detail.setText(part + ' · ' + result.message)
            self.detail.setToolTip(result.message)
        elif part:
            self.detail.setText(part + ' · 右侧自动预览，可切换 3D 模型、符号和封装')
        if part and part != self.current_preview:
            self.show_preview(part)

    def _setup_preview(self):
        self.web = QWebEngineView(self.preview_stack)
        page = PreviewPage(self.web_profile, self.web)
        page.state.connect(self.on_web_preview_state)
        self.web.setPage(page)
        self.web.loadFinished.connect(self.on_viewer_page_loaded)
        page.renderProcessTerminated.connect(self.on_viewer_terminated)
        self.web.settings().setAttribute(QWebEngineSettings.WebAttribute.WebGLEnabled, True)
        self.web.settings().setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, True)
        self.web.setContextMenuPolicy(Qt.NoContextMenu)
        self.preview_stack.addWidget(self.web)
        # setPage alone leaves Qt's rendering widget uninitialized. Load a local
        # blank page before show() so the first preview cannot recreate the HWND.
        # The placeholder remains visible; no model or remote page is loaded.
        self.web.setHtml('<!doctype html><html><body style="background:#f3f6fa"></body></html>')

    def set_preview_mode(self, mode):
        self.preview_mode = mode
        self.preview_mode_buttons[mode].setChecked(True)
        self.fit_button.setVisible(mode != '3d')
        self.preview_legend.setVisible(mode == 'footprint')
        self.symbol_unit_box.setVisible(mode == 'symbol' and self.symbol_unit_box.count() > 1)
        part = self.selected_part() or self.current_preview
        if part:
            self.show_preview(part)
        else:
            self.preview_hint.setText('选择左侧列表中的器件\n即可查看' + {'3d': '3D 模型', 'symbol': '符号', 'footprint': '封装'}[mode])
            self.preview_stack.setCurrentWidget(self.preview_empty)
            self.on_preview_state('empty', '选择器件后加载预览')

    def show_preview(self, part, reload=False):
        if not part or self.close_when_finished:
            return
        self.current_preview = part
        self.reload_button.setEnabled(True)
        self.store_button.setEnabled(True)
        if self.preview_mode != '3d':
            self.show_library_preview(part, reload)
            return
        self.library_pending = None
        self.symbol_unit_box.hide()
        self.preview_caption.setText(part + ' · 商城官方在线查看器')
        self.preview_stack.setCurrentWidget(self.web)
        if part == self.current_3d and self.viewer_started and not reload:
            self.on_preview_state(*self.web_state)
            return
        self.current_3d = part
        self.web_revision += 1
        self.web_state = ('loading', '正在连接商城在线查看器…')
        self.on_preview_state('loading', '正在连接商城在线查看器…')
        if not self.viewer_started or reload:
            self.viewer_started = True
            self.viewer_page_loads += 1
            html = (ROOT / 'viewer.html').read_text(encoding='utf-8').replace('__PART_JSON__', json.dumps(part))
            html = html.replace('__REVISION_JSON__', str(self.web_revision))
            self.web.setHtml(html, QUrl('https://item.szlcsc.com/'))
        else:
            self.update_viewer_part()

    def update_viewer_part(self):
        args = json.dumps(self.current_3d) + ',' + str(self.web_revision)
        self.web.page().runJavaScript(f'window.loadLcscPart && window.loadLcscPart({args})')

    def on_viewer_page_loaded(self, ok):
        if ok and self.viewer_started:
            self.update_viewer_part()

    def on_viewer_terminated(self, *args):
        self.viewer_started = False
        self.on_web_preview_state(self.web_revision, 'error', '3D 查看器已停止，请重新加载')

    def on_web_preview_state(self, token, status, message):
        if token not in (-1, self.web_revision):
            return
        self.web_state = (status, message)
        if self.preview_mode == '3d':
            self.on_preview_state(status, message)

    def show_library_preview(self, part, reload=False):
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
        if document.error or not view.show_document(document):
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

    def fit_preview(self):
        if self.preview_mode == 'symbol':
            self.symbol_view.fit_content()
        elif self.preview_mode == 'footprint':
            self.footprint_view.fit_content()

    def on_preview_state(self, status, message):
        self.preview_state = status
        self.preview_status.setText(message)
        self.preview_status.setStyleSheet('color:#b45745;' if status == 'error' else 'color:#168878;' if status == 'ready' else 'color:#788898;')
        self.preview_changed.emit(status)

    def open_store(self):
        part = self.current_preview or self.selected_part()
        if part:
            result = self.results.get(part)
            url = result.store_url if result else f'https://so.szlcsc.com/global.html?k={part}'
            QDesktopServices.openUrl(QUrl(url))

    def open_part_folder(self):
        result = self.results.get(self.selected_part())
        if result and result.folder:
            QDesktopServices.openUrl(QUrl.fromLocalFile(result.folder))

    def show_help(self):
        message = QMessageBox(self)
        message.setWindowTitle('使用说明与来源')
        message.setTextFormat(Qt.RichText)
        message.setText('<b>LCSC3D</b><br>版本：' + VERSION + '<br><br>'
            '1. 输入 C 开头的立创编号，支持换行、空格和逗号。<br>'
            '2. 载入列表后自动查询型号，勾选需要下载的器件；可使用「全选」「反选」。<br>'
            '3. 选择保存目录和 3D 格式，点击「开始下载」，仅下载勾选的器件。<br>'
            '4. 载入列表后自动预览首个器件，单击其他器件即可切换预览。<br><br>'
            'STEP 是原始 CAD 模型；WRL 适用于 KiCad；OBJ 是官方模型文本。<br>'
            '每个器件单独保存到“器件名_编号”目录，默认保留已有文件。<br>'
            '预览使用商城现有的官方查看器，联网加载模型；鼠标拖动旋转，滚轮缩放。<br><br>'
            '右侧上方可切换「3D 模型 / 符号 / 封装」，无需先下载。<br>'
            '符号和封装直接加载商城使用的官方 SVG，支持滚轮缩放、拖动平移及「适应窗口」；多单元符号可选择单元。<br><br>'
            '模型来源：<a href="https://lceda.cn/">JLCEDA</a> / <a href="https://easyeda.com/">EasyEDA 官方库</a>。<br>'
            '基于 <a href="https://github.com/uPesy/easyeda2kicad.py">easyeda2kicad 1.0.1</a>，软件采用 AGPL-3.0-or-later。<br>'
            '对应源码、构建脚本与第三方说明随交付提供。')
        message.exec()

    def closeEvent(self, event):
        if self.library_worker is not None or self.info_worker is not None or self.worker and self.worker.isRunning():
            self.close_when_finished = True
            self.info_pending = None
            self.library_pending = None
            for worker in (self.info_worker, self.library_worker):
                if worker is not None:
                    worker.cancelled.set()
            self.setEnabled(False)
            if self.worker and self.worker.isRunning():
                self.stop_batch()
            self.run_status.setText('正在关闭，等待网络请求结束…')
            event.ignore()
            return
        self.save_settings()
        event.accept()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--self-test', metavar='FOLDER', help=argparse.SUPPRESS)
    args = parser.parse_args()
    QApplication.setAttribute(Qt.AA_ShareOpenGLContexts)
    app = QApplication([sys.argv[0]])
    app.setApplicationName('LCSC3D')
    app.setApplicationVersion(VERSION)
    app.setStyle('Fusion')
    app.setFont(QFont('Microsoft YaHei UI', 9))
    app.setStyleSheet(STYLES)
    logging.getLogger().setLevel(logging.ERROR)
    window = MainWindow(settings_enabled=not bool(args.self_test))
    window.show()
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
        window.wrl_box.setChecked(True)
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
            passed = (window.preview_state == 'ready'
                      and state['second_3d_ready']
                      and state['library']
                      and report['preview_window']['hwnd_preserved']
                      and not preview_probe.events
                      and not report['csv_files']
                      and report['titles_before_download'].get('C2040') == 'RP2040'
                      and report['titles_before_download'].get('C20197') == '4D03WGJ0102T5E'
                      and report['titles_before_download'].get('C163691', '') not in
                          ('', '—', '查询中…', '查询失败', '未提供型号')
                      and report['download_selection'] == {
                          'checked_ids': ['C2040', 'C20197', 'C999999999999'],
                          'unchecked_ids': ['C163691'], 'selected_only': True}
                      and sum(result.status in ('成功', '已存在') for result in window.results.values()) == 2
                      and all({Path(file).suffix for file in result.files} == {'.step', '.wrl', '.obj'}
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
