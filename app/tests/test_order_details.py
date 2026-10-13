"""Native order navigation and cart photos, using isolated synthetic accounts."""
import copy
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
from PySide6.QtCore import Qt, QCoreApplication, QEvent
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication
from lcsc3d.core import app_logging as logs
from lcsc3d.services.store import StoreError
from lcsc3d.services.commerce import add_to_cart
from lcsc3d.services.order_details import parse_order_details, read_order_details
from lcsc3d.selftests.favorites_selftest import OfflineStore, fixture_order_detail
from lcsc3d.ui.favorites import FavoritesDialog


class OrderDataTests(unittest.TestCase):
    def payload(self):
        return fixture_order_detail({'uuid': 'private-order-id', 'orderCode': 'PRIVATE-ORDER-CODE',
            'orderTime': '2026-10-13', 'currencyType': 'RMB', 'orderTotalMoney': '12.50'})

    def test_native_amounts_goods_and_repeated_parts_are_preserved_without_contact_data(self):
        payload = self.payload()
        item = {**payload['result']['szNormalProductList'][0], 'purchaseNumber': 10, 'productDiscountPrice': '0.12345',
                'orderDetailTotalMoney': '1.10'}
        payload['result']['jsAllBrokageDetailsList'] = [item]
        parsed = parse_order_details(payload, 'private-order-id')
        self.assertEqual(len(parsed['items']), 3)
        self.assertEqual(parsed['items'][2]['part'], 'C2040')
        self.assertEqual(parsed['items'][2]['quantity'], '10')
        self.assertEqual(parsed['items'][2]['price'], '0.12345')
        self.assertEqual(parsed['items'][2]['total'], '1.10')
        self.assertEqual(parsed['total'], '12.50')
        self.assertFalse(parsed['items'][1]['downloadable'])
        self.assertNotIn('PRIVATE_ORDER_ADDRESS', json.dumps(parsed))
        self.assertNotIn('linkPhone', json.dumps(parsed))

    def test_wrong_order_malformed_groups_and_unknown_layout_are_rejected(self):
        for change in ('identity', 'groups', 'missing', 'row'):
            payload = self.payload()
            if change == 'identity': payload['result']['orderVO']['uuid'] = 'other-order'
            if change == 'groups': payload['result']['szNormalProductList'] = {}
            if change == 'row': payload['result']['szNormalProductList'] = [None]
            if change == 'missing': payload['result'] = {'orderVO': payload['result']['orderVO']}
            with self.subTest(change=change), self.assertRaises(StoreError):
                parse_order_details(payload, 'private-order-id')

    def test_unknown_amount_is_not_zero_and_mro_c_code_is_not_downloadable(self):
        payload = self.payload()
        row = payload['result']['szNormalProductList'][0]
        row.update(isMro=True, productPrice=None, orderDetailTotalMoney=None, purchaseNumber='0.5')
        parsed = parse_order_details(payload, 'private-order-id')['items'][0]
        self.assertFalse(parsed['downloadable'])
        self.assertEqual((parsed['price'], parsed['total'], parsed['quantity']), ('', '', '0.5'))


class OrderUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if QApplication.instance() is None:
            QApplication.setAttribute(Qt.AA_ShareOpenGLContexts)
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(); self.addCleanup(self.folder.cleanup)
        env = patch.dict(os.environ, LOCALAPPDATA=self.folder.name)
        env.start(); self.addCleanup(env.stop)
        logs.configure_logging(version='2.4.1'); self.addCleanup(logs.close_logging)
        self.service = OfflineStore(); self.addCleanup(self.service.close)
        self.client = self.service.client()
        self.client.session.request('https://www.szlcsc.com/cas/login')
        self.client.account = self.client.account_info()
        self.dialog = FavoritesDialog(client_factory=lambda: self.client, vault=False)
        self.dialog.show(); self.addCleanup(self.close)

    def wait(self, condition):
        deadline = time.monotonic() + 8
        while not condition() and time.monotonic() < deadline:
            self.app.processEvents(); time.sleep(.01)
        self.assertTrue(condition())

    def close(self):
        self.dialog.shutdown(); self.dialog.close()
        self.wait(lambda: not self.dialog.has_jobs())
        self.dialog.deleteLater(); QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        self.app.processEvents()

    def orders(self):
        self.dialog.tabs.setCurrentIndex(3)
        self.wait(lambda: self.dialog.orders_panel.loaded)
        return self.dialog.orders_panel

    def test_order_opens_inside_same_region_and_only_explicit_details_opens_browser(self):
        panel = self.orders()
        imported = []; self.dialog.import_requested.connect(imported.append)
        panel.keyword.setText('keep-filter')
        panel.table.setCurrentCell(0, 0)
        with patch('lcsc3d.ui.commerce_actions.QDesktopServices.openUrl', return_value=True) as browser:
            panel.table.cellClicked.emit(0, 0)
            self.wait(lambda: panel.detail.loaded)
            self.assertEqual(panel.content.currentWidget(), panel.detail)
            self.assertEqual(panel.detail.parentWidget(), panel.content)
            self.assertFalse(panel.detail.isWindow())
            self.assertEqual(panel.detail.table.rowCount(), 2)
            self.assertIn('¥12.50', panel.detail.summary.text())
            self.assertEqual(panel.detail.table.item(0, 4).text(), '5 个')
            self.assertEqual(panel.detail.table.item(0, 6).text(), '¥7.50')
            browser.assert_not_called()
            panel.detail.select.click()
            panel.detail.download.click()
            self.assertEqual(imported, [[{'part': 'C2040', 'title': 'RP2040'}]])
            self.assertFalse(panel.detail.table.item(1, 0).flags() & Qt.ItemIsUserCheckable)
            panel.detail.details.click()
            self.assertEqual(browser.call_count, 1)
            self.assertIn('/member/order/details.html?uuid=', browser.call_args.args[0].toString())
            panel.detail.back.click()
        self.assertEqual(panel.content.currentWidget(), panel.list_page)
        self.assertEqual(panel.keyword.text(), 'keep-filter')
        self.assertEqual((panel.page, panel.table.currentRow()), (1, 0))
        self.assertEqual(panel.detail.table.rowCount(), 0)

    def test_back_retains_page_scroll_and_selection_without_refetch(self):
        self.service.order_rows = [{**self.service.order_rows[0], 'uuid': f'fixture-{i}', 'orderCode': f'TEST-{i}'} for i in range(45)]
        panel = self.orders(); panel.next.click(); self.wait(lambda: panel.page == 2 and not self.dialog.orders_loading)
        panel.table.setCurrentCell(12, 0); panel.table.scrollToItem(panel.table.item(12, 0))
        self.app.processEvents()
        scroll = panel.table.verticalScrollBar().value()
        requests = len([r for r in self.service.requests if r[1] == '/member/order/list'])
        panel.open_row(12); self.wait(lambda: panel.detail.loaded)
        panel.detail.back.click()
        self.assertEqual((panel.page, panel.table.currentRow(), panel.table.verticalScrollBar().value()), (2, 12, scroll))
        self.assertEqual(len([r for r in self.service.requests if r[1] == '/member/order/list']), requests)

    def test_failure_retry_logout_and_import_disabled_state(self):
        panel = self.orders()
        self.service.order_details['fixture-order-1'] = {'code': 500}
        panel.open_row(0)
        self.wait(lambda: panel.detail.refresh.isEnabled())
        self.assertFalse(panel.detail.loaded)
        self.assertFalse(panel.detail.download.isEnabled())
        self.assertIn('失败', panel.detail.hint.text())
        self.service.order_details.clear()
        self.dialog.set_import_enabled(False)
        panel.detail.refresh.click(); self.wait(lambda: panel.detail.loaded)
        panel.detail.select.click(); self.assertFalse(panel.detail.download.isEnabled())
        self.dialog.set_import_enabled(True); self.assertTrue(panel.detail.download.isEnabled())
        self.dialog.clear_session()
        self.assertEqual(panel.content.currentWidget(), panel.list_page)
        self.assertFalse(panel.detail.rows)
        self.assertIsNone(panel.detail.order)

    def test_return_cancels_inflight_detail_and_ignores_late_response(self):
        panel = self.orders(); release = threading.Event(); entered = threading.Event()
        def delayed(*args):
            entered.set(); release.wait(4)
            return parse_order_details(fixture_order_detail(self.service.order_rows[0]), 'fixture-order-1')
        with patch('lcsc3d.ui.commerce_actions.read_order_details', side_effect=delayed):
            panel.open_row(0); self.wait(entered.is_set)
            panel.detail.back.click(); release.set()
            self.wait(lambda: not self.dialog.has_jobs())
        self.assertEqual(panel.content.currentWidget(), panel.list_page)
        self.assertFalse(panel.detail.loaded or panel.detail.rows)

    def test_detail_logs_contain_operation_and_counts_but_no_order_or_contact(self):
        result = read_order_details(self.client, 'fixture-order-1')
        self.assertEqual(len(result['items']), 2)
        data = (logs.get_log_directory() / 'LCSC3D.log').read_text('utf-8')
        self.assertIn('orders.detail_loaded', data)
        for secret in ('fixture-order-1', 'TEST-20261013', 'PRIVATE_ORDER_ADDRESS', 'PRIVATE_ORDER_PHONE', 'RP2040'):
            self.assertNotIn(secret, data)

    def test_cart_photos_load_cache_and_preview_uses_original_image(self):
        add_to_cart(self.client, {'part': 'C2040'}, 5)
        self.dialog.tabs.setCurrentIndex(2)
        panel = self.dialog.cart_panel
        self.wait(lambda: panel.loaded and not panel.table.item(0, panel.table.thumbnail_column).icon().isNull())
        self.wait(lambda: not self.dialog.has_jobs())
        calls = len([r for r in self.service.requests if r[1].endswith('.png')])
        self.dialog.load_cart(); self.wait(lambda: not self.dialog.cart_loading)
        self.wait(lambda: not self.dialog.has_jobs())
        self.assertEqual(len([r for r in self.service.requests if r[1].endswith('.png')]), calls)
        with patch.object(self.dialog, 'show_product_gallery') as gallery:
            panel.table.cellDoubleClicked.emit(0, panel.table.thumbnail_column)
        self.assertIn('/product/source/', gallery.call_args.args[0]['images'][0])
        self.assertEqual(gallery.call_args.args[0]['part'], 'C2040')

    def test_missing_or_stale_cart_photo_does_not_replace_new_row(self):
        add_to_cart(self.client, {'part': 'C2040'}, 5)
        self.service.cart_rows[0]['breviaryImageUrl'] = ''
        self.dialog.tabs.setCurrentIndex(2); self.wait(lambda: self.dialog.cart_panel.loaded)
        table = self.dialog.cart_panel.table
        self.assertEqual(table.item(0, table.thumbnail_column).text(), '—')
        picture = QImage(20, 20, QImage.Format_ARGB32); picture.fill(Qt.red)
        self.dialog.thumbnail_loaded(table, 0, ('https://alimg.szlcsc.com/old.png', picture))
        self.assertTrue(table.item(0, table.thumbnail_column).icon().isNull())
        self.assertTrue(self.dialog.cart_panel.open_cart.isEnabled())
