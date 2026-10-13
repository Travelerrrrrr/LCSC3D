"""Native shopping jobs and inline quantity actions."""
from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QMenu

from lcsc3d.core.app_logging import log_event, record_error, safe_part
from lcsc3d.core.i18n import text as tr, message as msg, render
from lcsc3d.services.store import StoreError, SessionExpired, normalize_items, check_cancelled, quantity
from lcsc3d.services.commerce import (add_cart_batch, read_cart, update_quantity, remove_items,
                                      read_orders, CART_URL, ORDERS_URL, REJECTION_REASONS)
from lcsc3d.ui.commerce import CartPanel, OrdersPanel
from lcsc3d.ui.shell_ui import notify


class CommerceActions:
    def setup_commerce(self):
        self.cart_busy = self.cart_loading = self.cart_mutating = self.orders_loading = False
        self.quantity_updates = {}
        self.quantity_timer = QTimer(self)
        self.quantity_timer.setSingleShot(True)
        self.quantity_timer.timeout.connect(self.save_cart_quantities)
        self.cart_panel = CartPanel()
        self.orders_panel = OrdersPanel()
        self.tabs.addTab(self.cart_panel, tr('购物车'))
        self.tabs.addTab(self.orders_panel, tr('我的订单'))
        self.cart_panel.refresh_requested.connect(self.load_cart)
        self.cart_panel.quantity_changed.connect(self.cart_quantity_changed)
        self.cart_panel.open_cart_requested.connect(lambda: self.open_commerce_link(CART_URL, 'cart'))
        self.cart_panel.remove_requested.connect(self.remove_cart_selection)
        self.cart_panel.download_requested.connect(lambda rows: self.import_requested.emit(normalize_items(rows)) if self.import_enabled else None)
        self.orders_panel.read_requested.connect(self.load_orders)
        self.orders_panel.open_requested.connect(lambda url: self.open_commerce_link(url, 'order'))

    def commerce_tab_changed(self):
        shopping = self.tabs.currentIndex() >= 2
        self.inspector.setVisible(not shopping)
        self.product_actions.setVisible(not shopping)
        if not shopping:
            return
        self.clear_detail()
        if self.international:
            self.cart_panel.hint.setText('International cart and orders are managed on LCSC.com.')
            self.orders_panel.hint.setText('International cart and orders are managed on LCSC.com.')
            return
        if not self.client.account:
            return
        if self.tabs.currentIndex() == 2 and not self.cart_panel.loaded:
            self.load_cart()
        elif self.tabs.currentIndex() == 3 and not self.orders_panel.loaded:
            self.load_orders(1)

    def require_commerce_account(self):
        if self.international or not self.client.account:
            self.status.setText(tr('请先登录，登录后重新选择此操作。'))
            self.open_login()
            return False
        return True

    def reset_commerce(self):
        self.quantity_timer.stop(); self.quantity_updates.clear()
        for channel in ('cart-read', 'cart-write', 'orders-read'):
            self.jobs.cancel(channel)
        self.cart_loading = self.cart_mutating = self.orders_loading = False
        self.cart_panel.clear(); self.orders_panel.clear()

    def load_cart(self):
        if self.cart_loading or self.cart_mutating or not self.require_commerce_account():
            return
        self.cart_loading = True
        self.quantity_timer.stop(); self.quantity_updates.clear()
        self.cart_panel.set_busy(True)
        self.cart_panel.hint.setText(tr('正在读取购物车…'))
        client = self.client
        self.jobs.start('cart-read', lambda stop, progress: read_cart(client, stop), self.cart_loaded, self.cart_read_failed)

    def cart_loaded(self, rows):
        self.cart_loading = self.cart_mutating = False
        self.cart_panel.show_rows(rows)

    def cart_read_failed(self, error):
        self.cart_loading = self.cart_mutating = False
        self.cart_panel.stale = True
        self.cart_panel.uncertain = True
        self.cart_panel.set_busy(False)
        self.cart_panel.hint.setText(str(error))
        if isinstance(error, SessionExpired):
            self.clear_session()
            self.status.setText(str(error))

    def cart_quantity_changed(self, row, amount):
        if amount == row['quantity']:
            self.quantity_updates.pop(row['key'], None)
        else:
            self.quantity_updates[row['key']] = (dict(row), amount)
        self.cart_panel.stale = self.cart_panel.uncertain or bool(self.quantity_updates)
        self.cart_panel.update_summary()
        self.quantity_timer.start(300)

    def save_cart_quantities(self):
        if not self.quantity_updates or self.cart_mutating or self.closing:
            return
        rows = list(self.quantity_updates.values()); self.quantity_updates.clear()
        self.cart_mutating = True; self.cart_panel.set_busy(True)
        self.cart_panel.hint.setText(tr('正在保存数量…'))
        client = self.client
        def save(stop, progress):
            for row, amount in rows:
                update_quantity(client, row, amount, cancelled=stop)
            return read_cart(client, stop)
        self.jobs.start('cart-write', save, self.cart_loaded, self.cart_write_failed)

    def cart_write_failed(self, error):
        self.cart_read_failed(error)
        message = tr('操作未确认，请刷新购物车核对后再试；不会自动重复提交。')
        self.status.setText(message + ' ' + str(error))
        self.cart_panel.hint.setText(self.status.text())

    def remove_cart_selection(self):
        rows = [dict(row) for row in self.cart_panel.selected_rows()]
        account_client = self.client
        if not rows or self.cart_mutating:
            return
        def remove():
            if self.client is not account_client or not self.client.account or self.cart_mutating:
                return
            self.cart_mutating = True; self.cart_panel.set_busy(True)
            client = self.client
            def perform(stop, progress):
                remove_items(client, rows, cancelled=stop)
                return read_cart(client, stop)
            self.jobs.start('cart-write', perform, self.cart_loaded, self.cart_write_failed)
        notify(self, tr('移除购物车商品'), msg('从购物车移除选中的 {0} 项？', len(rows)),
               actions=((tr('移除'), remove),), persistent=True)

    def open_commerce_link(self, url, kind):
        # Only URLs constructed from trusted store routes, never a response redirect.
        valid = url in (CART_URL, ORDERS_URL) or url.startswith('https://member.szlcsc.com/member/order/details.html?uuid=')
        if not valid:
            return
        opened = QDesktopServices.openUrl(QUrl(url))
        log_event('INFO' if opened else 'WARNING', 'commerce.external_open', kind=kind, opened=opened)
        if not opened:
            notify(self, tr('浏览器未打开'), tr('请点击按钮重新打开浏览器。'), severity='warning',
                   actions=((tr('打开浏览器'), lambda: self.open_commerce_link(url, kind)),))

    def load_orders(self, page=1):
        if self.orders_loading or page < 1 or not self.require_commerce_account():
            return
        self.orders_loading = True; self.orders_panel.set_busy(True)
        self.orders_panel.hint.setText(tr('正在读取订单…'))
        client = self.client
        keyword, status = self.orders_panel.keyword.text(), self.orders_panel.filter.currentData()
        self.jobs.start('orders-read', lambda stop, progress: read_orders(client, page, keyword=keyword, status=status, cancelled=stop),
                        self.orders_loaded, self.orders_failed)

    def orders_loaded(self, result):
        self.orders_loading = False
        self.orders_panel.show_result(result)

    def orders_failed(self, error):
        self.orders_loading = False; self.orders_panel.set_busy(False)
        self.orders_panel.hint.setText(str(error))
        if isinstance(error, SessionExpired):
            self.clear_session()
            self.status.setText(str(error))

    def context_products(self, table, row):
        product = table.item(row, 1).data(Qt.UserRole)
        return self.selected_items() if table.item(row, 0).checkState() == Qt.Checked else [product]

    def product_context_menu(self, table, position):
        row = table.rowAt(position.y())
        if row < 0:
            return
        table.setCurrentCell(row, 1)
        menu = self.make_product_menu(self.context_products(table, row), table)
        menu.exec(table.viewport().mapToGlobal(position))

    def make_product_menu(self, products, parent):
        menu = QMenu(parent)
        download = menu.addAction(render(tr('添加到下载列表')))
        download.setEnabled(self.import_enabled and bool(products))
        download.triggered.connect(lambda: self.import_requested.emit(normalize_items(products)))
        favorite = menu.addAction(render(tr('添加到收藏')))
        favorite.setEnabled(bool(products) and not self.collecting)
        favorite.triggered.connect(lambda: self.collect_products(products))
        cart = menu.addAction(render(tr('添加到购物车')))
        cart.setEnabled(bool(products) and not self.cart_busy)
        cart.triggered.connect(lambda: self.add_cart_products(products))
        log_event('DEBUG', 'store.context_menu', count=len(products))
        return menu

    def collect_products(self, products):
        if not products or self.collecting or not self.require_commerce_account():
            return
        self.collecting = True; self.update_collect_button()
        client = self.client
        products = [dict(p) for p in products]
        def collect(stop, progress):
            added, errors = [], []
            for product in products:
                check_cancelled(stop)
                try:
                    if not product.get('product_id'):
                        product = client.product(product, stop) or product
                    added.append(client.add_favorite(product, stop))
                except StoreError as exc:
                    errors.append((product['part'], str(exc)))
                    record_error(exc, 'favorites.batch_item_failed', part=safe_part(product['part']))
                    if isinstance(exc, SessionExpired):
                        break
            return added, errors
        self.jobs.start('collect', collect, self.products_collected, self.favorite_add_failed)

    def products_collected(self, result):
        added, errors = result
        self.collecting = False
        for item in added:
            product = item['product']
            if product['part'] not in self.items:
                self.items[product['part']] = product
                self.append_row(self.favorite_table, product)
        self.start_thumbnails(self.favorite_table); self.filter_favorites(); self.update_collect_button(); self.save_session()
        self.status.setText(msg('收藏完成 {0} 项，失败 {1} 项。', len(added), len(errors)) + (' ' + errors[0][1] if errors else ''))
        notify(self, tr('我的收藏'), self.status.text(), severity='warning' if errors else 'success')

    def add_cart_products(self, products):
        if not products or self.cart_busy or not self.require_commerce_account():
            return
        # Interpret any active editor before taking a snapshot for this write.
        for table in (self.search_table, self.favorite_table):
            for index in range(table.rowCount()):
                table.cellWidget(index, table.quantity_column).interpretText()
        products = list({p['part']: dict(p) for p in products}.values())
        for product in products:
            if self.purchase_rule_sources.get(product['part']):
                product['min_quantity'], product['quantity_step'] = self.purchase_rules[product['part']]
                product['purchase_rules_known'] = True
        rows = [(p, p.get('purchase_quantity', self.purchase_quantities.get(p['part'], quantity(p.get('min_quantity')) or 1))) for p in products]
        self.submit_cart(rows)

    def submit_cart(self, rows):
        if self.cart_busy or not self.client.account:
            return
        self.cart_busy = True; self.update_count()
        self.status.setText(tr('正在加入购物车…'))
        client = self.client
        self.jobs.start('cart-add', lambda stop, progress: add_cart_batch(client, rows, cancelled=stop),
                        self.cart_completed, self.cart_failed)

    def cart_completed(self, result):
        self.cart_busy = False
        rows = result['items']
        resolved = {product['part']: product for product in result['products']}
        for part, product in resolved.items():
            self.set_purchase_rules(product)
            self.set_purchase_quantity(part, self.purchase_quantities.get(part, 1))
        counts = {status: sum(row['status'] == status for row in rows)
                  for status in ('added', 'rejected', 'unconfirmed', 'not_sent')}
        # Keep failed rows selected, so repeating the action does not also add
        # the successfully handled products a second time.
        added_parts = {row['part'] for row in rows if row['status'] == 'added'}
        stopped_parts = {row['part'] for row in rows if row['status'] == 'rejected' and row['reason'] == 'unavailable'}
        for part in added_parts:
            self.search_selected.pop(part, None); self.favorite_checked.discard(part)
        for table in (self.search_table, self.favorite_table):
            table.blockSignals(True)
            for index in range(table.rowCount()):
                part = table.item(index, 1).text()
                if part in resolved:
                    product = table.item(index, 1).data(Qt.UserRole)
                    product.update(resolved[part])
                    table.item(index, 1).setData(Qt.UserRole, product)
                    if part in self.search_selected:
                        self.search_selected[part] = product
                if part in added_parts:
                    table.item(index, 0).setCheckState(Qt.Unchecked)
                if part in stopped_parts:
                    product = table.item(index, 1).data(Qt.UserRole)
                    product['purchase_status'] = 'unavailable'
                    table.item(index, 1).setData(Qt.UserRole, product)
                    if part in self.search_selected:
                        self.search_selected[part] = product
                    self.show_purchase_availability(table, index, product)
            table.blockSignals(False)
        if result['session_expired']:
            self.clear_session()
        self.update_count()
        self.cart_panel.loaded = False
        summary = msg('加购成功 {0} 项；失败 {1} 项；未确认 {2} 项；未发送 {3} 项。',
                      counts['added'], counts['rejected'], counts['unconfirmed'], counts['not_sent'])
        reason_text = {**REJECTION_REASONS,
            'session_expired': '登录已失效，请重新登录后再试。',
            'request_unconfirmed': '请求结果未确认，请先核对购物车，避免重复加购。',
            'previous_unconfirmed': '前一项结果未确认，已停止发送。',
            'account_check_failed': '账号校验未完成，本项未发送。'}
        labels = {'added': tr('成功'), 'rejected': tr('失败'), 'unconfirmed': tr('未确认'), 'not_sent': tr('未发送')}
        lines, issues = [], []
        for row in rows:
            line = msg('{0} × {1}：{2}', row['part'], row['quantity'], labels[row['status']])
            if row['reason']:
                rules = resolved.get(row['part'])
                if row['reason'] == 'quantity_rule' and rules and rules.get('min_quantity') and rules.get('quantity_step'):
                    line += ' · ' + msg('起订量 {0}，递增量 {1}；已更新数量，请确认后再加购。', rules['min_quantity'], rules['quantity_step'])
                else:
                    line += ' · ' + tr(reason_text.get(row['reason'], '请求结果未确认，请先核对购物车，避免重复加购。'))
            if row['service_code'] is not None:
                line += ' ' + msg('（错误码 {0}）', row['service_code'])
            lines.append(render(line))
            if row['status'] != 'added':
                issues.append(render(line))
        # A compact toast, with the complete per-item result in Messages.
        preview = '\n'.join(issues[:2])
        self.status.setText(summary + (' ' + issues[0] if issues else ''))
        actions = [(tr('查看购物车'), lambda: self.tabs.setCurrentIndex(2))]
        if self.app_window is not None and hasattr(self.app_window, 'open_messages'):
            actions.append((tr('查看结果'), self.app_window.open_messages))
        notify(self, tr('购物车'), summary + ('\n' + preview if preview else ''),
               severity='warning' if issues else 'success', actions=tuple(actions), details='\n'.join(lines))
        log_event('INFO', 'cart.batch_presented', count=len(rows))
        if not result['session_expired']:
            self.save_session()
        if self.tabs.currentIndex() == 2 and not result['session_expired']:
            self.load_cart()

    def cart_failed(self, error):
        self.cart_busy = False; self.update_count()
        self.status.setText(str(error))
        if isinstance(error, SessionExpired):
            self.clear_session()
        notify(self, tr('加购失败'), str(error), severity='warning')
