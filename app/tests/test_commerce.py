"""Shopping regressions use synthetic cookies and a local server, never an account."""
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PySide6.QtCore import Qt, QUrl, QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication
from lcsc3d.core import app_logging as logs
from lcsc3d.services.store import StoreClient, StoreError, SessionExpired, product_page, datasheet_url, commercial_data
from lcsc3d.services.commerce import (add_to_cart, cart_items, order_page, update_quantity, read_cart, read_orders, CART_URL)
from lcsc3d.selftests.favorites_selftest import OfflineStore
from lcsc3d.ui.commerce import QuantityInput, PriceTierBox
from lcsc3d.ui.favorites import FavoritesDialog


def page(record, **extra):
    return '<script id="__NEXT_DATA__">' + json.dumps({'props': {'pageProps': {'webData': {
        'productRecord': record, **extra}}}}) + '</script>'


class DatasheetTests(unittest.TestCase):
    def test_five_reported_public_parts(self):
        cases = json.loads((Path(__file__).parent / 'fixtures' / 'datasheets_241.json').read_text('utf-8'))
        for part, data in cases.items():
            with self.subTest(part=part):
                self.assertEqual(product_page(page(data['productRecord']), part)['datasheet'], data['expected'])

    def test_new_manufacturer_link_and_unrelated_certificates(self):
        record = {'productCode': 'C2861386', 'fileTypeVOList': [
            {'fileType': 'certification_data_property', 'detailVOList': [{'fileUrl': '/upload/public/pdf/rohs.pdf'}]},
            {'fileType': 'pdf_property', 'detailVOList': [{'linkAddress': 'https://www.ti.com/cn/lit/ds/symlink/tlv757p.pdf'}]}]}
        self.assertEqual(product_page(page(record), 'C2861386')['datasheet'],
                         'https://www.ti.com/cn/lit/ds/symlink/tlv757p.pdf')
        record['fileTypeVOList'].pop()
        self.assertEqual(product_page(page(record), 'C2861386')['datasheet'], '')

    def test_current_relative_file_legacy_and_invalid_candidates(self):
        data = {'productRecord': {'fileTypeVOList': [{'fileType': 'pdf_property', 'detailVOList': [
            None, {'linkAddress': 'javascript:alert(1)'}, {'fileUrl': '/upload/public/pdf/source/a.pdf'}]}]}}
        self.assertEqual(datasheet_url(data), 'https://atta.szlcsc.com/upload/public/pdf/source/a.pdf')
        for groups in (None, {}, [None], [{'fileType': 'pdf_property', 'detailVOList': 7}]):
            data['productRecord']['fileTypeVOList'] = groups
            data['pdfFileDetailVO'] = {'fileUrl': '//datasheet.lcsc.com/legacy.pdf'}
            self.assertEqual(datasheet_url(data), 'https://datasheet.lcsc.com/legacy.pdf')

    def test_eda_link_supplements_official_page_missing_pdf(self):
        client = StoreClient()
        official = page({'productCode': 'C1', 'productId': 123})
        catalog = dict(part='C1', model='', resources={}, datasheet='https://item.szlcsc.com/datasheet/part/123.html')
        with patch.object(client.session, 'request', return_value=(official, 'https://item.szlcsc.com/123.html')), \
                patch.object(client, '_catalog_search', return_value=[catalog]):
            product = client.product({'part': 'C1', 'product_id': '123'})
        self.assertEqual(product['datasheet'], catalog['datasheet'])
        self.assertTrue(product['details_complete'])


class CartApiTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.env = patch.dict(os.environ, LOCALAPPDATA=self.folder.name)
        self.env.start(); self.addCleanup(self.env.stop)
        logs.configure_logging(version='2.4.1'); self.addCleanup(logs.close_logging)
        self.service = OfflineStore(); self.addCleanup(self.service.close)
        self.client = self.service.client()

    def test_quick_add_posts_product_units_once_and_rejects_invalid_quantity(self):
        product = {'part': 'C2040', 'min_quantity': 5}
        for quantity in (0, -1, 4, True, '5', 1000000000):
            with self.assertRaises(StoreError):
                add_to_cart(self.client, product, quantity)
        self.assertFalse(self.service.cart_adds)
        add_to_cart(self.client, product, 25)
        self.assertEqual(self.service.cart_adds, [{'productCode': ['C2040'], 'productNumber': ['25']}])
        self.service.cart_code = 500
        with self.assertRaises(StoreError):
            add_to_cart(self.client, product, 10)
        self.assertEqual(len(self.service.cart_adds), 2)
        self.service.cart_code = 401
        with self.assertRaises(SessionExpired):
            add_to_cart(self.client, product, 10)

    def test_purchase_increment_uses_ratio_and_converts_minimum_and_tiers_to_pieces(self):
        for step in (5, 50):
            with self.subTest(step=step):
                data = commercial_data({'minBuyNumber': 2, 'theRatio': step, 'convesionRatio': step,
                    'productMinEncapsulationNumber': 3000, 'productPriceList': [
                        {'startPurchasedNumber': 1, 'endPurchasedNumber': 9, 'productPrice': '0.125'},
                        {'startPurchasedNumber': 10, 'endPurchasedNumber': -1, 'productPrice': '0.10'}]})
                self.assertEqual((data['min_quantity'], data['quantity_step']), (2 * step, step))
                self.assertEqual([r['quantity'] for r in data['price_tiers']], [2 * step, 10 * step])
                self.assertEqual(data['price_tiers'][0]['end'], 9 * step)
                self.assertEqual(data['price_tiers'][0]['price'], '0.125')

    def test_invalid_increment_is_rejected_before_cart_write(self):
        with self.assertRaises(StoreError):
            add_to_cart(self.client, {'part': 'C2040', 'min_quantity': 5, 'quantity_step': 5}, 7)
        with self.assertRaises(StoreError):
            update_quantity(self.client, {'part': 'C2040', 'minimum': 10, 'quantity_step': 5}, 12)
        self.assertFalse(self.service.cart_adds or self.service.cart_updates)

    def test_transport_error_is_never_retried_and_logs_exclude_secret(self):
        with patch.object(self.client, '_json', side_effect=StoreError('SENSITIVE fixture')) as request:
            with self.assertRaises(StoreError):
                add_to_cart(self.client, {'part': 'C2040'}, 1)
            self.assertEqual(request.call_count, 1)
        data = (logs.get_log_directory() / 'LCSC3D.log').read_text('utf-8')
        self.assertIn('cart.add.failed', data)
        self.assertNotIn('SENSITIVE', data)

    def test_cart_groups_ratio_prices_and_overseas_are_preserved(self):
        row = dict(shopCarMapKey='0~123~RMB~CN', productCode='C2040', productModel='RP2040',
                   productNumber='2', convesionRatio='10', productConsultPrice='0.125', lineMoney='2.50', checked=True)
        cart = {'rmbCnShoppingCart': {'currentlyProductList': [row]}, 'shoppingCartOverseaVO': {
            'digikeyProductList': [{**row, 'shopCarMapKey': '1~345', 'productCode': 'P1', 'isChecked': True}]}}
        rows = cart_items({'code': 200, 'result': {'shoppingCartVO': {'cartItemGroups': [cart]}, 'isCartItemGroup': True}})
        self.assertEqual(len(rows), 2)
        self.assertEqual((rows[0]['quantity'], rows[0]['price'], rows[0]['total']), (20, '0.125', '2.50'))
        self.assertEqual(rows[1]['region'], 'overseas')
        self.assertEqual((rows[0]['minimum'], rows[0]['quantity_step']), (10, 10))
        unavailable = {**row, 'errStatus': 'cannot_buy'}
        blocked = cart_items({'code': 200, 'result': {'shoppingCartVO': {'rmbCnShoppingCart': {'currentlyProductList': [unavailable]}}}})[0]
        self.assertFalse(blocked['available'])
        with patch.object(self.client, '_json', return_value={'code': 200}) as request:
            update_quantity(self.client, rows[0], 50)
            self.assertEqual(request.call_args.kwargs['form']['productNumber'], '5')

    def test_order_list_uses_customer_order_list_and_rejects_truncated_pages(self):
        self.client.session.request('https://www.szlcsc.com/cas/login')
        result = read_orders(self.client)
        self.assertEqual(result['items'][0]['code'], 'TEST-20261013')
        self.assertEqual(result['items'][0]['status'], '待处理')
        self.assertEqual(result['items'][0]['payment'], '待付款')
        self.assertIn('RP2040', result['items'][0]['summary'])
        self.assertNotIn('linkMan', result['items'][0])
        with self.assertRaises(StoreError):
            order_page({'code': 200, 'result': {'page': {'currPage': 1, 'pageRow': 20, 'totalRow': 2}, 'customerOrderList': []}}, 1)


class CommerceUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if QApplication.instance() is None:
            QApplication.setAttribute(Qt.AA_ShareOpenGLContexts)
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        env = patch.dict(os.environ, LOCALAPPDATA=self.folder.name)
        env.start(); self.addCleanup(env.stop); self.addCleanup(logs.close_logging)
        self.service = OfflineStore(); self.addCleanup(self.service.close)
        self.client = self.service.client()
        self.client.session.request('https://www.szlcsc.com/cas/login')
        self.client.account = self.client.account_info()
        self.dialog = FavoritesDialog(client_factory=lambda: self.client, vault=False)
        self.dialog.request_detail = lambda product: None
        self.dialog.show(); self.addCleanup(self.close)

    def close(self):
        self.dialog.shutdown(); self.dialog.close()
        self.wait(lambda: not self.dialog.has_jobs())
        self.dialog.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        self.app.processEvents()

    def wait(self, condition, timeout=8):
        end = time.monotonic() + timeout
        while not condition() and time.monotonic() < end:
            self.app.processEvents(); time.sleep(.01)
        self.assertTrue(condition())

    def products(self):
        rows = [dict(part='C1', title='One', min_quantity=5, details_complete=True),
                dict(part='C2', title='Two', details_complete=True)]
        for product in rows:
            self.dialog.append_row(self.dialog.search_table, product)
        return rows

    def test_search_increment_typed_quantity_and_price_tiers_stay_in_sync(self):
        rows = [dict(part='C1', title='One', min_quantity=5, quantity_step=5, details_complete=True,
                     price_tiers=[{'quantity': 5, 'end': 45, 'price': '1.2345'},
                                  {'quantity': 50, 'end': None, 'price': '0.99'}])]
        table = self.dialog.search_table
        self.dialog.append_row(table, rows[0])
        spin, price = table.cellWidget(0, table.quantity_column), table.cellWidget(0, 5)
        self.assertEqual(table.item(0, table.step_column).text(), '5')
        self.assertEqual((spin.minimum(), spin.singleStep(), spin.value()), (5, 5, 5))
        spin.stepUp(); self.assertEqual(spin.value(), 10)
        spin.stepDown(); self.assertEqual(spin.value(), 5)
        spin.lineEdit().setText('46'); spin.interpretText()
        self.assertEqual(spin.value(), 50)
        self.assertEqual(price.currentData(), 50)
        self.assertIn('0.99', price.currentText())
        self.assertIn('5–45', price.itemText(0))
        price.setCurrentIndex(0); price.activated.emit(0)
        self.assertEqual(price.currentData(), 50)  # Viewing a tier cannot override the actual price.
        self.assertEqual(spin.value(), 50)
        self.dialog.append_row(self.dialog.favorite_table, rows[0])
        favorite = self.dialog.favorite_table
        favorite.cellWidget(0, favorite.quantity_column).setValue(15)
        self.assertEqual(spin.value(), 15)
        self.assertEqual(price.currentData(), 5)
        self.dialog.make_product_menu(rows, table).actions()[2].trigger()
        self.wait(lambda: not self.dialog.cart_busy)
        self.assertEqual(self.service.cart_adds[0]['productNumber'], ['15'])

    def test_cart_increment_saves_physical_quantity_as_pack_count(self):
        self.service.cart_steps['C2040'] = 5
        add_to_cart(self.client, {'part': 'C2040'}, 10)
        self.dialog.tabs.setCurrentIndex(2)
        self.wait(lambda: self.dialog.cart_panel.loaded)
        panel = self.dialog.cart_panel
        self.assertEqual(panel.table.item(0, 8).text(), '5')
        spin = panel.table.cellWidget(0, 5)
        self.assertEqual(spin.singleStep(), 5)
        spin.lineEdit().setText('12'); spin.interpretText()
        self.wait(lambda: bool(self.service.cart_updates) and not self.dialog.cart_mutating)
        self.assertEqual(self.service.cart_updates[0]['productNumber'], ['3'])
        self.assertEqual(panel.rows[0]['quantity'], 15)
        self.assertEqual(panel.table.cellWidget(0, 5).value(), 15)

    def test_quantity_upper_bound_is_a_valid_multiple_and_price_can_be_unknown(self):
        spin = QuantityInput(1, 100, step=50)
        self.assertEqual(spin.value(), 100)
        spin.lineEdit().setText('999999999'); spin.interpretText()
        self.assertEqual(spin.value() % 50, 0)
        self.assertLessEqual(spin.value(), 999999999)
        price = PriceTierBox({'price_tiers': [{'quantity': 10, 'end': 20, 'price': '0.01'}]})
        price.set_quantity(25)
        self.assertEqual(price.currentIndex(), -1)

    def test_existing_cart_quantity_is_displayed_exactly_until_edited(self):
        spin = QuantityInput(7, 10, step=5, preserve_initial=True)
        self.assertEqual(spin.value(), 7)
        spin.stepUp()
        self.assertEqual(spin.value(), 10)
        spin.stepDown()
        self.assertEqual(spin.value(), 10)

    def test_right_click_adds_inline_quantities_without_page_or_dialog(self):
        from PySide6.QtWebEngineWidgets import QWebEngineView
        products = self.products(); table = self.dialog.search_table
        table.cellWidget(0, table.quantity_column).setValue(25)
        table.cellWidget(1, table.quantity_column).setValue(7)
        for row in range(2):
            table.item(row, 0).setCheckState(Qt.Checked)
        menu = self.dialog.make_product_menu(self.dialog.context_products(table, 0), table)
        menu.actions()[2].trigger(); menu.actions()[2].trigger()
        self.wait(lambda: not self.dialog.cart_busy)
        self.assertEqual([r['productNumber'][0] for r in self.service.cart_adds], ['25', '7'])
        self.assertEqual(self.dialog.tabs.currentIndex(), 0)
        self.assertFalse(self.dialog.findChildren(QWebEngineView))
        self.assertFalse(hasattr(self.dialog, 'cart_dialog'))

    def test_quantities_survive_page_changes_and_sync_favorites(self):
        rows = self.products(); table = self.dialog.search_table
        table.cellWidget(0, table.quantity_column).setValue(30)
        table.setRowCount(0)
        self.dialog.append_row(table, rows[0])
        self.assertEqual(table.cellWidget(0, table.quantity_column).value(), 30)
        self.dialog.append_row(self.dialog.favorite_table, rows[0])
        favorite = self.dialog.favorite_table
        favorite.cellWidget(0, favorite.quantity_column).setValue(50)
        self.assertEqual(table.cellWidget(0, table.quantity_column).value(), 50)
        self.assertEqual(self.dialog.context_products(table, 0)[0]['part'], 'C1')

    def test_add_commits_active_quantity_editor_without_opening_another_page(self):
        rows = self.products(); table = self.dialog.search_table
        editor = table.cellWidget(0, table.quantity_column)
        editor.lineEdit().setText('37')
        self.dialog.make_product_menu([rows[0]], table).actions()[2].trigger()
        self.wait(lambda: not self.dialog.cart_busy)
        self.assertEqual(self.service.cart_adds[0]['productNumber'], ['37'])
        self.assertEqual(self.dialog.tabs.currentIndex(), 0)

    def test_partial_add_stops_without_replaying_prior_rows(self):
        rows = self.products() + [dict(part='C3', title='Three')]
        with patch('lcsc3d.ui.commerce_actions.add_to_cart', side_effect=[{'product': rows[0]}, StoreError('fixture failure')]) as add:
            self.dialog.add_cart_products(rows)
            self.wait(lambda: not self.dialog.cart_busy)
        self.assertEqual(add.call_count, 2)
        self.assertIn('成功 1 项；未确认 1 项；未发送 1 项', self.dialog.status.text())

    def test_native_cart_updates_quantity_then_opens_browser_without_checkout(self):
        self.dialog.add_cart_products([dict(part='C2040', purchase_quantity=5)])
        self.wait(lambda: not self.dialog.cart_busy)
        self.dialog.tabs.setCurrentIndex(2)
        self.wait(lambda: self.dialog.cart_panel.loaded)
        panel = self.dialog.cart_panel
        spin = panel.table.cellWidget(0, 5)
        spin.setValue(12); spin.setValue(25)
        self.assertFalse(panel.remove.isEnabled())
        self.wait(lambda: bool(self.service.cart_updates) and not self.dialog.cart_mutating)
        self.assertEqual(len(self.service.cart_updates), 1)
        self.assertEqual(panel.rows[0]['quantity'], 25)
        self.dialog.set_import_enabled(False)
        panel.show_rows(panel.rows)
        self.assertFalse(panel.download.isEnabled())
        self.dialog.set_import_enabled(True)
        with patch('lcsc3d.ui.commerce_actions.QDesktopServices.openUrl', return_value=True) as opened:
            before = len(self.service.requests)
            panel.open_cart.click()
            self.assertEqual(len(self.service.requests), before)
        self.assertFalse(self.service.checkout_requests)
        self.assertEqual(opened.call_count, 1)
        self.assertEqual(opened.call_args.args[0].toString(), CART_URL)
        self.assertFalse(panel.table.isHidden())

    def test_cart_link_works_without_selection_account_or_native_requests(self):
        self.dialog.clear_session()
        panel = self.dialog.cart_panel
        self.assertEqual(panel.open_cart.text(), '跳转到购物车')
        self.assertTrue(panel.open_cart.isEnabled())
        with patch.object(self.client, '_json') as request, \
                patch('lcsc3d.ui.commerce_actions.QDesktopServices.openUrl', return_value=True) as opened:
            panel.open_cart.click()
            request.assert_not_called()
            self.assertEqual(opened.call_args.args[0].toString(), CART_URL)
        self.assertFalse(self.service.checkout_requests)

    def test_browser_failure_retry_only_reopens_cart_and_is_logged(self):
        logs.configure_logging(version='2.4.1')
        with patch('lcsc3d.ui.commerce_actions.QDesktopServices.openUrl', side_effect=[False, True]) as opened, \
                patch('lcsc3d.ui.commerce_actions.notify') as notice, patch.object(self.client, '_json') as request:
            self.dialog.cart_panel.open_cart.click()
            notice.call_args.kwargs['actions'][0][1]()
            self.assertEqual([call.args[0].toString() for call in opened.call_args_list], [CART_URL, CART_URL])
            request.assert_not_called()
        events = (logs.get_log_directory() / 'LCSC3D.log').read_text('utf-8')
        self.assertIn('commerce.external_open', events)
        self.assertIn('"opened": false', events)
        self.assertIn('"opened": true', events)

    def test_native_orders_pagination_filter_and_logout(self):
        self.service.order_rows = [{**self.service.order_rows[0], 'uuid': f'fixture-{n}', 'orderCode': f'TEST-{n:02}'} for n in range(25)]
        self.dialog.tabs.setCurrentIndex(3)
        panel = self.dialog.orders_panel
        self.wait(lambda: panel.loaded)
        self.assertEqual(panel.table.rowCount(), 20)
        panel.next.click(); self.wait(lambda: not self.dialog.orders_loading)
        self.assertEqual((panel.page, panel.table.rowCount()), (2, 5))
        panel.keyword.setText('TEST-03'); panel.refresh.click()
        self.wait(lambda: not self.dialog.orders_loading)
        self.assertEqual(panel.table.rowCount(), 1)
        self.assertEqual(panel.table.item(0, 0).text(), 'TEST-03')
        self.dialog.clear_session()
        self.assertEqual(panel.table.rowCount(), 0)
        self.assertFalse(panel.loaded)
