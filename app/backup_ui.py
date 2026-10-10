"""Backup controls and restoration inside the application's existing page host."""
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt, QThread, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QVBoxLayout, QHBoxLayout, QHeaderView, QAbstractItemView
from localized_widgets import QCheckBox, QDialog, QGroupBox, QLabel, QLineEdit, QPushButton, QTableWidget, QTableWidgetItem
from i18n import text as ui_text, message as ui_message, render
from app_logging import contextual, new_context, record_error
from backups import backup_directory, list_backups, prepare_restore, restore_backup, clear_backups
from shell_ui import choose_path, notify, page_host
from ui_components import title_block


def format_size(size):
    for unit in ('B', 'KiB', 'MiB', 'GiB'):
        if size < 1024 or unit == 'GiB':
            return f'{size:.1f} {unit}' if unit != 'B' else f'{size} B'
        size /= 1024


class BackupWorker(QThread):
    def __init__(self, task, parent):
        super().__init__(parent)
        self.task, self.result, self.error = task, None, ''
        self.log_context = new_context(feature='backups')

    @contextual
    def run(self):
        try:
            self.result = self.task()
        except Exception as exc:
            record_error(exc, 'backup.operation_failed')
            self.error = render(str(exc))


class BackupPanel(QGroupBox):
    def __init__(self, preferences, settings):
        super().__init__(ui_text('备份'), settings)
        self.settings, self.rows = settings, []
        self.restore_page = None
        self.busy = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 22, 16, 16)
        layout.setSpacing(12)
        self.auto_box = QCheckBox(ui_text('自动备份'))
        self.auto_box.setChecked(preferences.auto_backup)
        layout.addWidget(self.auto_box)
        hint = QLabel(ui_text('修改已有元件库或工程前保留原文件。自动备份开关在保存设置后生效；恢复会先备份当前内容。'))
        hint.setObjectName('muted')
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self.usage = QLabel('')
        self.usage.setObjectName('section')
        self.usage.setWordWrap(True)
        layout.addWidget(self.usage)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels([ui_text('备份时间'), ui_text('文件'), ui_text('原位置'), ui_text('大小')])
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.verticalHeader().hide()
        self.table.setMinimumHeight(220)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.table.setColumnWidth(0, 165)
        self.table.setColumnWidth(1, 150)
        self.table.setColumnWidth(3, 80)
        layout.addWidget(self.table)
        self.detail = QLabel('')
        self.detail.setWordWrap(True)
        self.detail.setTextFormat(Qt.PlainText)
        self.detail.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(self.detail)
        buttons = QHBoxLayout()
        self.restore_button = QPushButton(ui_text('恢复选中备份'))
        self.restore_button.setObjectName('primary')
        self.restore_button.clicked.connect(self.open_restore)
        self.refresh_button = QPushButton(ui_text('刷新'))
        self.refresh_button.clicked.connect(self.refresh)
        self.clear_button = QPushButton(ui_text('清除备份'))
        self.clear_button.clicked.connect(self.confirm_clear)
        for button in (self.restore_button, self.refresh_button):
            button.setAutoDefault(False)
            buttons.addWidget(button)
        buttons.addStretch()
        layout.addLayout(buttons)
        self.open_button = QPushButton(ui_text('打开目录'))
        self.open_button.setToolTip(ui_text('打开备份目录'))
        self.open_button.setAutoDefault(False)
        self.open_button.clicked.connect(self.open_directory)
        folders = QHBoxLayout()
        folders.addWidget(self.open_button)
        folders.addStretch()
        self.clear_button.setAutoDefault(False)
        self.clear_button.setProperty('variant', 'danger')
        folders.addWidget(self.clear_button)
        layout.addLayout(folders)
        self.table.itemSelectionChanged.connect(self.refresh_buttons)
        self.refresh()

    def main_window(self):
        host = page_host(self)
        return host.owner if host else self.settings.parentWidget()

    def showEvent(self, event):
        self.refresh()
        super().showEvent(event)

    def available(self):
        return self.settings.worker is None and not getattr(self.main_window(), 'batch_running', False)

    def selected(self):
        index = self.table.currentRow()
        return self.rows[index] if 0 <= index < len(self.rows) else None

    def refresh(self):
        selected = self.selected()
        try:
            self.rows = list_backups()
            self.usage.setText(ui_message('{0} 份备份 · 占用 {1}', len(self.rows), format_size(sum(row.stored_size for row in self.rows))))
        except Exception as exc:
            record_error(exc, 'backup.list_failed')
            self.rows = []
            self.usage.setText(ui_text('无法读取备份目录，请检查目录权限。'))
        self.table.setRowCount(0)
        for index, row in enumerate(self.rows):
            self.table.insertRow(index)
            stamp = datetime.fromisoformat(row.created).astimezone().strftime('%Y-%m-%d %H:%M:%S')
            location = ui_text('信息损坏，仅可清除') if row.error else row.original_path or ui_text('旧版备份，恢复时指定目标')
            for column, text in enumerate((stamp, row.name, location, format_size(row.size))):
                item = QTableWidgetItem(text)
                item.setToolTip(str(text))
                self.table.setItem(index, column, item)
        self.table.verticalHeader().setDefaultSectionSize(max(36, self.table.fontMetrics().height() + 16))
        if self.rows:
            self.table.selectRow(next((i for i, row in enumerate(self.rows) if selected and row.id == selected.id), 0))
        self.refresh_buttons()

    def refresh_buttons(self):
        row = self.selected()
        enabled = self.available()
        self.auto_box.setEnabled(self.settings.worker is None)
        self.restore_button.setEnabled(enabled and row is not None and not row.error)
        self.clear_button.setEnabled(enabled and bool(self.rows))
        self.refresh_button.setEnabled(self.settings.worker is None)
        self.open_button.setEnabled(self.settings.worker is None)
        self.table.setEnabled(self.settings.worker is None)
        if row is None:
            detail = ui_text('暂无备份。开启自动备份后，修改已有元件库或工程时会在此保留原文件。')
        elif row.error:
            detail = ui_text('信息损坏，仅可清除')
        elif row.original_path:
            detail = ui_text('原位置：') + row.original_path
        else:
            detail = ui_text('旧版备份未记录原路径，恢复时请指定完整目标路径。')
        self.detail.setText(detail)

    def open_directory(self):
        try:
            folder = backup_directory()
            folder.mkdir(parents=True, exist_ok=True)
            if QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder))):
                return
        except OSError as exc:
            record_error(exc, 'backup.open_directory_failed')
        notify(self.settings, ui_text('备份'), ui_text('无法打开备份目录，请检查目录权限。'), severity='warning')

    def start_task(self, task, completed):
        if not self.available():
            return
        self.busy = True
        worker = BackupWorker(task, self.settings)
        self.settings.worker = worker
        self.settings.set_log_busy(True)
        if self.restore_page is not None:
            self.restore_page.setEnabled(False)
        self.refresh_buttons()
        self.settings.status.setText(ui_text('正在处理备份，请稍候…'))
        self.settings.status.show()
        def finish():
            self.settings.worker = None
            self.busy = False
            self.settings.set_log_busy(False)
            if self.restore_page is not None:
                self.restore_page.setEnabled(True)
            self.refresh()
            if worker.error:
                self.settings.status.setText(worker.error)
                notify(self.settings, ui_text('备份操作失败'), worker.error, severity='error', persistent=True)
            else:
                completed(worker.result)
            worker.deleteLater()
        worker.finished.connect(finish)
        worker.start()

    def confirm_clear(self):
        if not self.available() or not self.rows:
            return
        keys = tuple(row.id for row in self.rows)
        def completed(result):
            self.settings.status.setText(ui_message('已清除 {0} 份备份；{1} 份未能清除。', len(result['cleared']), len(result['failed'])))
        self.confirmation = notify(self.settings, ui_text('清除备份'),
            ui_message('将永久清除当前列表中的 {0} 份备份（{1}），无法撤销。原元件库和工程文件不会删除。', len(keys), format_size(sum(row.stored_size for row in self.rows))),
            severity='warning', persistent=True,
            actions=((ui_text('取消'), None), (ui_text('清除备份'), lambda: self.start_task(lambda: clear_backups(keys), completed))))

    def open_restore(self):
        row = self.selected()
        if self.available() and row is not None and not row.error:
            self.restore_page = RestorePage(self, row)
            self.restore_page.show()


class RestorePage(QDialog):
    def __init__(self, panel, row):
        super().__init__(panel.settings)
        self.panel, self.row = panel, row
        self.setWindowTitle(ui_text('恢复备份'))
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 18)
        layout.setSpacing(16)
        layout.addWidget(title_block(ui_text('恢复备份'), row.name, large=True))
        note = QLabel(ui_text('选择恢复位置。可输入新的完整文件路径，或选择已有文件；文件类型须与备份一致。'))
        note.setWordWrap(True)
        layout.addWidget(note)
        self.target = QLineEdit(row.original_path)
        self.target.setPlaceholderText(ui_text('输入完整目标文件路径'))
        self.target.setMinimumWidth(300)
        layout.addWidget(self.target)
        browse = QPushButton(ui_text('选择已有文件…'))
        browse.clicked.connect(self.choose_target)
        layout.addWidget(browse, 0, Qt.AlignLeft)
        hint = QLabel(ui_text('恢复前会校验备份，并保留目标文件的当前内容；即使关闭自动备份，此保护仍生效。'))
        hint.setWordWrap(True)
        hint.setObjectName('muted')
        layout.addWidget(hint)
        layout.addStretch()
        buttons = QHBoxLayout()
        buttons.addStretch()
        cancel = QPushButton(ui_text('取消'))
        cancel.clicked.connect(self.reject)
        buttons.addWidget(cancel)
        self.restore_button = QPushButton(ui_text('恢复备份'))
        self.restore_button.setObjectName('primary')
        self.restore_button.clicked.connect(self.prepare)
        buttons.addWidget(self.restore_button)
        layout.addLayout(buttons)

    def choose_target(self):
        self.file_page = choose_path(self, ui_text('选择恢复目标'), self.target.text(), self.target.setText,
                                     filter='Altium (*' + self.row.path.suffix + ')')

    def prepare(self):
        if not self.target.text().strip():
            notify(self, ui_text('恢复备份'), ui_text('请先指定恢复目标。'), severity='warning')
            return
        target = self.target.text().strip()
        self.panel.start_task(lambda: prepare_restore(self.row.id, target), self.confirm)

    def confirm(self, plan):
        self.panel.settings.status.hide()
        self.confirmation = notify(self, ui_text('确认恢复备份'),
            ui_message('恢复目标：\n{0}\n确认后将写入备份内容；已有文件会先备份再替换。', str(plan.target)),
            severity='warning', persistent=True, actions=((ui_text('取消'), None),
                (ui_text('确认恢复'), lambda: self.panel.start_task(lambda: restore_backup(plan), self.completed))))

    def completed(self, result):
        self.accept()
        self.panel.settings.status.setText(ui_message('备份已恢复到：{0}', result['path']))
        self.panel.settings.status.show()
        notify(self.panel.settings, ui_text('恢复完成'), ui_message('备份已恢复到：{0}', result['path']), severity='success')

    def done(self, result):
        if not self.panel.busy:
            super().done(result)

    def closeEvent(self, event):
        if self.panel.busy:
            event.ignore()
        else:
            super().closeEvent(event)
