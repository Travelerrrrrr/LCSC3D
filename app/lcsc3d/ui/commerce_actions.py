"""Shopping actions shared by search, favorites and the download table."""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget, QMenu
from shiboken6 import isValid

from lcsc3d.core.app_logging import log_event, record_error, safe_part
from lcsc3d.core.i18n import text as tr, message as msg, render
from lcsc3d.services.store import StoreError, SessionExpired, normalize_items, check_cancelled
from lcsc3d.services.commerce import (CART_URL, ORDERS_URL, add_to_cart, session_cookies, apply_browser_cookies)
from lcsc3d.ui.localized_widgets import QLabel, QPushButton
from lcsc3d.ui.commerce import CommerceBrowser, CartQuantityDialog
from lcsc3d.ui.shell_ui import notify


class CommerceActions:
    def setup_commerce(self):
        self.commerce_browser = None
        self.commerce_starting = False
        self.cart_dialog = None
        self.cart_busy = False
        self.commerce_panels = []
        for title in ('购物车', '我的订单'):
            panel = QWidget()
            layout = QVBoxLayout(panel)
            layout.setContentsMargins(0, 0, 0, 0)
            hint = QLabel(tr('登录商城账号后，即可查看购物车与订单。'))
            hint.setWordWrap(True)
            hint.setObjectName('muted')
            layout.addWidget(hint)
            login = QPushButton(tr('账号登录'))
            login.setMaximumWidth(180)
            login.setObjectName('primary')
            login.clicked.connect(self.open_login)
            layout.addWidget(login)
            layout.addStretch()
            self.commerce_panels.append(panel)
            self.tabs.addTab(panel, tr(title))

    def commerce_tab_changed(self):
        index = self.tabs.currentIndex()
        shopping = index >= 2
        self.inspector.setVisible(not shopping)
        self.product_actions.setVisible(not shopping)
        if not shopping:
            return
        self.clear_detail()
        if self.international:
            for panel in self.commerce_panels:
                panel.layout().itemAt(0).widget().setText('Cart and orders are managed on LCSC.com.')
            return
        if not self.client.account or self.commerce_starting:
            return
        target = CART_URL if index == 2 else ORDERS_URL
        if self.commerce_browser is not None and not self.commerce_browser.ready and not self.commerce_browser.sync_timer.isActive():
            self.reset_commerce()
        if self.commerce_browser is not None:
            self.attach_commerce_browser()
            self.commerce_browser.navigate(target)
            return
        self.commerce_starting = True
        client = self.client
        self.status.setText(tr('正在打开商城页面…'))
        self.jobs.start('commerce-session', lambda stop, progress: session_cookies(client),
                        self.commerce_ready, self.commerce_failed)

    def commerce_ready(self, cookies):
        self.commerce_starting = False
        if self.closing or not self.client.account or self.tabs.currentIndex() < 2:
            return
        self.commerce_browser = CommerceBrowser(self)
        # Defer destruction until the current Chromium navigation callback ends.
        self.commerce_browser.login_required.connect(self.commerce_login_required, Qt.QueuedConnection)
        self.attach_commerce_browser()
        self.commerce_browser.initialize(cookies, CART_URL if self.tabs.currentIndex() == 2 else ORDERS_URL)
        self.status.setText(tr('购物车内勾选商品后点击「去结算」，核对订单信息并确认下单。'))

    def commerce_login_required(self):
        if self.closing or self.commerce_browser is None:
            return
        self.clear_session()
        self.status.setText(tr('商城登录已失效，请重新登录。'))
        self.open_login()

    def attach_commerce_browser(self):
        panel = self.commerce_panels[self.tabs.currentIndex() - 2]
        for current in self.commerce_panels:
            for index in (0, 1):
                current.layout().itemAt(index).widget().setVisible(current is not panel)
        panel.layout().addWidget(self.commerce_browser, 1)
        self.commerce_browser.show()

    def commerce_failed(self, error):
        self.commerce_starting = False
        self.status.setText(str(error))

    def reset_commerce(self):
        self.jobs.cancel('commerce-session')
        self.commerce_starting = False
        if self.commerce_browser is not None:
            self.commerce_browser.dispose()
            self.commerce_browser = None
        for panel in self.commerce_panels:
            for index in (0, 1):
                panel.layout().itemAt(index).widget().show()

    def context_products(self, table, row):
        product = table.item(row, 1).data(Qt.UserRole)
        if table.item(row, 0).checkState() == Qt.Checked:
            return self.selected_items()
        return [product]

    def product_context_menu(self, table, position):
        row = table.rowAt(position.y())
        if row < 0:
            return
        table.setCurrentCell(row, 1)
        products = self.context_products(table, row)
        menu = self.make_product_menu(products, table)
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
        if self.international:
            self.open_login()
            return
        if not products or self.collecting:
            return
        if not self.client.account:
            self.status.setText(tr('请先登录，登录后重新选择此操作。'))
            self.open_login()
            return
        self.collecting = True
        self.update_collect_button()
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
        self.start_thumbnails(self.favorite_table)
        self.filter_favorites()
        self.update_collect_button()
        self.save_session()
        self.status.setText(msg('收藏完成 {0} 项，失败 {1} 项。', len(added), len(errors)) +
                            (' ' + errors[0][1] if errors else ''))
        notify(self, tr('我的收藏'), self.status.text(), severity='warning' if errors else 'success')

    def add_cart_products(self, products):
        if self.international:
            self.open_login()
            return
        if not products or self.cart_busy:
            return
        if not self.client.account:
            self.status.setText(tr('请先登录，登录后重新选择此操作。'))
            self.open_login()
            return
        if self.cart_dialog is not None and isValid(self.cart_dialog) and self.cart_dialog.isVisible():
            self.cart_dialog.raise_()
            return
        products = list({p['part']: dict(p) for p in products}.values())
        dialog = self.cart_dialog = CartQuantityDialog(products, self)
        dialog.submitted.connect(self.submit_cart)
        dialog.show()

    def submit_cart(self, rows):
        if self.cart_busy or not self.client.account:
            return
        self.cart_busy = True
        self.update_count()
        client, dialog = self.client, self.cart_dialog
        cookies = list(self.commerce_browser.cookies.values()) if self.commerce_browser else []
        def add(stop, progress):
            apply_browser_cookies(client, cookies)
            client.account_info(stop)
            added, failed = [], []
            for index, (product, amount) in enumerate(rows):
                check_cancelled(stop)
                try:
                    added.append(add_to_cart(client, product, amount, cancelled=stop))
                except StoreError as exc:
                    # Stop after an uncertain write; never replay completed rows.
                    failed.append((product['part'], str(exc)))
                    return dict(added=added, failed=failed, unattempted=rows[index + 1:])
            return dict(added=added, failed=failed, unattempted=[])
        self.jobs.start('cart-add', add, lambda result: self.cart_completed(dialog, result),
                        lambda error: self.cart_failed(dialog, error))

    def cart_completed(self, dialog, result):
        self.cart_busy = False
        self.update_count()
        if isValid(dialog):
            dialog.completed(result)
        added, failed = len(result['added']), len(result['failed'])
        self.status.setText(msg('加购成功 {0} 项；未确认 {1} 项；未发送 {2} 项。', added, failed, len(result['unattempted'])))
        notify(self, tr('购物车'), self.status.text(), severity='warning' if failed else 'success',
               actions=((tr('查看购物车'), lambda: (dialog.accept() if isValid(dialog) else None, self.tabs.setCurrentIndex(2))),))
        log_event('INFO' if not failed else 'WARNING', 'cart.batch_completed', count=added, failed_count=failed,
                  pending_count=len(result['unattempted']))
        self.save_session()

    def cart_failed(self, dialog, error):
        self.cart_busy = False
        self.update_count()
        if isValid(dialog):
            dialog.status.setText(str(error) + '\n' + tr('请先核对购物车再重试，避免重复增加数量。'))
        self.status.setText(str(error))
        if isinstance(error, SessionExpired):
            self.clear_session()
