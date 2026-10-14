"""Native category and filter pages occupying the search results area."""
from copy import deepcopy
from decimal import Decimal, InvalidOperation

from PySide6.QtCore import Qt, Signal, QTimer
from PySide6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QListWidget, QListWidgetItem, QTreeWidget, QTreeWidgetItem, QScrollArea, QFrame, QLayout
from lcsc3d.ui.localized_widgets import QLabel, QPushButton, QLineEdit, QComboBox, QCheckBox
from lcsc3d.ui.ui_components import surface, IconButton
from lcsc3d.core.i18n import text as tr, message as msg
from lcsc3d.core.app_logging import log_event
from lcsc3d.services.catalog_filters import (DEFAULTS, SORTS, normalize_filters, filter_count,
                                           range_input, range_bounds, option_in_range, finite_number)
from lcsc3d.services.store import StoreError


def hint(text=''):
    label = QLabel(tr(text)); label.setObjectName('muted')
    label.setTextFormat(Qt.PlainText); label.setWordWrap(True)
    return label


class CategoryPanel(QWidget):
    back_requested = Signal()
    refresh_requested = Signal()
    selected = Signal(object)

    def __init__(self):
        super().__init__()
        self.categories = []
        layout = QVBoxLayout(self); layout.setContentsMargins(0, 0, 0, 0)
        tools = QHBoxLayout()
        back = IconButton(tr('返回商品'), 'back'); back.clicked.connect(self.back_requested)
        tools.addWidget(back)
        self.search = QLineEdit(); self.search.setPlaceholderText(tr('查找分类，例如 电容 / 电源 / MCU'))
        self.search.setClearButtonEnabled(True); tools.addWidget(self.search, 1)
        self.refresh = IconButton(tr('刷新分类'), 'refresh'); self.refresh.clicked.connect(self.refresh_requested)
        tools.addWidget(self.refresh); layout.addLayout(tools)
        self.hint = hint('分类来自立创商城，双击分类可查看商品。'); layout.addWidget(self.hint)
        card, body = surface(); body.setContentsMargins(8, 8, 8, 8)
        columns = QHBoxLayout()
        self.groups = QListWidget(); self.groups.setObjectName('catalogGroups'); self.groups.setMaximumWidth(210)
        self.groups.currentRowChanged.connect(self.populate)
        columns.addWidget(self.groups, 1)
        self.tree = QTreeWidget(); self.tree.setHeaderHidden(True); self.tree.setObjectName('catalogTree')
        self.tree.setIndentation(20); self.tree.setUniformRowHeights(True)
        self.tree.itemSelectionChanged.connect(self.update_choice)
        self.tree.itemDoubleClicked.connect(lambda *_: self.choose())
        columns.addWidget(self.tree, 3); body.addLayout(columns); layout.addWidget(card, 1)
        footer = QHBoxLayout(); self.choice = hint('选择一个分类'); footer.addWidget(self.choice, 1)
        self.apply = QPushButton(tr('查看分类商品')); self.apply.setObjectName('primary')
        self.apply.setEnabled(False); self.apply.clicked.connect(self.choose); footer.addWidget(self.apply)
        layout.addLayout(footer)
        self.timer = QTimer(self); self.timer.setSingleShot(True); self.timer.setInterval(120)
        self.timer.timeout.connect(self.populate); self.search.textChanged.connect(lambda: self.timer.start())

    def show_categories(self, categories):
        self.categories = categories
        self.groups.blockSignals(True); self.groups.clear()
        self.groups.addItems([row['name'] for row in categories]); self.groups.setCurrentRow(0)
        self.groups.blockSignals(False); self.populate()
        self.refresh.setEnabled(True)
        self.hint.setText(tr('分类来自立创商城，双击分类可查看商品。'))

    def populate(self, *_):
        query = self.search.text().strip().casefold()
        self.tree.clear(); self.groups.setEnabled(not query)
        def add(row, parent=None, path=''):
            full = (path + ' / ' if path else '') + row['name']
            item = QTreeWidgetItem([full if query else row['name']])
            item.setData(0, Qt.UserRole, dict(id=row['id'], name=row['name']))
            item.setToolTip(0, full)
            if not query or query in full.casefold():
                (parent.addChild(item) if parent else self.tree.addTopLevelItem(item))
            for child in row['children']:
                add(child, None if query else item, full)
            item.setExpanded(True)
        if query:
            for group in self.categories:
                for row in group['children']:
                    add(row, path=group['name'])
        elif 0 <= self.groups.currentRow() < len(self.categories):
            for row in self.categories[self.groups.currentRow()]['children']:
                add(row)
        self.update_choice()

    def update_choice(self):
        item = self.tree.currentItem()
        self.apply.setEnabled(bool(item and item.data(0, Qt.UserRole)['id']))
        self.choice.setText(item.toolTip(0) if item else tr('选择一个分类'))

    def choose(self):
        item = self.tree.currentItem()
        if item and item.data(0, Qt.UserRole)['id']:
            self.selected.emit(item.data(0, Qt.UserRole))


class FilterPanel(QWidget):
    back_requested = Signal()
    changed = Signal(object)
    applied = Signal(object)
    refresh_requested = Signal()

    def __init__(self):
        super().__init__()
        self.draft = normalize_filters(); self.facets = []; self.filling = False
        self.range_inputs = {}; self.matching_views = {}; self.displayed_key = None
        layout = QVBoxLayout(self); layout.setContentsMargins(0, 0, 0, 0)
        tools = QHBoxLayout()
        back = IconButton(tr('返回商品'), 'back'); back.clicked.connect(self.back_requested); tools.addWidget(back)
        self.hint = hint('筛选条件来自当前商品范围，可多选。'); tools.addWidget(self.hint, 1)
        self.refresh = IconButton(tr('更新选项'), 'refresh'); self.refresh.clicked.connect(self.refresh_requested)
        tools.addWidget(self.refresh); layout.addLayout(tools)
        scroll = QScrollArea(); scroll.setWidgetResizable(True); scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        content = QWidget(); content_layout = QVBoxLayout(content); content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSizeConstraint(QLayout.SetMinimumSize)
        scroll.setWidget(content); layout.addWidget(scroll, 1)
        card, form = surface(); form.setContentsMargins(12, 10, 12, 10)
        form.setSizeConstraint(QLayout.SetMinimumSize)
        flags = QHBoxLayout(); self.flags = {}
        for key, title in (('spotFilter', '现货'), ('discountFilter', '折扣'), ('hasDataFile', '数据手册')):
            box = QCheckBox(tr(title)); self.flags[key] = box; flags.addWidget(box)
            box.toggled.connect(self.common_changed)
        flags.addStretch()
        self.origin = QComboBox()
        for title, value in (('全部品牌', ''), ('国产品牌', '中国大陆$中国台湾'), ('国际品牌', '国际')):
            self.origin.addItem(tr(title), value)
        self.origin.currentIndexChanged.connect(self.common_changed); flags.addWidget(self.origin)
        form.addLayout(flags)
        grid = QGridLayout(); self.sort = QComboBox()
        for title, value in SORTS:
            self.sort.addItem(tr(title), value)
        self.sort.currentIndexChanged.connect(self.common_changed)
        grid.addWidget(self.sort, 0, 0, 1, 2)
        self.second = QLineEdit(); self.second.setMaxLength(160); self.second.setPlaceholderText(tr('在结果中搜索'))
        self.second.editingFinished.connect(self.common_changed); grid.addWidget(self.second, 0, 2, 1, 4)
        self.inputs = {}
        for col, (key, title) in enumerate((('startPrice', '最低单价'), ('endPrice', '最高单价'), ('demandNumber', '需求数量'))):
            edit = QLineEdit(); edit.setMaxLength(16); edit.setMinimumWidth(60)
            edit.setPlaceholderText(tr('不限')); edit.editingFinished.connect(self.common_changed)
            self.inputs[key] = edit; grid.addWidget(QLabel(tr(title)), 1, col * 2); grid.addWidget(edit, 1, col * 2 + 1)
        form.addLayout(grid)
        form.addWidget(hint('价格按需求数量所在梯度筛选；未填写数量时使用第一阶梯。'))
        content_layout.addWidget(card)
        card, body = surface(); body.setContentsMargins(8, 8, 8, 8)
        columns = QHBoxLayout()
        self.groups = QListWidget(); self.groups.setObjectName('catalogGroups'); self.groups.setMaximumWidth(200)
        self.groups.currentRowChanged.connect(self.show_options); columns.addWidget(self.groups, 1)
        right = QVBoxLayout()
        self.option_search = QLineEdit(); self.option_search.setPlaceholderText(tr('查找当前筛选项'))
        self.option_search.setClearButtonEnabled(True); self.option_search.textChanged.connect(self.filter_options)
        right.addWidget(self.option_search)
        self.range_widget = QWidget(); ranges = QHBoxLayout(self.range_widget); ranges.setContentsMargins(0, 0, 0, 0)
        self.low = QLineEdit(); self.low.setPlaceholderText(tr('最小值'))
        self.high = QLineEdit(); self.high.setPlaceholderText(tr('最大值'))
        self.low.setMaxLength(64); self.high.setMaxLength(64)
        self.unit = QComboBox()
        for widget in (self.low, self.high, self.unit):
            widget.setMinimumWidth(50); ranges.addWidget(widget)
        self.range_apply = QPushButton(tr('选用范围')); self.range_apply.clicked.connect(self.use_range); ranges.addWidget(self.range_apply)
        right.addWidget(self.range_widget)
        self.range_choice = hint(); right.addWidget(self.range_choice)
        self.options = QListWidget(); self.options.setObjectName('filterOptions'); self.options.setUniformItemSizes(True)
        self.options.itemChanged.connect(self.option_changed); right.addWidget(self.options, 1)
        local_actions = QHBoxLayout()
        self.select_visible = QPushButton(tr('勾选匹配项')); self.select_visible.clicked.connect(self.select_matching)
        local_actions.addWidget(self.select_visible)
        clear = QPushButton(tr('清除此项')); clear.clicked.connect(self.clear_group); local_actions.addWidget(clear)
        local_actions.addStretch(); right.addLayout(local_actions)
        columns.addLayout(right, 3); body.addLayout(columns)
        card.setMinimumHeight(280); content_layout.addWidget(card, 1)
        footer = QHBoxLayout()
        reset = QPushButton(tr('重置筛选')); reset.clicked.connect(self.reset); footer.addWidget(reset)
        self.summary = hint(); footer.addWidget(self.summary, 1)
        self.apply = QPushButton(tr('应用筛选')); self.apply.setObjectName('primary'); self.apply.clicked.connect(self.apply_filters)
        footer.addWidget(self.apply); layout.addLayout(footer)
        self.begin({}, [])

    def begin(self, filters, facets):
        self.filling = True; self.draft = normalize_filters(deepcopy(filters))
        self.range_inputs.clear(); self.matching_views.clear(); self.displayed_key = None
        for key, box in self.flags.items():
            box.setChecked(self.draft[key] if key == 'hasDataFile' else self.draft[key] == 0)
        self.origin.setCurrentIndex(max(0, self.origin.findData(self.draft['brandOriginFilter'])))
        self.sort.setCurrentIndex(max(0, self.sort.findData(self.draft['sortNumber'])))
        self.second.setText(self.draft['secondKeyword'])
        for key, edit in self.inputs.items():
            edit.setText(self.draft[key])
        self.filling = False; self.show_facets(facets)

    def current_group(self):
        row = self.groups.currentRow()
        return self.facets[row] if 0 <= row < len(self.facets) else None

    def selected_values(self, key):
        return self.draft['parameters'].get(key, []) if key.startswith('param_') else self.draft.get(key, '').split('$')

    def view_ranges(self, key):
        return self.draft['parameters'].get(key + '#range', []) or self.matching_views.get(key, {}).get('ranges', [])

    def show_facets(self, facets):
        current = (self.current_group() or {}).get('key')
        query, scroll = self.option_search.text(), self.options.verticalScrollBar().value()
        self.remember_range()
        keys = {group['key'] for group in facets}
        facets = list(facets) + [g for g in self.facets if g['key'] not in keys and
            (any(self.selected_values(g['key'])) or self.draft['parameters'].get(g['key'] + '#range')
             or any(self.range_inputs.get(g['key'], ('', '', ''))[:2]))]
        self.facets = facets
        self.groups.blockSignals(True); self.groups.clear()
        for group in facets:
            self.groups.addItem(group['label'])
        index = next((i for i, g in enumerate(facets) if g['key'] == current), 0)
        self.groups.setCurrentRow(index); self.groups.blockSignals(False)
        self.show_options(); self.update_summary()
        if (self.current_group() or {}).get('key') == current:
            self.option_search.setText(query); self.options.verticalScrollBar().setValue(scroll)

    def remember_range(self):
        if self.displayed_key:
            self.range_inputs[self.displayed_key] = self.low.text(), self.high.text(), self.unit.currentText()

    def show_options(self, *_):
        self.remember_range()
        self.filling = True; self.options.clear(); self.option_search.clear(); self.low.clear(); self.high.clear(); self.unit.clear()
        group = self.current_group()
        self.displayed_key = (group or {}).get('key')
        ranges = self.view_ranges((group or {}).get('key', ''))
        self.range_choice.setText(tr('已选范围：') + ' / '.join(ranges) if ranges else '')
        self.range_choice.setVisible(bool(ranges))
        self.range_widget.setVisible(bool(group and group.get('numeric') and group.get('units')))
        if group:
            key = group['key']; selected = self.selected_values(key); values = list(group['options'])
            present = {row['value'] for row in values}
            values += [dict(value=v, label=v, enabled=True) for v in selected if v and v not in present]
            for row in values:
                count = row.get('count'); label = row['label'] + (f'  ({count:,})' if count is not None else '')
                item = QListWidgetItem(label); item.setData(Qt.UserRole, row['value']); item.setToolTip(row['label'])
                item.setData(Qt.UserRole + 1, row)
                item.setFlags(Qt.ItemIsSelectable | Qt.ItemIsUserCheckable | Qt.ItemIsEnabled)
                if row.get('enabled') is False and row['value'] not in selected:
                    item.setFlags(item.flags() & ~Qt.ItemIsEnabled)
                item.setCheckState(Qt.Checked if row['value'] in selected else Qt.Unchecked); self.options.addItem(item)
            for unit in group.get('units', {}):
                self.unit.addItem(unit)
            fields = self.range_inputs.get(key)
            if fields is None and ranges:
                fields = range_input(ranges[0], group.get('units', {}))
            if fields:
                low, high, unit = fields
                self.low.setText(low); self.high.setText(high)
                if unit and self.unit.findText(unit) < 0:
                    self.unit.addItem(unit)
                self.unit.setCurrentText(unit)
        self.filling = False; self.filter_options()

    def filter_options(self, *_):
        if self.filling:
            return
        query = self.option_search.text().casefold()
        group = self.current_group() or {}
        ranges = self.view_ranges(group.get('key', ''))
        bounds = range_bounds(ranges[0], group.get('units', {})) if ranges else None
        for index in range(self.options.count()):
            item = self.options.item(index)
            item.setHidden(query not in item.text().casefold() or bounds is not None and
                           not option_in_range(item.data(Qt.UserRole + 1) or {}, bounds))

    def set_values(self, key, values, *, preserve_view=False):
        if key.startswith('param_'):
            if preserve_view:
                self.remember_range()
                # A batch selection submits discrete values, while its local
                # range remains available for narrowing and editing the list.
                self.matching_views[key] = dict(inputs=self.range_inputs.get(key), ranges=self.view_ranges(key))
            self.draft['parameters'].pop(key + '#range', None)
            self.draft['parameters'][key] = values
            if key not in self.matching_views:
                self.range_inputs.pop(key, None)
                if self.displayed_key == key:
                    self.low.clear(); self.high.clear(); self.range_choice.clear(); self.range_choice.hide()
                    self.filter_options()
        else:
            self.draft[key] = '$'.join(values)
            if key == 'catalogIdFilter':
                self.draft['parameters'] = {}
                self.range_inputs.clear(); self.matching_views.clear()
        self.draft['lastParamName'] = key

    def option_changed(self, *_, preserve_view=False):
        if self.filling or not self.current_group():
            return
        self.set_values(self.current_group()['key'], [self.options.item(i).data(Qt.UserRole) for i in range(self.options.count())
                                                    if self.options.item(i).checkState() == Qt.Checked], preserve_view=preserve_view)
        self.emit_change()

    def common_changed(self, *_):
        if self.filling:
            return
        for key, box in self.flags.items():
            self.draft[key] = box.isChecked() if key == 'hasDataFile' else 0 if box.isChecked() else 1
        self.draft.update(brandOriginFilter=self.origin.currentData() or '', sortNumber=self.sort.currentData() or 0,
                          secondKeyword=self.second.text().strip())
        for key, edit in self.inputs.items():
            self.draft[key] = edit.text().strip()
        self.emit_change()

    def emit_change(self):
        try:
            value = normalize_filters(self.draft)
        except StoreError as error:
            # Keep Apply reachable: after correcting an input the next click
            # validates again, even if editingFinished has not fired yet.
            self.hint.setText(str(error)); return
        self.apply.setEnabled(True); self.update_summary(); self.changed.emit(value)

    def update_summary(self):
        try:
            count = filter_count(self.draft)
        except StoreError:
            return
        selected = [g['label'] for g in self.facets if any(self.selected_values(g['key'])) or self.draft['parameters'].get(g['key'] + '#range')]
        self.summary.setText(msg('已设置 {0} 项', count) + (' · ' + ' / '.join(selected) if selected else ''))

    def select_matching(self):
        self.filling = True; count = 0
        for index in range(self.options.count()):
            item = self.options.item(index)
            if not item.isHidden() and item.flags() & Qt.ItemIsEnabled:
                item.setCheckState(Qt.Checked); count += 1
        self.filling = False
        if count:
            self.option_changed(preserve_view=True)
        log_event('INFO', 'catalog.matching_selected', matching_count=count,
                  lower_bound=bool(self.low.text()), upper_bound=bool(self.high.text()))

    def clear_group(self):
        group = self.current_group()
        if group:
            self.matching_views.pop(group['key'], None)
            self.set_values(group['key'], []); self.show_options(); self.emit_change()

    def use_range(self):
        group = self.current_group(); low, high, unit = self.low.text().strip(), self.high.text().strip(), self.unit.currentText()
        if not group or not group['key'].startswith('param_') or not unit or not (low or high):
            return True
        try:
            values = [finite_number(value) for value in (low, high) if value]
            if any(v is None or v.copy_abs() > Decimal('1e18') for v in values) or low and high and Decimal(low) > Decimal(high):
                raise ValueError()
        except (InvalidOperation, ValueError):
            self.hint.setText(tr('请输入有效数值范围，最小值不能大于最大值。'))
            log_event('WARNING', 'catalog.range_rejected', reason='invalid_bounds')
            return False
        self.draft['parameters'].pop(group['key'], None)
        self.matching_views.pop(group['key'], None)
        self.draft['parameters'][group['key'] + '#range'] = [(low + unit if low else '') + '~' + (high + unit if high else '')]
        self.draft['lastParamName'] = group['key']; self.show_options(); self.emit_change()
        log_event('INFO', 'catalog.range_selected', lower_bound=bool(low), upper_bound=bool(high),
                  option_count=self.options.count(), matching_count=sum(not self.options.item(i).isHidden()
                                                                       for i in range(self.options.count())))
        return True

    def reset(self):
        # Keep a browsed category so resetting does not turn into an empty query.
        self.begin({'catalogIdFilter': self.draft['catalogIdFilter']}, self.facets); self.emit_change()

    def apply_filters(self):
        group = self.current_group() or {}
        view = self.matching_views.get(group.get('key'))
        fields = self.low.text(), self.high.text(), self.unit.currentText()
        # Unchanged range fields describe the view used for batch selection;
        # do not replace the selected discrete values when Apply is clicked.
        if (not view or fields != view['inputs']) and not self.use_range():
            return
        self.common_changed()
        try:
            value = normalize_filters(self.draft)
        except StoreError as error:
            self.hint.setText(str(error)); return
        self.applied.emit(value)
