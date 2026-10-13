"""Final EXE: native cart, inline quantities, orders and external cart link stub."""
import json
from pathlib import Path
import sys
import time

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QApplication
from PySide6.QtWebEngineWidgets import QWebEngineView
from lcsc3d.core.app_logging import log_event, get_log_directory
from lcsc3d.services.log_support import package_logs, clear_logs
from lcsc3d.services.commerce import CART_URL
from lcsc3d.selftests.favorites_selftest import OfflineStore, fixture_store_record
from lcsc3d.ui import commerce_actions
from lcsc3d.ui.favorites import FavoritesDialog


def start(window, folder):
    main = sys.modules[type(window).__module__]
    root = Path(folder).resolve(); root.mkdir(parents=True, exist_ok=True)
    report = dict(version=main.VERSION, frozen=bool(getattr(sys, 'frozen', False)))
    state = dict(mode='catalog-start', deadline=time.monotonic() + 60, done=False)
    service = OfflineStore(); client = service.client()
    service.cart_steps = {'C2861386': 5, 'C507270': 10}
    client.session.request('https://www.szlcsc.com/cas/login')
    client.account = client.account_info()
    window.request_component_info = lambda: None
    window.show_preview = lambda *args, **kw: None
    original_desktop = commerce_actions.QDesktopServices
    opened = []
    class Desktop:
        @staticmethod
        def openUrl(url):
            opened.append(url.toString())
            return True
    commerce_actions.QDesktopServices = Desktop
    dialog = window.bind_store(FavoritesDialog(window, client_factory=lambda: client, vault=False))
    dialog.request_detail = lambda product: None
    timer = QTimer(window); timer.setInterval(25)

    def finish(error=''):
        if state['done']:
            return
        state['done'] = True; timer.stop()
        report.update(success=not error, error=error, stage=state['mode'])
        (root / 'commerce-verification.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), 'utf-8')
        commerce_actions.QDesktopServices = original_desktop
        dialog.shutdown()
        def close():
            if dialog.has_jobs():
                QTimer.singleShot(50, close)
                return
            service.close(); window.close(); QApplication.instance().exit(1 if error else 0)
        close()

    def tick():
        try:
            if time.monotonic() > state['deadline']:
                raise AssertionError('Native commerce verification timed out')
            mode = state['mode']
            if mode == 'catalog-start':
                window.open_favorites(); dialog.open_categories(); state['mode'] = 'categories'
            elif mode == 'categories' and dialog.categories:
                assert dialog.search_content.currentWidget() is dialog.category_panel
                assert not dialog.category_panel.isWindow()
                window.grab().save(str(root / 'native-categories.png'))
                dialog.browse_category({'id': '11329', 'name': '单片机(MCU/MPU/SOC)'})
                state['mode'] = 'category-results'
            elif mode == 'category-results' and not dialog.searching and dialog.search_items:
                dialog.open_filters(); state['mode'] = 'filter-options'
            elif mode == 'filter-options' and dialog.filter_cache:
                panel = dialog.filter_panel
                assert dialog.search_content.currentWidget() is panel and not panel.isWindow()
                panel.groups.setCurrentRow(next(i for i, group in enumerate(panel.facets) if group['key'] == 'param_31013_n'))
                panel.low.setText('64'); panel.high.setText('128'); panel.unit.setCurrentText('KB'); panel.range_apply.click()
                window.grab().save(str(root / 'native-filters.png'))
                panel.apply.click(); state['mode'] = 'filtered'
            elif mode == 'filtered' and not dialog.searching:
                from urllib.parse import unquote
                requests = [json.loads(row[3]) for row in service.requests if row[1] == '/category/product']
                assert '64KB~128KB@param_31013_n#range' in unquote(requests[-1]['queryParameterValue'])
                assert dialog.search_content.currentWidget() is dialog.search_results
                report['native_catalog_and_filters'] = True
                dialog.search_table.setRowCount(0); dialog.search_items = []; dialog.search_selected.clear()
                state['mode'] = 'start'
            elif mode == 'start':
                window.open_favorites()
                assert [dialog.tabs.tabText(i) for i in range(4)] == ['搜索商品', '我的收藏', '购物车', '我的订单']
                rows = [dict(part='C2861386', title='TLV75718PDRVR', manufacturer='TI', package='WSON-6',
                             datasheet='https://www.ti.com/cn/lit/ds/symlink/tlv757p.pdf', details_complete=True,
                             min_quantity=5, quantity_step=5, price_tiers=[{'quantity': 5, 'end': 45, 'price': '0.50'},
                                 {'quantity': 50, 'end': None, 'price': '0.40'}]),
                        dict(part='C507270', title='TLV75718PDBVR', manufacturer='TI', package='SOT-23-5',
                             min_quantity=10, quantity_step=10, details_complete=True)]
                for product in rows:
                    dialog.append_row(dialog.search_table, product)
                table = dialog.search_table
                for row, amount in ((0, 5), (1, 20)):
                    table.item(row, 0).setCheckState(Qt.Checked)
                    table.cellWidget(row, table.quantity_column).setValue(amount)
                spin = table.cellWidget(0, table.quantity_column)
                spin.lineEdit().setText('46'); spin.interpretText()
                assert spin.value() == 50 and spin.singleStep() == 5
                assert table.item(0, table.step_column).text() == '5'
                price = table.cellWidget(0, 5)
                assert price.currentData() == 50 and '0.40' in price.currentText()
                price.setCurrentIndex(0); price.activated.emit(0)
                assert price.currentData() == 50 and '5–45' in price.itemText(0)
                spin.setValue(5)
                report['quantity_increments_and_price_tiers'] = True
                table.selectRow(0)
                assert dialog.datasheet_button.isEnabled()
                menu = dialog.make_product_menu(dialog.context_products(table, 0), table)
                menu.actions()[0].trigger()
                assert 'C2861386' in window.ids
                assert window.table.columnCount() == 4
                assert window.purchase_quantities['C2861386'] == 5
                report['download_list_without_quantity'] = True
                report['context_download_and_quantity_sync'] = True
                for toast in list(window.notifications.toasts):
                    toast.close()
                window.grab().save(str(root / 'native-search.png'))
                menu.actions()[2].trigger(); menu.actions()[2].trigger()
                state['mode'] = 'added'
            elif mode == 'added' and not dialog.cart_busy:
                assert [row['productNumber'][0] for row in service.cart_adds] == ['5', '20']
                assert window._page_host.current_page() is dialog
                assert not hasattr(dialog, 'cart_dialog')
                assert not dialog.findChildren(QWebEngineView)
                report['inline_add_without_navigation'] = report['no_storefront_webview'] = True
                dialog.tabs.setCurrentIndex(2)
                state['mode'] = 'cart'
            elif mode == 'cart' and dialog.cart_panel.loaded:
                panel = dialog.cart_panel
                if any(panel.table.item(row, panel.table.thumbnail_column).icon().isNull() for row in range(panel.table.rowCount())):
                    return
                assert panel.table.rowCount() == 2
                report['cart_product_photos'] = True
                assert panel.table.item(0, 8).text() == '5'
                assert panel.table.cellWidget(0, 5).singleStep() == 5
                for toast in list(window.notifications.toasts):
                    toast.close()
                window.grab().save(str(root / 'native-cart.png'))
                panel.table.cellWidget(0, 5).setValue(50)
                assert not panel.remove.isEnabled()
                state['mode'] = 'quantity'
            elif mode == 'quantity' and service.cart_updates and not dialog.cart_mutating:
                panel = dialog.cart_panel
                assert panel.rows[0]['quantity'] == 50
                assert len(service.cart_updates) == 1
                report['native_cart_quantity_saved'] = True
                before = len(service.requests)
                panel.open_cart.click()
                assert len(service.requests) == before
                state['mode'] = 'checkout'
            elif mode == 'checkout' and not dialog.cart_mutating:
                assert opened == [CART_URL]
                assert not service.checkout_requests
                report['external_cart_without_checkout'] = True
                dialog.tabs.setCurrentIndex(3)
                state['mode'] = 'orders'
            elif mode == 'orders' and dialog.orders_panel.loaded:
                assert dialog.orders_panel.table.item(0, 0).text() == 'TEST-20261013'
                report['native_order_list'] = True
                window.grab().save(str(root / 'native-orders.png'))
                state['browser_count'] = len(opened)
                dialog.orders_panel.table.setCurrentCell(0, 0)
                dialog.orders_panel.table.cellClicked.emit(0, 0)
                state['mode'] = 'order-items'
            elif mode == 'order-items' and dialog.orders_panel.detail.loaded:
                panel = dialog.orders_panel
                detail = panel.detail
                if detail.table.item(0, detail.table.thumbnail_column).icon().isNull():
                    return
                report['order_product_photos'] = True
                assert panel.content.currentWidget() is detail and not detail.isWindow()
                assert detail.table.rowCount() == 2 and '¥12.50' in detail.summary.text()
                assert len(opened) == state['browser_count']
                window.grab().save(str(root / 'native-order-items.png'))
                detail.select.click(); detail.download.click()
                assert 'C2040' in window.ids and 'P-TOOL' not in window.ids
                detail.back.click()
                assert panel.content.currentWidget() is panel.list_page and panel.table.currentRow() == 0
                report['native_order_items_in_place'] = report['order_parts_to_download'] = True
                dialog.tabs.setCurrentIndex(0)
                assert dialog.search_table.cellWidget(0, dialog.search_table.quantity_column).value() == 5
                report['quantity_preserved_between_tabs'] = True
                batch_products = (
                    dict(part='C20618009', title='OD1EL89CEIBXE6YLC-100M', details_complete=True),
                    dict(part='C5832372', title='FTC252012S2R2MBCA', details_complete=True,
                         product_id='6763492'))
                service.details['6763492'] = {**fixture_store_record('C5832372'),
                                              'productId': '6763492', 'convesionRatio': 20}
                dialog.items = {product['part']: product for product in batch_products}
                dialog.tabs.setCurrentIndex(1)
                table = dialog.favorite_table
                table.setRowCount(0)
                for index, product in enumerate(batch_products):
                    dialog.append_row(table, product)
                    table.item(index, 0).setCheckState(Qt.Checked)
                service.cart_replies['C20618009'] = {'code': 402, 'msg': '商品已停售'}
                for toast in list(window.notifications.toasts):
                    toast.close()
                dialog.cart_button.click()
                state['mode'] = 'favorite-rules'
            elif mode == 'favorite-rules' and not dialog.cart_busy:
                assert '失败 2 项' in dialog.status.text()
                assert not any(row['productCode'][0] == 'C5832372' for row in service.cart_adds)
                table = dialog.favorite_table
                assert table.cellWidget(1, table.quantity_column).value() == 20
                assert table.item(1, table.step_column).text() == '20'
                report['favorite_rules_checked_before_write'] = True
                for toast in list(window.notifications.toasts):
                    toast.close()
                dialog.cart_button.click()
                state['mode'] = 'batch-result'
            elif mode == 'batch-result' and not dialog.cart_busy:
                assert [row['productCode'][0] for row in service.cart_adds[-2:]] == ['C20618009', 'C5832372']
                assert '成功 1 项；失败 1 项；未确认 0 项；未发送 0 项' in dialog.status.text()
                assert 'C20618009' in dialog.status.text()
                assert '已停售' in dialog.favorite_table.item(0, 2).text()
                assert dialog.favorite_table.item(1, 0).checkState() == Qt.Unchecked
                entry = window.notifications.history.entries[0]
                assert 'C20618009 × 1：失败' in entry['details'] and 'C5832372 × 20：成功' in entry['details']
                report['batch_rejection_continues'] = report['per_item_cart_results'] = True
                state['capture_after'] = time.monotonic() + .4
                state['mode'] = 'batch-capture'
            elif mode == 'batch-capture' and time.monotonic() >= state['capture_after']:
                window.grab().save(str(root / 'native-batch-result.png'))
                dialog.clear_session()
                assert dialog.cart_panel.table.rowCount() == dialog.orders_panel.table.rowCount() == 0
                report['logout_clears_account_tables'] = True
                state['mode'] = 'finish'
            elif mode == 'finish' and not dialog.has_jobs():
                events = (get_log_directory() / 'LCSC3D.log').read_text('utf-8')
                assert all(event in events for event in ('cart.add_confirmed', 'cart.quantity.finished', 'orders.loaded',
                    'orders.detail_loaded', 'orders.download_import', 'image.thumbnails.finished',
                    'commerce.external_open', 'cart.response_rejected', 'cart.batch_item', 'cart.batch_completed', 'cart.batch_presented'))
                assert package_logs()['path'].is_file()
                assert not clear_logs()['failures']
                assert log_event('INFO', 'verification.native_commerce_after_clear')
                assert 'verification.native_commerce_after_clear' in (get_log_directory() / 'LCSC3D.log').read_text('utf-8')
                report['logs_package_and_continue_after_clear'] = True
                report['single_native_window'] = [w for w in QApplication.topLevelWidgets() if w.isVisible()] == [window]
                assert report['single_native_window']
                finish()
        except Exception as exc:
            finish(type(exc).__name__ + ': ' + str(exc))
    timer.timeout.connect(tick); timer.start()
