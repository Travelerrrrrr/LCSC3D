"""Native shopping tables. No web view or intermediate quantity page."""
from decimal import Decimal

from PySide6.QtCore import Qt, Signal, QSize, QTimer
from PySide6.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget, QSpinBox, QHeaderView, QAbstractItemView, QStackedWidget

from lcsc3d.core.i18n import text as tr, message as msg
from lcsc3d.services.commerce import MAX_CART_QUANTITY
from lcsc3d.ui.localized_widgets import QLabel, QPushButton, QTableWidget, QTableWidgetItem, QLineEdit, QComboBox
from lcsc3d.ui.ui_components import surface


class QuantityInput(QSpinBox):
    """Commit typed quantities on focus loss; scrolling never changes a purchase."""
    def __init__(self, value=1, minimum=1, parent=None, *, step=1, preserve_initial=False):
        super().__init__(parent)
        self.set_rules(minimum, step)
        if preserve_initial:
            # A pre-existing cart may contain a nonconforming quantity. Display
            # what the server actually has until the user explicitly edits it.
            self.setRange(min(value, self.minimum()), max(value, self.maximum()))
            super().setValue(value)
        else:
            self.setValue(value)
        self.setKeyboardTracking(False)
        self.setAlignment(Qt.AlignRight)
        self.setContextMenuPolicy(Qt.NoContextMenu)
        self.setAccessibleName(str(tr('数量')))

    def set_rules(self, minimum=1, step=1):
        step = max(1, min(int(step), MAX_CART_QUANTITY))
        upper = MAX_CART_QUANTITY // step * step
        lower = min(upper, (max(1, minimum) + step - 1) // step * step)
        self.purchase_minimum = lower
        self.setSingleStep(step)
        self.setRange(lower, upper)
        self.setValue(self.value())
        self.setToolTip(str(msg('起订量 {0}，递增量 {1}；输入后向上取整到有效数量。', lower, step)))

    def rounded(self, value):
        step = self.singleStep()
        return min(MAX_CART_QUANTITY // step * step,
                   max(self.purchase_minimum, (value + step - 1) // step * step))

    def setValue(self, value):
        super().setValue(self.rounded(value))

    def valueFromText(self, value):
        return self.rounded(super().valueFromText(value))

    def stepBy(self, steps):
        step = self.singleStep()
        base = (self.value() + (step - 1 if steps < 0 else 0)) // step
        self.setValue((base + steps) * step)

    def wheelEvent(self, event):
        event.ignore()


class PriceTierBox(QComboBox):
    """Display the entered quantity's price; the popup is a tier reference."""
    def __init__(self, product, parent=None):
        super().__init__(parent)
        self.tiers = product.get('price_tiers') or []
        self.actual_index = -1
        self.setMaxVisibleItems(12)
        self.setPlaceholderText('—')
        unit, symbol = product.get('unit') or tr('个'), product.get('currency_symbol', '¥')
        for index, tier in enumerate(self.tiers):
            end = tier.get('end')
            if end is None and index + 1 < len(self.tiers):
                end = self.tiers[index + 1]['quantity'] - 1
            span = f"{tier['quantity']}–{end}" if end is not None else f"{tier['quantity']}+"
            self.addItem(f"{span}  {symbol}{tier['price']}/{unit}", tier['quantity'])
        self.setToolTip(str(tr('单价按输入数量自动匹配；展开查看全部价格区间。')))
        self.activated.connect(lambda _: self.setCurrentIndex(self.actual_index))

    def set_quantity(self, amount):
        self.actual_index = next((index for index in range(len(self.tiers) - 1, -1, -1)
            if amount >= self.tiers[index]['quantity'] and
            (self.tiers[index].get('end') is None or amount <= self.tiers[index]['end'])), -1)
        self.setCurrentIndex(self.actual_index)

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
    open_cart_requested = Signal()
    remove_requested = Signal()
    download_requested = Signal(object)
    thumbnails_requested = Signal()
    preview_requested = Signal(object)

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
        self.table = native_table(['选择', 'C 编号', '型号', '封装', '单价', '数量', '小计', '状态', '递增量', '图片'], 2)
        self.table.thumbnail_column = 9
        self.table.thumbnail_channel = 'cart-thumbnails'
        self.table.thumbnail_visible_only = True
        self.table.setIconSize(QSize(48, 48))
        self.table.setColumnWidth(9, 76)
        self.table.verticalScrollBar().valueChanged.connect(lambda _: self.thumbnails_requested.emit())
        self.table.cellDoubleClicked.connect(lambda row, col: self.preview_requested.emit(self.rows[row])
            if col == self.table.thumbnail_column and 0 <= row < len(self.rows) and self.rows[row]['image'] else None)
        for column, width in ((0, 50), (1, 110), (3, 120), (4, 115), (5, 150), (6, 120), (7, 90), (8, 85)):
            self.table.setColumnWidth(column, width)
        self.table.horizontalHeader().moveSection(8, 5)
        self.table.horizontalHeader().moveSection(self.table.horizontalHeader().visualIndex(9), 1)
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
        self.open_cart = QPushButton(tr('跳转到购物车'))
        self.open_cart.setObjectName('primary')
        self.open_cart.setToolTip(str(tr('在外部浏览器打开商城购物车')))
        self.open_cart.clicked.connect(self.open_cart_requested)
        actions.addWidget(self.open_cart)
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
            photo = table_cell(self.table, index, self.table.thumbnail_column, '…' if row['image'] else '—')
            photo.setTextAlignment(Qt.AlignCenter)
            photo.setData(Qt.UserRole, row['image'])
            photo.setToolTip(str(tr('正在加载图片…') if row['image'] else tr('暂无商品图片')))
            for column, key in ((1, 'part'), (2, 'title'), (3, 'package'), (7, 'status')):
                table_cell(self.table, index, column, row[key])
            for column, key in ((4, 'price'), (6, 'total')):
                amount = format(Decimal(row[key]), '.2f' if key == 'total' else '.6f') if row[key] else ''
                if key == 'price' and amount:
                    amount = amount.rstrip('0').rstrip('.')
                table_cell(self.table, index, column, row['currency'] + amount if amount else '—', True)
            step = row.get('quantity_step', 1)
            table_cell(self.table, index, 8, step, True)
            spin = QuantityInput(row['quantity'], row['minimum'], step=step, preserve_initial=True)
            spin.ensurePolished()
            self.table.setRowHeight(index, max(68, spin.sizeHint().height() + 12))
            spin.valueChanged.connect(lambda amount, data=row: self.quantity_changed.emit(data, amount))
            self.table.setCellWidget(index, 5, spin)
        self.table.blockSignals(False)
        self.loaded = True; self.stale = self.uncertain = False
        self.hint.setText(msg('共 {0} 项 · 修改数量自动保存，可跳转到浏览器购物车。', len(rows)) if rows else tr('购物车为空，去搜索商品并添加。'))
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
        self.remove.setEnabled(bool(selected) and not self.busy and not self.stale)
        self.download.setEnabled(bool(selected) and not self.busy and self.import_enabled)

    def clear(self):
        self.rows = []; self.loaded = False; self.stale = self.uncertain = False
        self.table.setRowCount(0)
        self.hint.setText(tr('登录后读取购物车，可直接修改数量。'))
        self.set_busy(False)

    def showEvent(self, event):
        super().showEvent(event)
        QTimer.singleShot(0, self.thumbnails_requested.emit)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        QTimer.singleShot(0, self.thumbnails_requested.emit)


class OrdersPanel(QWidget):
    read_requested = Signal(int)
    open_requested = Signal(str)
    detail_requested = Signal(object)
    back_requested = Signal()
    download_requested = Signal(object)

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
        self.content = QStackedWidget()
        layout.addWidget(self.content, 1)
        self.list_page = QWidget()
        list_layout = QVBoxLayout(self.list_page); list_layout.setContentsMargins(0, 0, 0, 0)
        self.content.addWidget(self.list_page)
        from lcsc3d.ui.order_details import OrderDetailPanel
        self.detail = OrderDetailPanel()
        self.content.addWidget(self.detail)
        self.detail.back_requested.connect(self.back_requested)
        self.detail.open_requested.connect(self.open_requested)
        self.detail.download_requested.connect(self.download_requested)
        self.detail.refresh_requested.connect(lambda: self.detail_requested.emit(self.detail.order) if self.detail.order else None)
        self.hint = QLabel(tr('登录后读取订单；点击订单可在此查看商品。'))
        self.hint.setObjectName('muted'); self.hint.setWordWrap(True)
        list_layout.addWidget(self.hint)
        card, body = surface(); body.setContentsMargins(0, 0, 0, 0)
        self.table = native_table(['订单号', '下单时间', '商品', '金额', '订单状态', '付款状态'], 2)
        for col, width in ((0, 190), (1, 175), (3, 120), (4, 115), (5, 110)):
            self.table.setColumnWidth(col, width)
        self.table.cellClicked.connect(self.open_row)
        self.table.cellActivated.connect(self.open_row)
        body.addWidget(self.table)
        list_layout.addWidget(card, 1)
        footer = QHBoxLayout()
        self.previous = QPushButton(tr('上一页'))
        self.previous.clicked.connect(lambda: self.read_requested.emit(self.page - 1))
        footer.addWidget(self.previous)
        self.page_label = QLabel(tr('尚未查询')); self.page_label.setObjectName('muted')
        footer.addWidget(self.page_label, 1, Qt.AlignCenter)
        self.next = QPushButton(tr('下一页'))
        self.next.clicked.connect(lambda: self.read_requested.emit(self.page + 1))
        footer.addWidget(self.next)
        list_layout.addLayout(footer)
        self.set_busy(False)

    def open_row(self, row, column=0):
        item = self.table.item(row, 0) if row >= 0 else None
        if item is not None:
            self.detail_requested.emit(item.data(Qt.UserRole))

    def begin_detail(self, order):
        self.detail.begin(order)
        self.content.setCurrentWidget(self.detail)

    def show_list(self):
        self.content.setCurrentWidget(self.list_page)
        self.detail.clear()

    def show_result(self, result):
        self.table.setRowCount(len(result['items']))
        for index, row in enumerate(result['items']):
            for col, key in enumerate(('code', 'date', 'summary', 'total', 'status', 'payment')):
                value = row['currency'] + row[key] if key == 'total' and row[key] else row[key] or '—'
                table_cell(self.table, index, col, value, key == 'total')
            self.table.item(index, 0).setData(Qt.UserRole, row)
        self.page, self.pages = result['page'], result['pages']
        self.loaded = True
        self.page_label.setText(msg('第 {0} / {1} 页 · 共 {2} 个订单', self.page, max(1, self.pages), result['total']))
        self.hint.setText(tr('点击订单查看购买的商品与金额。') if result['items'] else tr('没有符合条件的订单。'))
        self.set_busy(False)

    def set_busy(self, value):
        for widget in (self.refresh, self.keyword, self.filter, self.table):
            widget.setEnabled(not value)
        self.previous.setEnabled(not value and self.page > 1)
        self.next.setEnabled(not value and self.page < self.pages)

    def clear(self):
        self.loaded = False; self.page = self.pages = 0
        self.table.setRowCount(0)
        self.page_label.setText(tr('尚未查询'))
        self.hint.setText(tr('登录后读取订单；点击订单可在此查看商品。'))
        self.show_list()
        self.set_busy(False)
