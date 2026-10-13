"""Native shopping jobs and inline quantity actions."""
from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QMenu

from lcsc3d.core.app_logging import log_event, record_error, safe_part
from lcsc3d.core.i18n import text as tr, message as msg, render
from lcsc3d.services.store import StoreError, SessionExpired, normalize_items, check_cancelled, quantity
from lcsc3d.services.commerce import (add_to_cart, read_cart, update_quantity, remove_items,
                                      prepare_checkout, read_orders, CART_URL, ORDERS_URL, CHECKOUT_URL)
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
        self.cart_panel.checkout_requested.connect(self.checkout_cart)
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
        for channel in ('cart-read', 'cart-write', 'cart-checkout', 'orders-read'):
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

    def checkout_cart(self):
        if not self.cart_panel.checkout.isEnabled() or not self.require_commerce_account():
            return
        rows = [dict(row) for row in self.cart_panel.selected_rows()]
        self.cart_mutating = True; self.cart_panel.set_busy(True)
        self.cart_panel.hint.setText(tr('正在提交所选商品…'))
        client = self.client
        self.jobs.start('cart-checkout', lambda stop, progress: prepare_checkout(client, rows, cancelled=stop),
                        self.checkout_prepared, self.cart_write_failed)

    def checkout_prepared(self, result):
        self.cart_mutating = False; self.cart_panel.set_busy(False)
        self.cart_panel.hint.setText(tr('所选商品已提交，请在外部浏览器使用同一商城账号继续。'))
        self.open_commerce_link(result['url'], 'checkout')
        self.orders_panel.loaded = False

    def open_commerce_link(self, url, kind):
        # Only URLs constructed from trusted store routes, never a response redirect.
        valid = url in (CART_URL, ORDERS_URL, CHECKOUT_URL) or url.startswith('https://member.szlcsc.com/member/order/details.html?uuid=')
        if not valid:
            return
        opened = QDesktopServices.openUrl(QUrl(url))
        log_event('INFO' if opened else 'WARNING', 'commerce.external_open', kind=kind, opened=opened)
        if not opened:
            notify(self, tr('浏览器未打开'), tr('已提交的操作无需重复，点击按钮重新打开浏览器。'), severity='warning',
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
        rows = [(p, p.get('purchase_quantity', self.purchase_quantities.get(p['part'], quantity(p.get('min_quantity')) or 1))) for p in products]
        self.submit_cart(rows)

    def submit_cart(self, rows):
        if self.cart_busy or not self.client.account:
            return
        self.cart_busy = True; self.update_count()
        self.status.setText(tr('正在加入购物车…'))
        client = self.client
        def add(stop, progress):
            client.account_info(stop)
            added = []
            for index, (product, amount) in enumerate(rows):
                check_cancelled(stop)
                try:
                    added.append(add_to_cart(client, product, amount, cancelled=stop))
                except StoreError as exc:
                    return dict(added=added, failed=[(product['part'], str(exc))], unattempted=rows[index + 1:])
            return dict(added=added, failed=[], unattempted=[])
        self.jobs.start('cart-add', add, self.cart_completed, self.cart_failed)

    def cart_completed(self, result):
        self.cart_busy = False; self.update_count()
        added, failed = len(result['added']), len(result['failed'])
        self.cart_panel.loaded = False
        self.status.setText(msg('加购成功 {0} 项；未确认 {1} 项；未发送 {2} 项。', added, failed, len(result['unattempted'])))
        if failed:
            self.status.setText(self.status.text() + ' ' + tr('请先核对购物车再重试，避免重复增加数量。'))
        notify(self, tr('购物车'), self.status.text(), severity='warning' if failed else 'success',
               actions=((tr('查看购物车'), lambda: self.tabs.setCurrentIndex(2)),))
        log_event('INFO' if not failed else 'WARNING', 'cart.batch_completed', count=added, failed_count=failed,
                  pending_count=len(result['unattempted']))
        self.save_session()
        if self.tabs.currentIndex() == 2:
            self.load_cart()

    def cart_failed(self, error):
        self.cart_busy = False; self.update_count()
        self.status.setText(str(error))
        if isinstance(error, SessionExpired):
            self.clear_session()
        notify(self, tr('加购失败'), str(error), severity='warning')
