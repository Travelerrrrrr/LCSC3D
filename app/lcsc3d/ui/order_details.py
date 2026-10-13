"""An order's goods, displayed inside the existing Orders content area."""
from decimal import Decimal

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout
from lcsc3d.core.i18n import text as tr, message as msg
from lcsc3d.ui.localized_widgets import QLabel, QPushButton, QTableWidgetItem
from lcsc3d.ui.ui_components import surface


class OrderDetailPanel(QWidget):
    back_requested = Signal()
    refresh_requested = Signal()
    open_requested = Signal(str)
    download_requested = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        from lcsc3d.ui.commerce import native_table
        self.order = None
        self.rows = []
        self.loaded = False
        self.import_enabled = True
        layout = QVBoxLayout(self); layout.setContentsMargins(0, 0, 0, 0)
        header = QHBoxLayout()
        self.back = QPushButton(tr('返回订单列表')); self.back.clicked.connect(self.back_requested)
        header.addWidget(self.back)
        self.heading = QLabel(tr('订单商品')); self.heading.setObjectName('section')
        self.heading.setTextFormat(Qt.PlainText); self.heading.setWordWrap(True)
        header.addWidget(self.heading, 1)
        self.refresh = QPushButton(tr('刷新明细')); self.refresh.clicked.connect(self.refresh_requested)
        header.addWidget(self.refresh)
        self.details = QPushButton(tr('查看订单详情'))
        self.details.setToolTip(str(tr('在外部浏览器查看商城完整订单')))
        self.details.clicked.connect(lambda: self.open_requested.emit(self.order['url']) if self.order else None)
        header.addWidget(self.details)
        layout.addLayout(header)
        self.hint = QLabel(''); self.hint.setObjectName('muted')
        self.hint.setTextFormat(Qt.PlainText); self.hint.setWordWrap(True)
        layout.addWidget(self.hint)
        card, body = surface(); body.setContentsMargins(0, 0, 0, 0)
        self.table = native_table(['选择', '商品编号', '商品 / 型号', '封装', '购买数量', '单价', '金额', '类别'], 2)
        for column, width in ((0, 50), (1, 135), (3, 130), (4, 120), (5, 120), (6, 130), (7, 100)):
            self.table.setColumnWidth(column, width)
        self.table.itemChanged.connect(lambda item: self.update_actions() if item.column() == 0 else None)
        body.addWidget(self.table); layout.addWidget(card, 1)
        footer = QHBoxLayout()
        self.select = QPushButton(tr('全选器件 / 取消')); self.select.clicked.connect(self.toggle_selection)
        footer.addWidget(self.select)
        self.summary = QLabel(''); self.summary.setTextFormat(Qt.PlainText); self.summary.setWordWrap(True)
        footer.addWidget(self.summary, 1)
        self.download = QPushButton(tr('加入下载列表')); self.download.setObjectName('primary')
        self.download.clicked.connect(lambda: self.download_requested.emit(self.selected_rows()))
        footer.addWidget(self.download); layout.addLayout(footer)
        self.clear()

    def begin(self, order):
        self.clear()
        self.order = dict(order)
        self.heading.setText(msg('订单商品 · {0}', order['code']) + '\n' +
                             ' · '.join(value for value in (order.get('date'), order.get('status'), order.get('payment')) if value))
        self.hint.setText(tr('正在读取订单商品…'))
        self.details.setEnabled(True)

    def show_result(self, result):
        from lcsc3d.ui.commerce import table_cell
        self.rows = result['items']
        self.table.blockSignals(True); self.table.setRowCount(len(self.rows))
        for index, row in enumerate(self.rows):
            choice = QTableWidgetItem()
            flags = Qt.ItemIsEnabled | Qt.ItemIsSelectable
            if row['downloadable']:
                flags |= Qt.ItemIsUserCheckable
                choice.setCheckState(Qt.Unchecked)
            else:
                choice.setToolTip(str(tr('此物品没有可导入的器件编号')))
            choice.setFlags(flags); self.table.setItem(index, 0, choice)
            title = '\n'.join(dict.fromkeys(value for value in (row['title'], row['name']) if value))
            for col, value in ((1, row['code']), (2, title), (3, row['package']),
                               (4, (row['quantity'] + ' ' + row['unit']).strip()), (7, row['kind'])):
                table_cell(self.table, index, col, value or '—', col == 4)
            for col, key in ((5, 'price'), (6, 'total')):
                value = row[key]
                if value:
                    value = (format(Decimal(value), '.2f') if key == 'total' else value) + ('/' + row['unit'] if key == 'price' and row['unit'] else '')
                table_cell(self.table, index, col, result['currency'] + value if value else '—', True)
            self.table.setRowHeight(index, max(60, self.table.fontMetrics().height() * 2 + 18))
        self.table.blockSignals(False)
        self.loaded = True
        self.refresh.setEnabled(True)
        self.hint.setText(msg('共 {0} 项 · 勾选器件可加入下载列表。', len(self.rows)) if self.rows else tr('订单暂无商品明细。'))
        amounts = []
        for title, key in (('订单总额', 'total'), ('商品金额', 'goods_total'), ('运费', 'freight')):
            value = format(Decimal(result[key]), '.2f') if result[key] else '—'
            amounts.append(tr(title) + ' ' + (result['currency'] + value if result[key] else value))
        self.summary.setText(tr(' · ').join(amounts))
        self.update_actions()

    def fail(self, error):
        self.loaded = False
        self.hint.setText(str(error)); self.refresh.setEnabled(True)
        self.update_actions()

    def selected_rows(self):
        return [row for index, row in enumerate(self.rows) if row['downloadable']
                and self.table.item(index, 0).checkState() == Qt.Checked]

    def toggle_selection(self):
        selected = len(self.selected_rows())
        checked = selected != sum(row['downloadable'] for row in self.rows)
        self.table.blockSignals(True)
        for index, row in enumerate(self.rows):
            if row['downloadable']:
                self.table.item(index, 0).setCheckState(Qt.Checked if checked else Qt.Unchecked)
        self.table.blockSignals(False); self.update_actions()

    def update_actions(self):
        self.select.setEnabled(self.loaded and any(row['downloadable'] for row in self.rows))
        self.download.setEnabled(self.loaded and self.import_enabled and bool(self.selected_rows()))

    def clear(self):
        self.order = None; self.rows = []; self.loaded = False
        self.table.setRowCount(0)
        self.heading.setText(tr('订单商品')); self.hint.clear(); self.summary.clear()
        self.refresh.setEnabled(False); self.details.setEnabled(False)
        self.update_actions()
