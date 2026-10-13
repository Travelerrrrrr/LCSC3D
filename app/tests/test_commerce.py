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
from lcsc3d.services.store import StoreClient, StoreError, SessionExpired, product_page, datasheet_url
from lcsc3d.services.commerce import add_to_cart, CART_QUICK_API, current_cart, apply_browser_cookies
from lcsc3d.selftests.favorites_selftest import OfflineStore
from lcsc3d.ui.commerce import CommerceBrowser, browser_cookie, native_cookie, CartQuantityDialog
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

    def test_transport_error_is_never_retried_and_logs_exclude_secret(self):
        with patch.object(self.client, '_json', side_effect=StoreError('SENSITIVE fixture')) as request:
            with self.assertRaises(StoreError):
                add_to_cart(self.client, {'part': 'C2040'}, 1)
            self.assertEqual(request.call_count, 1)
        data = (logs.get_log_directory() / 'LCSC3D.log').read_text('utf-8')
        self.assertIn('cart.add.failed', data)
        self.assertNotIn('SENSITIVE', data)


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
        env.start(); self.addCleanup(env.stop)
        self.addCleanup(logs.close_logging)
        self.client = StoreClient()
        self.client.account = {'name': 'Fixture'}
        self.dialog = FavoritesDialog(client_factory=lambda: self.client, vault=False)
        self.dialog.request_detail = lambda product: None
        self.dialog.show()
        self.addCleanup(self.close)

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

    def test_tabs_lazy_browser_context_target_and_download(self):
        self.assertEqual([self.dialog.tabs.tabText(i) for i in range(4)], ['搜索商品', '我的收藏', '购物车', '我的订单'])
        self.assertIsNone(self.dialog.commerce_browser)
        rows = self.products()
        self.dialog.search_table.item(0, 0).setCheckState(Qt.Checked)
        self.assertEqual(self.dialog.context_products(self.dialog.search_table, 1), [rows[1]])
        self.dialog.search_table.item(1, 0).setCheckState(Qt.Checked)
        self.assertEqual(len(self.dialog.context_products(self.dialog.search_table, 0)), 2)
        found = []
        self.dialog.import_requested.connect(found.append)
        menu = self.dialog.make_product_menu(rows, self.dialog)
        self.assertEqual([a.text() for a in menu.actions()], ['添加到下载列表', '添加到收藏', '添加到购物车'])
        menu.actions()[0].trigger()
        self.assertEqual(found[0], [{'part': 'C1', 'title': 'One'}, {'part': 'C2', 'title': 'Two'}])

    def test_batch_stops_on_uncertain_write_and_never_replays_success(self):
        rows = self.products() + [dict(part='C3', title='Three')]
        self.dialog.add_cart_products(rows)
        self.assertEqual(self.dialog.cart_dialog.inputs[0].value(), 5)
        with patch.object(self.client, 'account_info', return_value=self.client.account), \
                patch('lcsc3d.ui.commerce_actions.add_to_cart', side_effect=[{'product': rows[0]}, StoreError('failure')]) as add:
            self.dialog.cart_dialog.submit.click()
            self.dialog.cart_dialog.submit.click()
            self.wait(lambda: not self.dialog.cart_busy)
        self.assertEqual(add.call_count, 2)
        self.assertIn('成功 1 项；未确认 1 项；未发送 1 项', self.dialog.cart_dialog.status.text())
        self.assertFalse(self.dialog.cart_dialog.submit.isEnabled())

    def test_cookie_sync_memory_profile_and_navigation_rules(self):
        browser = CommerceBrowser(self.dialog)
        self.dialog.commerce_browser = browser
        from PySide6.QtNetwork import QNetworkCookie
        cookie = QNetworkCookie(b'fixture', b'NO_REAL_SESSION')
        cookie.setDomain('.szlcsc.com'); cookie.setPath('/'); cookie.setHttpOnly(True); cookie.setSecure(True)
        native = native_cookie(cookie)
        self.assertTrue(browser.profile.isOffTheRecord())
        with patch.object(browser, 'navigate') as navigate:
            browser.initialize([native], 'https://cart.szlcsc.com/cart/display.html')
            self.wait(lambda: browser.ready)
            self.assertEqual(navigate.call_count, 1)
        self.assertEqual(len(browser.cookies), 1)
        apply_browser_cookies(self.client, list(browser.cookies.values()))
        self.assertEqual(next(iter(self.client.session.cookies)).value, 'NO_REAL_SESSION')
        self.assertTrue(browser.page.acceptNavigationRequest(QUrl('https://order.szlcsc.com/member/perfect.html'), 0, True))
        for url in ('file:///C:/secret', 'http://cart.szlcsc.com', 'https://cart.szlcsc.com.evil.test', 'javascript:alert(1)'):
            self.assertFalse(browser.page.acceptNavigationRequest(QUrl(url), 0, True))
        self.dialog.clear_session()
        self.assertIsNone(self.dialog.commerce_browser)

    def test_cookie_cart_selection_and_removed_cookie(self):
        from PySide6.QtNetwork import QNetworkCookie
        cookie = QNetworkCookie(b'currentCartVo', b'%7B%22cartCode%22%3A%22fixture-cart%22%7D')
        cookie.setDomain('.szlcsc.com'); cookie.setPath('/')
        apply_browser_cookies(self.client, [native_cookie(cookie)])
        self.assertEqual(current_cart(self.client), 'fixture-cart')
        browser = CommerceBrowser(self.dialog)
        self.dialog.commerce_browser = browser
        browser.cookie_removed(cookie)
        apply_browser_cookies(self.client, list(browser.cookies.values()))
        self.assertEqual(current_cart(self.client), '')
