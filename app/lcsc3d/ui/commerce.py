"""Native shopping tables. No web view or intermediate quantity page."""
from decimal import Decimal

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget, QSpinBox, QHeaderView, QAbstractItemView

from lcsc3d.core.i18n import text as tr, message as msg
from lcsc3d.services.commerce import MAX_CART_QUANTITY
from lcsc3d.ui.localized_widgets import QLabel, QPushButton, QTableWidget, QTableWidgetItem, QLineEdit, QComboBox
from lcsc3d.ui.ui_components import surface


class QuantityInput(QSpinBox):
    """Commit typed quantities on focus loss; scrolling never changes a purchase."""
    def __init__(self, value=1, minimum=1, parent=None):
        super().__init__(parent)
        self.setRange(minimum, MAX_CART_QUANTITY)
        self.setValue(value)
        self.setKeyboardTracking(False)
        self.setAlignment(Qt.AlignRight)
        self.setContextMenuPolicy(Qt.NoContextMenu)
        self.setAccessibleName(str(tr('数量')))

    def wheelEvent(self, event):
        event.ignore()


def native_table(headers, stretch):
    table = QTableWidget(0, len(headers))
    table.setHorizontalHeaderLabels([tr(title) for title in headers])
    table.setEditTriggers(QAbstractItemView.NoEditTriggers)
    table.setSelectionBehavior(QAbstractItemView.SelectRows)
    table.setSelectionMode(QAbstractItemView.SingleSelection)
    table.setAlternatingRowColors(True)
    table.setShowGrid(False)
    table.setWordWrap(False)
    table.verticalHeader().hide()
    table.verticalHeader().setDefaultSectionSize(54)
    table.horizontalHeader().setSectionResizeMode(stretch, QHeaderView.Stretch)
    table.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
    return table


def table_cell(table, row, column, value, right=False):
    item = QTableWidgetItem(str(value))
    item.setToolTip(str(value))
    if right:
        item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
    table.setItem(row, column, item)
    return item


class CartPanel(QWidget):
    refresh_requested = Signal()
    quantity_changed = Signal(object, int)
    checkout_requested = Signal()
    remove_requested = Signal()
    download_requested = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.rows = []
        self.loaded = False
        self.busy = False
        self.stale = False
        self.uncertain = False
        self.import_enabled = True
        layout = QVBoxLayout(self); layout.setContentsMargins(0, 8, 0, 0)
        toolbar = QHBoxLayout()
        self.refresh = QPushButton(tr('刷新购物车'))
        self.refresh.clicked.connect(self.refresh_requested)
        toolbar.addWidget(self.refresh)
        self.hint = QLabel(tr('登录后读取购物车，可直接修改数量。'))
        self.hint.setObjectName('muted'); self.hint.setWordWrap(True)
        toolbar.addWidget(self.hint, 1)
        layout.addLayout(toolbar)
        card, body = surface()
        body.setContentsMargins(0, 0, 0, 0)
        self.table = native_table(['选择', 'C 编号', '型号', '封装', '单价', '数量', '小计', '状态'], 2)
        for column, width in ((0, 50), (1, 110), (3, 120), (4, 115), (5, 150), (6, 120), (7, 90)):
            self.table.setColumnWidth(column, width)
        self.table.itemChanged.connect(lambda item: self.update_summary() if item.column() == 0 else None)
        body.addWidget(self.table)
        layout.addWidget(card, 1)
        actions = QHBoxLayout()
        self.select = QPushButton(tr('全选 / 取消'))
        self.select.clicked.connect(self.toggle_selection)
        actions.addWidget(self.select)
        self.remove = QPushButton(tr('移除所选'))
        self.remove.setProperty('variant', 'danger')
        self.remove.clicked.connect(self.remove_requested)
        actions.addWidget(self.remove)
        self.download = QPushButton(tr('加入下载列表'))
        self.download.clicked.connect(lambda: self.download_requested.emit(self.selected_rows()))
        actions.addWidget(self.download)
        self.summary = QLabel(''); self.summary.setObjectName('section')
        actions.addWidget(self.summary, 1, Qt.AlignRight)
        self.checkout = QPushButton(tr('快捷下单'))
        self.checkout.setObjectName('primary')
        self.checkout.clicked.connect(self.checkout_requested)
        actions.addWidget(self.checkout)
        layout.addLayout(actions)
        self.update_summary()

    def selected_rows(self):
        return [row for index, row in enumerate(self.rows)
                if self.table.item(index, 0).checkState() == Qt.Checked]

    def show_rows(self, rows):
        selected = {row['key'] for row in self.selected_rows()} if self.loaded else None
        self.rows = rows
        self.table.blockSignals(True)
        self.table.setRowCount(len(rows))
        for index, row in enumerate(rows):
            item = QTableWidgetItem()
            item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable | Qt.ItemIsUserCheckable)
            checked = row['key'] in selected if selected is not None else row['checked']
            item.setCheckState(Qt.Checked if checked else Qt.Unchecked)
            self.table.setItem(index, 0, item)
            for column, key in ((1, 'part'), (2, 'title'), (3, 'package'), (7, 'status')):
                table_cell(self.table, index, column, row[key])
            for column, key in ((4, 'price'), (6, 'total')):
                amount = format(Decimal(row[key]), '.2f' if key == 'total' else '.6f') if row[key] else ''
                if key == 'price' and amount:
                    amount = amount.rstrip('0').rstrip('.')
                table_cell(self.table, index, column, row['currency'] + amount if amount else '—', True)
            spin = QuantityInput(row['quantity'], row['minimum'])
            spin.setToolTip(str(tr('修改后自动保存到购物车')))
            spin.ensurePolished()
            self.table.setRowHeight(index, max(54, spin.sizeHint().height() + 12))
            spin.valueChanged.connect(lambda amount, data=row: self.quantity_changed.emit(data, amount))
            self.table.setCellWidget(index, 5, spin)
        self.table.blockSignals(False)
        self.loaded = True; self.stale = self.uncertain = False
        self.hint.setText(msg('共 {0} 项 · 修改数量自动保存，勾选后可快捷下单。', len(rows)) if rows else tr('购物车为空，去搜索商品并添加。'))
        self.set_busy(False)

    def toggle_selection(self):
        checked = len(self.selected_rows()) != len(self.rows)
        for index in range(len(self.rows)):
            self.table.item(index, 0).setCheckState(Qt.Checked if checked else Qt.Unchecked)

    def set_busy(self, value):
        self.busy = value
        self.table.setEnabled(not value)
        self.refresh.setEnabled(not value)
        self.select.setEnabled(not value and bool(self.rows))
        self.update_summary()

    def update_summary(self):
        selected = self.selected_rows()
        totals = {}
        for row in selected:
            if row['total']:
                totals[row['currency']] = totals.get(row['currency'], Decimal(0)) + Decimal(row['total'])
        amounts = ' + '.join(symbol + format(amount, '.2f') for symbol, amount in totals.items())
        self.summary.setText(msg('已选 {0} 项', len(selected)) + (' · ' + amounts if amounts else ''))
        self.checkout.setEnabled(bool(selected) and not self.busy and not self.stale and all(row['available'] for row in selected))
        self.remove.setEnabled(bool(selected) and not self.busy and not self.stale)
        self.download.setEnabled(bool(selected) and not self.busy and self.import_enabled)

    def clear(self):
        self.rows = []; self.loaded = False; self.stale = self.uncertain = False
        self.table.setRowCount(0)
        self.hint.setText(tr('登录后读取购物车，可直接修改数量。'))
        self.set_busy(False)


class OrdersPanel(QWidget):
    read_requested = Signal(int)
    open_requested = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.loaded = False
        self.page = self.pages = 0
        layout = QVBoxLayout(self); layout.setContentsMargins(0, 8, 0, 0)
        tools = QHBoxLayout()
        self.keyword = QLineEdit(); self.keyword.setMaxLength(160)
        self.keyword.setPlaceholderText(tr('搜索订单号、器件编号或型号'))
        self.keyword.returnPressed.connect(lambda: self.read_requested.emit(1))
        tools.addWidget(self.keyword, 1)
        self.filter = QComboBox()
        for label, value in (('全部订单', ''), ('待付款', 'wait'), ('待审核', 'wait_audit'), ('待发货', 'notSend'), ('已发货', 'already_send'), ('已取消', 'canceled')):
            self.filter.addItem(tr(label), value)
        self.filter.currentIndexChanged.connect(lambda: self.read_requested.emit(1))
        tools.addWidget(self.filter)
        self.refresh = QPushButton(tr('查询订单'))
        self.refresh.clicked.connect(lambda: self.read_requested.emit(1))
        tools.addWidget(self.refresh)
        layout.addLayout(tools)
        self.hint = QLabel(tr('登录后读取订单；双击订单可在浏览器查看详情。'))
        self.hint.setObjectName('muted'); self.hint.setWordWrap(True)
        layout.addWidget(self.hint)
        card, body = surface(); body.setContentsMargins(0, 0, 0, 0)
        self.table = native_table(['订单号', '下单时间', '商品', '金额', '订单状态', '付款状态'], 2)
        for col, width in ((0, 190), (1, 175), (3, 120), (4, 115), (5, 110)):
            self.table.setColumnWidth(col, width)
        self.table.cellDoubleClicked.connect(self.open_row)
        body.addWidget(self.table)
        layout.addWidget(card, 1)
        footer = QHBoxLayout()
        self.previous = QPushButton(tr('上一页'))
        self.previous.clicked.connect(lambda: self.read_requested.emit(self.page - 1))
        footer.addWidget(self.previous)
        self.page_label = QLabel(tr('尚未查询')); self.page_label.setObjectName('muted')
        footer.addWidget(self.page_label, 1, Qt.AlignCenter)
        self.next = QPushButton(tr('下一页'))
        self.next.clicked.connect(lambda: self.read_requested.emit(self.page + 1))
        footer.addWidget(self.next)
        self.details = QPushButton(tr('查看订单详情'))
        self.details.clicked.connect(lambda: self.open_row(self.table.currentRow()))
        footer.addWidget(self.details)
        self.table.currentCellChanged.connect(lambda *args: self.details.setEnabled(self.table.currentRow() >= 0))
        layout.addLayout(footer)
        self.set_busy(False)

    def open_row(self, row, column=0):
        item = self.table.item(row, 0) if row >= 0 else None
        if item is not None:
            self.open_requested.emit(item.data(Qt.UserRole))

    def show_result(self, result):
        self.table.setRowCount(len(result['items']))
        for index, row in enumerate(result['items']):
            for col, key in enumerate(('code', 'date', 'summary', 'total', 'status', 'payment')):
                value = row['currency'] + row[key] if key == 'total' and row[key] else row[key] or '—'
                table_cell(self.table, index, col, value, key == 'total')
            self.table.item(index, 0).setData(Qt.UserRole, row['url'])
        self.page, self.pages = result['page'], result['pages']
        self.loaded = True
        self.page_label.setText(msg('第 {0} / {1} 页 · 共 {2} 个订单', self.page, max(1, self.pages), result['total']))
        self.hint.setText(tr('双击订单或点击「查看订单详情」在浏览器继续。') if result['items'] else tr('没有符合条件的订单。'))
        self.set_busy(False)

    def set_busy(self, value):
        for widget in (self.refresh, self.keyword, self.filter, self.table):
            widget.setEnabled(not value)
        self.previous.setEnabled(not value and self.page > 1)
        self.next.setEnabled(not value and self.page < self.pages)
        self.details.setEnabled(not value and self.table.currentRow() >= 0)

    def clear(self):
        self.loaded = False; self.page = self.pages = 0
        self.table.setRowCount(0)
        self.page_label.setText(tr('尚未查询'))
        self.hint.setText(tr('登录后读取订单；双击订单可在浏览器查看详情。'))
        self.set_busy(False)
