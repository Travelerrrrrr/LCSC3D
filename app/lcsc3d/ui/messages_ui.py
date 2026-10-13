"""A searchable, newest-first page for previously displayed application notices."""
from datetime import datetime

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QVBoxLayout, QHBoxLayout, QHeaderView, QAbstractItemView

from lcsc3d.core.app_logging import log_event
from lcsc3d.core.i18n import text as ui_text, message as ui_message, register
from lcsc3d.ui.localized_widgets import (QDialog, QLabel, QLineEdit, QComboBox, QPushButton,
                               QTableWidget, QTableWidgetItem, QPlainTextEdit)
from lcsc3d.ui.ui_components import title_block

LEVEL_LABELS = dict(info='信息', success='成功', warning='提醒', error='错误')


class MessagesPage(QDialog):
    def __init__(self, history, parent):
        super().__init__(parent)
        self.history = history
        self.rows = []
        self.search_texts = {}
        self.setWindowTitle(ui_text('消息'))
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 18)
        layout.setSpacing(12)
        layout.addWidget(title_block(ui_text('消息'), ui_text('查看软件已弹出的通知，最新消息在上方'), large=True))
        filters = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText(ui_text('搜索消息'))
        self.level = QComboBox()
        self.level.addItem(ui_text('全部类型'), '')
        for key, value in LEVEL_LABELS.items():
            self.level.addItem(ui_text(value), key)
        self.clear_button = QPushButton(ui_text('清空记录'))
        self.clear_button.clicked.connect(self.history.clear)
        filters.addWidget(self.search, 1)
        filters.addWidget(self.level)
        filters.addWidget(self.clear_button)
        layout.addLayout(filters)
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels([ui_text('时间'), ui_text('类型'), ui_text('标题')])
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.verticalHeader().hide()
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.table.setMinimumHeight(180)
        layout.addWidget(self.table, 1)
        self.details = QPlainTextEdit()
        self.details.setReadOnly(True)
        self.details.setPlaceholderText(ui_text('选择消息查看完整内容'))
        self.details.setMinimumHeight(140)
        layout.addWidget(self.details, 1)
        footer = QHBoxLayout()
        self.status = QLabel('')
        self.status.setObjectName('muted')
        self.status.setWordWrap(True)
        self.copy_button = QPushButton(ui_text('复制消息'))
        self.copy_button.clicked.connect(self.copy_current)
        footer.addWidget(self.status, 1)
        footer.addWidget(self.copy_button)
        layout.addLayout(footer)
        self.search.textChanged.connect(self.reload)
        self.level.currentIndexChanged.connect(self.reload)
        self.history.changed.connect(self.reload)
        self.table.currentCellChanged.connect(self.show_current)
        register(self)
        self.reload()

    def retranslate(self):
        super().retranslate()
        if hasattr(self, 'table'):
            self.reload()

    def reload(self):
        selected = self.rows[self.table.currentRow()]['id'] if 0 <= self.table.currentRow() < len(self.rows) else None
        query, level = self.search.text().casefold().strip(), self.level.currentData()
        texts = {}
        for entry in self.history.entries:
            source = entry['title'], entry['message'], entry['details']
            previous = self.search_texts.get(entry['id'])
            texts[entry['id']] = previous if previous and previous[0] == source else (source, '\n'.join(source).casefold())
        self.search_texts = texts
        rows = [e for e in self.history.entries if (not level or e['severity'] == level)
                and (not query or query in texts[e['id']][1])]
        wanted = {e['id'] for e in rows}
        self.table.blockSignals(True)
        updating = self.table.updatesEnabled()
        self.table.setUpdatesEnabled(False)
        for index in range(len(self.rows) - 1, -1, -1):
            if self.rows[index]['id'] not in wanted:
                self.table.removeRow(index)
                self.rows.pop(index)
        for row, entry in enumerate(rows):
            if row >= len(self.rows) or self.rows[row]['id'] != entry['id']:
                self.table.insertRow(row)
                self.rows.insert(row, entry)
            time = datetime.fromisoformat(entry['time']).astimezone().strftime('%Y-%m-%d %H:%M:%S')
            for column, value in enumerate((time, ui_text(LEVEL_LABELS[entry['severity']]), entry['title'])):
                item = self.table.item(row, column)
                if item is None:
                    item = QTableWidgetItem(value)
                    self.table.setItem(row, column, item)
                elif item.source_text() != value:
                    item.setText(value)
                if item._messages.get('setToolTip') != entry['message']:
                    item.setToolTip(entry['message'])
        self.table.setRowCount(len(rows))
        self.rows = rows
        index = next((i for i, e in enumerate(self.rows) if e['id'] == selected), 0)
        if self.rows:
            self.table.selectRow(index)
            self.table.setCurrentCell(index, 0)
        else:
            self.table.setCurrentCell(-1, -1)
        self.table.blockSignals(False)
        self.table.setUpdatesEnabled(updating)
        self.status.setText(ui_text('消息保存失败，当前记录仅在本次运行中可用。') if self.history.save_failed else
            ui_message('显示 {0} / {1} 条消息，保留最近 500 条。', len(self.rows), len(self.history.entries)))
        self.clear_button.setEnabled(bool(self.history.entries))
        self.show_current()

    def show_current(self, *args):
        row = self.table.currentRow()
        entry = self.rows[row] if 0 <= row < len(self.rows) else None
        text = '' if entry is None else '\n\n'.join(filter(None, (entry['title'], entry['message'], entry['details'])))
        self.details.setPlainText(text)
        self.copy_button.setEnabled(entry is not None)

    def copy_current(self):
        if self.copy_button.isEnabled():
            QApplication.clipboard().setText(self.details.toPlainText())
            log_event('INFO', 'notification.history_copied', message_id=self.rows[self.table.currentRow()]['id'])
