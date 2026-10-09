"""Native storefront behavior against a local HTTP service, never a real account."""
import http.cookiejar
import http.client
import base64
import ctypes
import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import main
from errors import Cancelled
from favorites import FavoritesDialog
from favorites_selftest import (OfflineStore, fixture_favorites, fixture_product, fixture_search,
                                fixture_store_record, fixture_detail, native_click)
from store import (MemorySession, StoreClient, StoreError, SessionExpired, catalog_products,
                   favorite_products, normalize_items, public_url, product_page, search_page_products, CaptchaRequired,
                   storefront_product)
from store_session import SessionVault
from PySide6.QtCore import QEvent, Qt, QPoint, QPointF
from PySide6.QtGui import QWheelEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLineEdit
from PySide6.QtWebEngineWidgets import QWebEngineView


def captured_detail(part):
    fixtures = Path(__file__).parent / 'fixtures' / 'storefront_products.json'
    web = json.loads(fixtures.read_text(encoding='utf-8'))[part]
    return '<script id="__NEXT_DATA__" type="application/json">' + json.dumps({'props': {'pageProps': {'webData': web}}}) + '</script>'


def desktop_input_available():
    """Native foreground input requires an unlocked, visible Windows desktop."""
    from ctypes import wintypes
    class Flags(ctypes.Structure):
        _fields_ = [('inherit', wintypes.BOOL), ('reserved', wintypes.BOOL), ('flags', wintypes.DWORD)]
    user = ctypes.windll.user32
    station = user.GetProcessWindowStation
    station.restype = ctypes.c_void_p
    info = user.GetUserObjectInformationW
    info.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    flags, needed = Flags(), wintypes.DWORD()
    if not info(station(), 1, ctypes.byref(flags), ctypes.sizeof(flags), ctypes.byref(needed)) or not flags.flags & 1:
        return False
    open_input = user.OpenInputDesktop
    open_input.argtypes, open_input.restype = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], ctypes.c_void_p
    handle = open_input(0, False, 1)
    if not handle:
        return False
    close = user.CloseDesktop
    close.argtypes = [ctypes.c_void_p]
    close(handle)
    return True


def activate_test_window(window):
    """Select this test window through native input, then restore ordinary style."""
    from ctypes import wintypes
    class MouseInput(ctypes.Structure):
        _fields_ = [('dx', wintypes.LONG), ('dy', wintypes.LONG), ('data', wintypes.DWORD),
                    ('flags', wintypes.DWORD), ('time', wintypes.DWORD), ('extra', ctypes.c_void_p)]
    class Input(ctypes.Structure):
        _fields_ = [('type', wintypes.DWORD), ('mouse', MouseInput)]
    QApplication.processEvents()
    user, hwnd = ctypes.windll.user32, int(window.winId())
    context = user.SetThreadDpiAwarenessContext
    context.argtypes, context.restype = [ctypes.c_void_p], ctypes.c_void_p
    previous_context = context(ctypes.c_void_p(-4))
    position = user.SetWindowPos
    position.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
                        ctypes.c_int, ctypes.c_int, ctypes.c_uint]
    previous = wintypes.POINT()
    user.GetCursorPos(ctypes.byref(previous))
    try:
        position(hwnd, ctypes.c_void_p(-1), 0, 0, 0, 0, 0x13)
        rectangle = wintypes.RECT()
        get_rect = user.GetWindowRect
        get_rect.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.RECT)]
        assert get_rect(hwnd, ctypes.byref(rectangle))
        target = wintypes.POINT(rectangle.left + 80, rectangle.top + 80)
        hit = user.WindowFromPoint
        hit.argtypes, hit.restype = [wintypes.POINT], ctypes.c_void_p
        ancestor = user.GetAncestor
        ancestor.argtypes, ancestor.restype = [ctypes.c_void_p, ctypes.c_uint], ctypes.c_void_p
        assert ancestor(hit(target), 2) == hwnd, 'Test window is covered'
        user.SetCursorPos(target.x, target.y)
        events = (Input * 2)(Input(0, MouseInput(flags=2)), Input(0, MouseInput(flags=4)))
        send = user.SendInput
        send.argtypes, send.restype = [ctypes.c_uint, ctypes.POINTER(Input), ctypes.c_int], ctypes.c_uint
        assert send(2, events, ctypes.sizeof(Input)) == 2
        time.sleep(0.05)
        QApplication.processEvents()
    finally:
        position(hwnd, ctypes.c_void_p(-2), 0, 0, 0, 0, 0x13)
        user.SetCursorPos(previous.x, previous.y)
        context(previous_context)
    style = user.GetWindowLongPtrW
    style.argtypes, style.restype = [ctypes.c_void_p, ctypes.c_int], ctypes.c_ssize_t
    assert not style(hwnd, -20) & 8, 'Test window still has topmost style'


class ProductTests(unittest.TestCase):
    def test_captured_public_prices_stock_and_unquoted_product_are_parsed(self):
        data = json.loads((Path(__file__).parent / 'fixtures/storefront_commerce.json').read_text(encoding='utf-8'))
        for part, row in data.items():
            payload = fixture_search([(part, '')])
            payload['result']['searchResult']['productRecordList'] = [row]
            product = search_page_products(payload, 1)['items'][0]
            self.assertEqual(product['stock'], row['totalStockNumber'])
            if part == 'C499531':
                self.assertEqual([t['quantity'] for t in product['price_tiers']], [1, 10, 30, 100, 500, 1000])
                self.assertEqual(product['price_tiers'][0]['price'], '20.17')
            elif part == 'C20618009':
                self.assertEqual(product['price_tiers'], [])

    def test_mainland_prices_keep_precision_and_use_the_minimum_buy_quantity(self):
        record = fixture_store_record()
        record.update({'minBuyNumber': 5, 'productMinEncapsulationNumber': 5000,
                       'productHkDollerPriceList': [{'startPurchasedNumber': 1, 'productPrice': '99'}]})
        product = storefront_product(record)
        self.assertEqual([(t['quantity'], t['price']) for t in product['price_tiers']],
                         [(5, '0.012345'), (10, '0.010111'), (100, '0.008765')])
        self.assertEqual(product['min_quantity'], 5)
        self.assertEqual(product['unit'], '个')

    def test_default_prices_start_at_the_first_available_valid_tier(self):
        record = fixture_store_record()
        record['minBuyNumber'] = 20
        product = storefront_product(record)
        self.assertEqual([t['quantity'] for t in product['price_tiers']], [20, 100])
        self.assertEqual(product['price_tiers'][0]['price'], '0.010111')

    def test_bad_prices_are_unknown_and_missing_stock_is_not_zero(self):
        record = fixture_store_record()
        record.pop('stockNumber')
        record['productPriceList'] = [
            {'startPurchasedNumber': 1, 'productPrice': 'NaN'},
            {'startPurchasedNumber': 2, 'productPrice': False},
            {'startPurchasedNumber': 3, 'productPrice': '-1'},
            {'startPurchasedNumber': 4, 'productPrice': '1e9999999'}]
        product = storefront_product(record)
        self.assertNotIn('stock', product)
        self.assertEqual(product['price_tiers'], [])
        record['isShowPrice'] = False
        record['productPriceList'] = fixture_store_record()['productPriceList']
        self.assertEqual(storefront_product(record)['price_tiers'], [])

    def test_search_inventory_uses_the_row_total_including_zero(self):
        payload = fixture_search([('C2040', 'RP2040')])
        row = payload['result']['searchResult']['productRecordList'][0]
        row['totalStockNumber'], row['smtStockNumber'] = 0, 99000
        product = search_page_products(payload, 1)['items'][0]
        self.assertEqual(product['stock'], 0)
        self.assertEqual(len(product['price_tiers']), 3)

    def test_import_normalization_rejects_bad_codes_and_preserves_first_title(self):
        result = normalize_items([{'part':' c2040 ', 'title':' RP2040 '}, {'part':'C2040'},
                                  {'part':'12345'}, {'part':'C1<script>'}, 'C2',
                                  {'part':'C3','title':'a'*999}, {'part':'C4','title':None}])
        self.assertEqual([p['part'] for p in result], ['C2040', 'C3', 'C4'])
        self.assertEqual(result[0]['title'], 'RP2040')
        self.assertEqual(len(result[1]['title']), 240)
        self.assertEqual(normalize_items({'part':'C1'}), [])

    def test_search_ranks_exact_code_and_deduplicates(self):
        raw = [fixture_product('C2','Other'), fixture_product(), fixture_product()]
        result = catalog_products({'code':0,'result':raw}, 'c2040')
        self.assertEqual([p['part'] for p in result], ['C2040','C2'])

    def test_search_contains_details_links_and_resource_associations(self):
        product = catalog_products({'code':0,'result':[fixture_product()]})[0]
        self.assertEqual(product['title'], 'RP2040')
        self.assertEqual(product['store_url'], 'https://item.szlcsc.com/2392.html')
        self.assertIn(('CPU Core','ARM Cortex-M0+'), product['parameters'])
        self.assertEqual(product['resources'], {'Symbol':True,'Footprint':True,'3D Model':True})

    def test_search_does_not_guess_code_from_model_name(self):
        result = catalog_products({'code':0,'result':[{'title':'C2040'}, {'product_code':'C1<script>'}]})
        self.assertEqual(result, [])

    def test_empty_search_and_api_error_are_distinct(self):
        self.assertEqual(catalog_products({'code':0,'result':[]}), [])
        for payload in (None, {'code':500,'result':[]}, {'code':0,'result':{}}, {'code':0,'success':False,'result':[]}):
            with self.assertRaises(StoreError):
                catalog_products(payload)

    def test_non_web_links_are_removed(self):
        for url in ('javascript:alert(1)','file:///C:/secret','https://user:pass@site.test','http://site.test'):
            self.assertEqual(public_url(url), '')
        self.assertEqual(public_url('//image.lceda.cn/test.png'), 'https://image.lceda.cn/test.png')

    def test_account_collection_uses_only_the_page_product_rows(self):
        payload = fixture_favorites([('C2040','RP2040'), ('C2040','RP2040'), ('M1','MRO'), ('C2','Resistor')], total=4)
        payload['result']['recommendations'] = [{'productCode':'C999'}]
        products, total, size = favorite_products(payload, 1)
        self.assertEqual([p['part'] for p in products], ['C2040','C2'])
        self.assertEqual((total,size), (4,2))
        self.assertEqual(products[0]['manufacturer'], 'Offline fixture')

    def test_bad_page_metadata_and_missing_page_are_errors(self):
        for payload in ({'code':200,'result':{}}, fixture_favorites([], page=2),
                        fixture_favorites([], total=2), fixture_favorites([], size=0)):
            with self.assertRaises(StoreError):
                favorite_products(payload, 1)

    def test_reported_oscillator_details_and_original_images_are_preserved(self):
        product = product_page(captured_detail('C20618009'), 'C20618009')
        self.assertEqual(product['package'], 'SMD5032-6P')
        self.assertEqual(len(product['parameters']), 7)
        self.assertIn(('频率', '100MHz'), product['parameters'])
        self.assertEqual(len(product['images']), 4)
        self.assertIn('/product/source/', product['images'][0])
        self.assertIn('/brand/product/certificate/', product['images'][3])
        self.assertTrue(product['datasheet'].startswith('https://datasheet.lcsc.com/upload/public/pdf/'))

    def test_reported_resistor_has_all_three_native_photos(self):
        product = product_page(captured_detail('C5879483'), 'C5879483')
        self.assertEqual(product['title'], 'RT0603BRD0750RL')
        self.assertEqual(len(product['images']), 3)
        self.assertEqual(len(set(product['images'])), 3)
        self.assertTrue(all('/product/source/' in url for url in product['images']))

    def test_wrong_product_and_missing_page_data_are_not_shown_as_selected_details(self):
        for html in (captured_detail('C5879483'), '<html>Temporarily unavailable</html>'):
            with self.assertRaises(StoreError):
                product_page(html, 'C20618009')

    def test_store_search_checks_page_metadata_and_does_not_include_recommendations(self):
        page = fixture_search([('C2', 'Target')], total=1)
        page['result']['recommendations'] = [fixture_store_record('C999', 'Unrelated')]
        self.assertEqual([p['part'] for p in search_page_products(page, 1)['items']], ['C2'])
        for key, value in (('currePage', 2), ('pageSize', 0), ('totalCount', -1), ('countPage', 0)):
            broken = json.loads(json.dumps(page))
            broken['result']['searchResult'][key] = value
            with self.assertRaises(StoreError):
                search_page_products(broken, 1)


class StoreProtocolTests(unittest.TestCase):
    def test_detail_enrichment_preserves_search_stock_and_selected_price_data(self):
        product = self.client.search('C2040')[0]
        product['stock'] = 0
        product['min_quantity'] = 5
        product['price_tiers'][0]['quantity'] = 5
        before = {k: product[k] for k in ('stock', 'min_quantity', 'price_tiers', 'unit')}
        detailed = self.client.product(product)
        self.assertTrue(detailed['details_complete'])
        self.assertEqual({k: detailed[k] for k in before}, before)

    def setUp(self):
        self.service = OfflineStore()
        self.addCleanup(self.service.close)
        self.client = self.service.client()

    def login(self):
        return self.client.finish_login('offline-token')

    def test_native_qr_fetch_and_official_success_status(self):
        qr = self.client.start_qr()
        self.assertTrue(qr['image'].startswith(b'\x89PNG'))
        self.assertEqual(self.client.scan_status(qr['token']), 'CREATED')
        self.service.scan_state = 'SUCCESS'
        self.assertEqual(self.client.scan_status(qr['token']), 'LOGIN_SUCCESS')

    def test_malformed_qr_fields_are_reported_without_an_uncaught_exception(self):
        for data in ('unexpected response', ['unexpected'],
                     {'token': 'offline-token', 'url': 'https://mp.weixin.qq.com/offline.png', 'expireSeconds': 'invalid'},
                     {'token': {'invalid': True}, 'url': 'https://mp.weixin.qq.com/offline.png'}):
            with self.subTest(data=data), \
                    patch.object(self.client, '_json', return_value={'code': 200, 'data': data}), \
                    self.assertRaises(StoreError):
                self.client.start_qr()
        self.assertFalse(self.service.requests)

    def test_rejected_risk_check_never_submits_password_or_sends_sms(self):
        original = self.client._json

        def rejected(url, **kwargs):
            if '/secure/check-' in url:
                return {'code': 500, 'message': '登录服务暂不可用'}
            return original(url, **kwargs)

        for action in ('password', 'sms'):
            with self.subTest(action=action), patch.object(self.client, '_json', side_effect=rejected), \
                    self.assertRaisesRegex(StoreError, '登录服务暂不可用'):
                if action == 'password':
                    self.client.login_password('OFFLINE', 'fixture-password')
                else:
                    self.client.send_sms('13800000000')
        self.assertEqual(self.service.sms_sends, 0)
        self.assertFalse(self.service.auth_inputs)
        self.assertFalse(any('/with-password' in row[1] or '/with-sms/' in row[1] for row in self.service.requests))

    def test_malformed_authorization_does_not_establish_an_account(self):
        with self.assertRaisesRegex(StoreError, '登录授权数据格式异常'):
            self.client._finish_auth({'code': 200, 'data': 'unexpected response'})
        self.assertIsNone(self.client.account)
        self.assertFalse(self.service.requests)

    def test_scan_expiry_and_unknown_state_do_not_complete_login(self):
        self.service.scan_state = 'EXPIRED'
        self.assertEqual(self.client.scan_status('offline-token'), 'EXPIRED')
        self.service.scan_state = 'UNRECOGNIZED'
        with self.assertRaises(StoreError):
            self.client.scan_status('offline-token')
        self.assertIsNone(self.client.account)

    def test_authorization_is_exchanged_and_account_is_verified(self):
        self.assertEqual(self.login()['code'], 'OFFLINE')
        paths = [r[1] for r in self.service.requests]
        self.assertLess(paths.index('/api/cas/login/get-init-session'), paths.index('/api/cas/login/with-official-qrcode'))
        self.assertLess(paths.index('/cas/login'), paths.index('/cas/user/info'))
        submission = next(json.loads(r[3]) for r in self.service.requests if r[1].endswith('with-official-qrcode'))
        self.assertFalse(submission['isAutoLogin'])

    def test_additional_identity_verification_is_not_reported_as_logged_in(self):
        self.service.flow_code = 2014
        with self.assertRaises(StoreError):
            self.login()
        self.assertIsNone(self.client.account)
        self.assertFalse(any(r[1] == '/cas/login' for r in self.service.requests))

    def test_remember_login_requests_the_official_persistent_session(self):
        self.client.finish_login('offline-token', remember=True)
        submission = next(json.loads(r[3]) for r in self.service.requests if r[1].endswith('with-official-qrcode'))
        self.assertTrue(submission['isAutoLogin'])

    def test_valid_sso_renews_an_expired_store_session_without_qr(self):
        self.service.account_valid = False
        self.service.sso_valid = True
        self.assertEqual(self.client.restore_account()['code'], 'OFFLINE')
        self.assertFalse(any(r[1].endswith('get-official-qrcode') for r in self.service.requests))

    def test_account_verification_failure_does_not_promote_session(self):
        self.service.account_valid = False
        with self.assertRaises(SessionExpired):
            self.login()
        self.assertIsNone(self.client.account)

    def test_unlogged_session_cannot_read_favorites(self):
        with self.assertRaises(SessionExpired):
            self.client.favorites()
        self.assertFalse(any(r[1] == '/member/favorite/v3' for r in self.service.requests))

    def test_account_favorites_automatically_pages_and_deduplicates(self):
        self.login()
        progress = []
        products = self.client.favorites(on_page=lambda page, rows: progress.append((page,rows)))
        self.assertEqual([p['part'] for p in products], ['C2040','C20197','C163691'])
        self.assertEqual([p[0] for p in progress], [1,2])
        self.assertEqual(self.client.pages_read, 2)
        requests = [r for r in self.service.requests if r[1] == '/member/favorite/v3']
        self.assertEqual([r[2]['currentPage'] for r in requests], [['1'],['2']])
        self.assertTrue(all(r[0] == 'GET' for r in requests))

    def test_empty_collection_is_success(self):
        self.login()
        self.service.pages[1] = fixture_favorites([], total=0)
        self.assertEqual(self.client.favorites(), [])

    def test_duplicate_page_stops_and_preserves_completed_page(self):
        self.login()
        self.service.pages[2] = fixture_favorites([('C2040','RP2040'),('C20197','Resistor')], page=2)
        progress = []
        with self.assertRaises(StoreError):
            self.client.favorites(on_page=lambda page, rows: progress.append(page))
        self.assertEqual(progress, [1])

    def test_empty_followup_page_is_not_silently_treated_as_complete(self):
        self.login()
        self.service.pages[2] = fixture_favorites([], page=2)
        with self.assertRaises(StoreError):
            self.client.favorites()

    def test_stopping_prevents_requesting_another_page(self):
        self.login()
        stop = threading.Event()
        with self.assertRaises(Cancelled):
            self.client.favorites(stop, lambda page, rows: stop.set())
        self.assertEqual(sum(r[1] == '/member/favorite/v3' for r in self.service.requests), 1)

    def test_expired_account_stops_collection_before_reading(self):
        self.login()
        self.service.account_valid = False
        with self.assertRaises(SessionExpired):
            self.client.favorites()

    def test_clear_removes_cookies_and_account(self):
        self.login()
        self.client.clear()
        self.assertIsNone(self.client.account)
        self.assertFalse(list(self.client.session.cookies))

    def multi_page_search(self):
        rows = [(f'C{1000 + i}', f'MCU{i}') for i in range(75)]
        for page in range(1, 4):
            self.service.search_pages[('单片机', page)] = fixture_search(rows[(page-1)*30:page*30], page, len(rows))
        return rows

    def test_search_loads_one_fifty_result_page_and_requests_next_page_explicitly(self):
        rows = self.multi_page_search()
        first = self.client.search_results_page('单片机')
        self.assertEqual([p['part'] for p in first['items']], [part for part, _ in rows[:50]])
        self.assertEqual((first['total'], first['pages'], first['size']), (75, 2, 50))
        requests = [r for r in self.service.requests if r[1] == '/query/product']
        self.assertEqual([json.loads(r[3])['currentPage'] for r in requests], [1, 2])
        self.assertTrue(all(r[0] == 'POST' and json.loads(r[3])['pageSize'] == 30 for r in requests))
        second = self.client.search_results_page('单片机', 2)
        self.assertEqual([p['part'] for p in second['items']], [part for part, _ in rows[50:]])
        self.assertEqual(len(first['items']) + len(second['items']), 75)

    def test_cancelled_search_stops_before_requesting_followup_pages(self):
        self.multi_page_search()
        stop = threading.Event()
        original = self.client.search_page
        def read(keyword, page, cancelled):
            result = original(keyword, page, cancelled)
            stop.set()
            return result
        self.client.search_page = read
        with self.assertRaises(Cancelled):
            self.client.search_results_page('单片机', cancelled=stop)
        self.assertEqual(sum(r[1] == '/query/product' for r in self.service.requests), 1)

    def test_repeated_search_page_is_reported_instead_of_silently_truncating(self):
        self.multi_page_search()
        repeated = json.loads(json.dumps(self.service.search_pages[('单片机', 1)]))
        repeated['result']['searchResult']['currePage'] = 2
        self.service.search_pages[('单片机', 2)] = repeated
        with self.assertRaises(StoreError):
            self.client.search_results_page('单片机')

    def test_storefront_detail_is_available_when_eda_has_no_component(self):
        self.service.catalog_missing.add('C2')
        product = self.client.product({'part': 'C2', 'product_id': '102'})
        self.assertEqual(product['part'], 'C2')
        self.assertTrue(product['details_complete'])
        self.assertGreater(len(product['parameters']), 1)
        self.assertEqual(len(product['images']), 3)
        self.assertEqual(product['detail_source'], '立创商城')

    def test_detail_can_resolve_a_part_code_without_an_existing_product_id(self):
        product = self.client.product('C2')
        self.assertEqual(product['product_id'], '102')
        self.assertTrue(product['details_complete'])
        self.assertTrue(any(r[1] == '/102.html' for r in self.service.requests))

    def test_adding_an_existing_favorite_never_toggles_it_off(self):
        self.login()
        product = self.client.product('C2040')
        self.assertFalse(self.client.add_favorite(product)['added'])
        self.assertFalse(any(r[1].endswith('/add/dynamic') for r in self.service.requests))
        self.assertTrue(self.client.favorite_state('2392'))

    def test_new_favorite_is_verified_and_repeated_add_is_idempotent(self):
        self.login()
        product = self.client.product('C2')
        self.assertTrue(self.client.add_favorite(product)['added'])
        self.assertFalse(self.client.add_favorite(product)['added'])
        self.assertTrue(self.client.favorite_state('102'))
        self.assertIn('C2', [p['part'] for p in self.client.favorites()])
        self.assertEqual(sum(r[1].endswith('/add/dynamic') for r in self.service.requests), 1)

    def test_favorite_failure_and_expired_account_never_report_success_or_retry_toggle(self):
        product = self.client.product('C2')
        with self.assertRaises(SessionExpired):
            self.client.add_favorite(product)
        self.login()
        self.service.toggle_code = 500
        with self.assertRaises(StoreError):
            self.client.add_favorite(product)
        self.assertFalse(self.client.favorite_state('102'))
        self.assertEqual(sum(r[1].endswith('/add/dynamic') for r in self.service.requests), 1)

    def test_cancel_favorite_uses_form_post_and_is_verified_and_idempotent(self):
        self.login()
        product = self.client.product('C2040')
        self.assertTrue(self.client.remove_favorite(product)['removed'])
        self.assertFalse(self.client.favorite_state(product['product_id']))
        self.assertFalse(self.client.remove_favorite(product)['removed'])
        requests = [r for r in self.service.requests if r[1] == '/async/favorite/cancel']
        self.assertEqual(len(requests), 1)
        self.assertEqual((requests[0][0], requests[0][3]), ('POST', b'productIds=2392'))
        self.assertNotIn('C2040', [p['part'] for p in self.client.favorites()])

    def test_cancel_failure_keeps_the_favorite_and_invalid_account_never_sends_cancel(self):
        product = self.client.product('C2040')
        with self.assertRaises(SessionExpired):
            self.client.remove_favorite(product)
        self.login()
        self.service.cancel_code = 500
        with self.assertRaises(StoreError):
            self.client.remove_favorite(product)
        self.assertTrue(self.client.favorite_state('2392'))

    def test_password_login_encrypts_credentials_and_verifies_store_account(self):
        self.assertEqual(self.client.login_password('OFFLINE', 'fixture-password', remember=True)['code'], 'OFFLINE')
        submitted = next(r for r in self.service.requests if r[1].endswith('/with-password'))
        self.assertNotIn(b'fixture-password', submitted[3])
        self.assertNotIn(b'OFFLINE', submitted[3])
        self.assertEqual(self.service.auth_inputs[-1][1]['password'], 'fixture-password')
        self.assertTrue(self.service.auth_inputs[-1][1]['isAutoLogin'])

    def test_sms_login_requires_encrypted_phone_and_verified_account(self):
        self.assertTrue(self.client.send_sms('13800000000')['sent'])
        self.assertEqual(self.service.sms_sends, 1)
        self.assertEqual(self.client.login_sms('13800000000', '123456')['code'], 'OFFLINE')
        requests = [r for r in self.service.requests if '/with-sms/' in r[1]]
        self.assertEqual(len(requests), 2)
        self.assertTrue(all(b'13800000000' not in r[3] for r in requests))
        self.assertEqual(self.service.auth_inputs[-1][1]['validateCode'], '123456')

    def test_image_captcha_continues_auth_and_invalid_answer_never_succeeds(self):
        self.service.risk_required = True
        with self.assertRaises(CaptchaRequired) as error:
            self.client.login_password('OFFLINE', 'fixture-password')
        self.assertEqual(error.exception.scene, 'pass_word_login')
        self.assertFalse(self.service.auth_inputs)
        image = self.client.captcha_image(error.exception.scene)
        self.assertTrue(image['image'].startswith(b'\x89PNG'))
        with self.assertRaises(StoreError):
            self.client.check_captcha(error.exception.scene, image['ticket_code'], 'wrong')
        ticket = self.client.check_captcha(error.exception.scene, image['ticket_code'], '1234')
        self.assertEqual(self.client.login_password('OFFLINE', 'fixture-password', captcha_ticket=ticket)['code'], 'OFFLINE')

    def test_encrypted_auth_response_is_decoded_before_storefront_exchange(self):
        self.service.encrypt_auth_response = True
        self.assertEqual(self.client.login_password('OFFLINE', 'fixture-password')['code'], 'OFFLINE')
        exchange = next(r for r in self.service.requests if r[1] == '/cas/login')
        self.assertEqual(exchange[2]['code'], ['offline-auth'])

    def test_offline_transport_key_keeps_leading_zero_bytes_in_wire_format(self):
        from gmalg import SM2
        self.service.transport_secret, self.service.transport_public = SM2(rnd_fn=lambda bits: 1).generate_keypair()
        self.service.encrypt_auth_response = True
        self.assertLess(len(self.service.transport_secret), 32)
        self.assertEqual(self.client.login_password('OFFLINE', 'fixture-password')['code'], 'OFFLINE')
        self.assertEqual(self.client._auth_keys['privateHexKey'], '00' * 31 + '01')
        exchange = next(r for r in self.service.requests if r[1] == '/cas/login')
        self.assertEqual(exchange[2]['code'], ['offline-auth'])

    def test_bad_credentials_and_invalid_fields_never_promote_a_session(self):
        for username, password in (('', 'x'), ('user', '')):
            with self.assertRaises(StoreError):
                self.client.login_password(username, password)
        with self.assertRaises(StoreError):
            self.client.send_sms('bad phone')
        self.assertFalse(self.service.requests)
        self.service.credential_code = 10212
        with self.assertRaises(StoreError):
            self.client.login_password('OFFLINE', 'fixture-password')
        self.assertIsNone(self.client.account)


@unittest.skipUnless(sys.platform == 'win32', 'Windows current-user DPAPI')
class SessionVaultTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.vault = SessionVault(Path(self.directory.name) / 'session.bin')
        self.client = StoreClient()
        cookie = http.cookiejar.Cookie(0,'synthetic-account','synthetic-secret-value',None,False,'.szlcsc.com',
                                       True,True,'/',True,True,None,True,None,None,{'HttpOnly':None},False)
        self.client.session.cookies.set_cookie(cookie)

    def test_encrypted_session_restores_in_a_new_client(self):
        self.vault.save(self.client, self.vault.generation)
        encrypted = self.vault.path.read_bytes()
        self.assertNotIn(b'synthetic-secret-value', encrypted)
        restored = StoreClient()
        self.assertTrue(SessionVault(self.vault.path).load_into(restored))
        self.assertEqual([(c.name,c.value,c.secure) for c in restored.session.cookies],
                         [('synthetic-account','synthetic-secret-value',True)])

    def test_logout_invalidates_a_queued_save(self):
        ticket = self.vault.generation
        self.vault.delete()
        self.assertFalse(self.vault.save(self.client, ticket))
        self.assertFalse(self.vault.exists())

    def test_corrupt_saved_session_is_removed(self):
        self.vault.path.write_bytes(b'bad-encrypted-session')
        self.assertFalse(self.vault.load_into(StoreClient()))
        self.assertFalse(self.vault.exists())

    def test_expired_and_unrelated_cookies_are_excluded(self):
        cookie = next(iter(self.client.session.cookies))
        self.client.session.cookies.set_cookie(http.cookiejar.Cookie(0,'expired','old',None,False,'.szlcsc.com',True,True,'/',True,True,1,False,None,None,{},False))
        self.client.session.cookies.set_cookie(http.cookiejar.Cookie(0,'unrelated','private',None,False,'.example.com',True,True,'/',True,True,None,True,None,None,{},False))
        self.vault.save(self.client, self.vault.generation)
        restored = StoreClient()
        self.vault.load_into(restored)
        self.assertEqual([c.name for c in restored.session.cookies], [cookie.name])

    def test_cancelled_save_preserves_previous_encrypted_file(self):
        self.vault.save(self.client, self.vault.generation)
        before = self.vault.path.read_bytes()
        stop = threading.Event()
        stop.set()
        with self.assertRaises(Cancelled):
            self.vault.save(self.client, self.vault.generation, stop)
        self.assertEqual(self.vault.path.read_bytes(), before)


@unittest.skipUnless(sys.platform == 'win32', 'Native Qt window regression')
class NativeStoreWindowTests(unittest.TestCase):
    def test_each_search_product_has_independent_precise_price_tiers_and_stock(self):
        payload = fixture_search([('C2040', 'RP2040'), ('C2', 'Other')])
        payload['result']['searchResult']['productRecordList'][1]['productVO']['minBuyNumber'] = 5
        self.service.search_pages[('报价', 1)] = payload
        self.search_ready('报价')
        table = self.dialog.search_table
        first, second = table.cellWidget(0, 5), table.cellWidget(1, 5)
        self.assertEqual((first.currentData(), second.currentData()), (1, 5))
        self.assertIn('0.012345', first.currentText())
        self.assertEqual(table.item(0, 6).text(), '12,345')
        count = len(self.service.requests)
        first.setCurrentIndex(2)
        self.assertEqual((first.currentData(), second.currentData()), (100, 5))
        self.assertEqual(len(self.service.requests), count)
        self.assertEqual(table.item(0, 0).checkState(), Qt.Unchecked)

    def test_price_tier_selection_survives_pages_and_resets_for_new_search(self):
        rows = [(f'C{1000+i}', f'MCU{i}') for i in range(75)]
        for page in range(1, 4):
            self.service.search_pages[('分页报价', page)] = fixture_search(rows[(page-1)*30:page*30], page, 75)
        self.search_ready('分页报价')
        self.dialog.search_table.cellWidget(0, 5).setCurrentIndex(2)
        self.dialog.next_page_button.click()
        self.wait_until(lambda: not self.dialog.searching and not self.dialog.has_jobs())
        self.assertEqual(self.dialog.search_table.cellWidget(0, 5).currentData(), 1)
        self.dialog.previous_page_button.click()
        self.wait_until(lambda: not self.dialog.searching and not self.dialog.has_jobs())
        self.assertEqual(self.dialog.search_table.cellWidget(0, 5).currentData(), 100)
        self.dialog.start_search()
        self.wait_until(lambda: not self.dialog.searching and not self.dialog.has_jobs())
        self.assertEqual(self.dialog.search_table.cellWidget(0, 5).currentData(), 1)

    def test_unknown_search_price_and_stock_and_zero_stock_are_distinct(self):
        products = [{'part': 'C404', 'title': 'Unknown', 'details_complete': True},
                    {'part': 'C405', 'title': 'Out of stock', 'stock': 0, 'details_complete': True}]
        self.dialog.search_loaded({'items': products, 'page': 1, 'pages': 1, 'total': 2})
        self.assertIsNone(self.dialog.search_table.cellWidget(0, 5))
        self.assertEqual(self.dialog.search_table.item(0, 5).text(), '—')
        self.assertEqual(self.dialog.search_table.item(0, 6).text(), '—')
        self.assertEqual(self.dialog.search_table.item(1, 6).text(), '0')

    def test_long_description_and_parameter_values_are_readable_in_details_scroll(self):
        self.dialog.resize(1180, 700)
        description = ('这是较长的商品介绍，用于核对自动换行和完整显示。' * 35) + '介绍结束标记'
        feature = ('轻载高效模式；外部补偿；逐波限流；热保护；软启动。' * 10) + '参数结束标记'
        self.dialog.show_detail({'part': 'C499531', 'title': 'SIC461ED-T1-GE3', 'description': description,
                                 'parameters': [('普通参数', str(i)) for i in range(12)] + [('功能特性', feature)]})
        label = self.dialog.product_description
        self.wait_until(lambda: self.dialog.detail_scroll.verticalScrollBar().maximum() > 0
                        and self.dialog.parameters.rowHeight(12) > 60
                        and label.height() >= label.heightForWidth(label.width()))
        self.assertEqual(label.text(), description)
        self.assertGreaterEqual(label.height(), label.heightForWidth(label.width()))
        self.assertEqual(self.dialog.parameters.item(12, 1).text(), feature)
        self.assertGreaterEqual(self.dialog.parameters.viewport().height(), self.dialog.parameters.verticalHeader().length())
        self.dialog.detail_scroll.verticalScrollBar().setValue(self.dialog.detail_scroll.verticalScrollBar().maximum())
        self.app.processEvents()
        self.assertTrue(self.dialog.datasheet_button.isVisible())
        self.assertTrue(self.dialog.image_label.isVisible())
        self.assertFalse(self.dialog.detail_scroll.isAncestorOf(self.dialog.image_label))
        self.assertFalse(self.dialog.detail_scroll.isAncestorOf(self.dialog.collect_button))
        self.dialog.resize(1420, 820)
        self.wait_until(lambda: label.height() >= label.heightForWidth(label.width()))
        self.assertEqual(label.text(), description)

    @classmethod
    def setUpClass(cls):
        QApplication.setAttribute(Qt.AA_ShareOpenGLContexts)
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setStyle('Fusion')
        cls.app.setStyleSheet(main.STYLES)

    def setUp(self):
        for method in ('_setup_preview','request_component_info','show_preview'):
            stub = patch.object(main.MainWindow, method)
            stub.start()
            self.addCleanup(stub.stop)
        self.service = OfflineStore()
        self.window = main.MainWindow(settings_enabled=False)
        self.dialog = FavoritesDialog(self.window, client_factory=self.service.client, vault=False)
        self.window.bind_store(self.dialog)
        self.dialog.show()
        self.app.processEvents()

    def tearDown(self):
        self.dialog.shutdown()
        self.dialog.close()
        self.wait_until(lambda: not self.dialog.has_jobs())
        self.window.close()
        self.window.deleteLater()
        self.app.sendPostedEvents(None, QEvent.DeferredDelete)
        self.service.close()

    def wait_until(self, condition, timeout=15):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and not condition():
            QTest.qWait(10)
        active = [(w.channel, w.isRunning()) for w in self.dialog.jobs.workers]
        self.assertTrue(condition(), self.dialog.status.text() + ' · active=' + str(active))

    def login(self):
        self.dialog.set_mode('favorites')
        self.dialog.open_login()
        self.wait_until(lambda: bool(self.dialog.login_dialog.token))
        self.service.scan_state = 'SUCCESS'
        self.dialog.login_dialog.poll_status()
        self.wait_until(lambda: bool(self.dialog.client.account) and not self.dialog.busy)

    def test_search_details_and_login_use_native_widgets(self):
        self.dialog.search_input.setText('C2040')
        self.dialog.start_search()
        self.wait_until(lambda: self.dialog.current_product and self.dialog.current_product.get('details_complete'))
        self.assertEqual(self.dialog.current_product['part'], 'C2040')
        self.assertGreaterEqual(self.dialog.parameters.rowCount(), 2)
        self.assertFalse(self.dialog.findChildren(QWebEngineView))
        self.dialog.open_login()
        self.wait_until(lambda: bool(self.dialog.login_dialog.token))
        self.assertFalse(self.dialog.login_dialog.findChildren(QWebEngineView))
        self.assertTrue(self.dialog.login_dialog.remember_box.isChecked())

    def search_ready(self, keyword='C2040'):
        self.dialog.set_mode('search')
        self.dialog.search_input.setText(keyword)
        self.dialog.start_search()
        self.wait_until(lambda: not self.dialog.searching and self.dialog.current_product
                        and self.dialog.current_product.get('details_complete') and not self.dialog.has_jobs())

    def test_single_store_entry_reopens_the_same_window_and_preserves_its_tab(self):
        self.assertEqual(self.window.market_button.text(), '立创商城')
        self.assertFalse(hasattr(self.window, 'search_button'))
        self.assertFalse(hasattr(self.window, 'favorites_button'))
        self.dialog.set_mode('favorites')
        self.window.market_button.click()
        self.assertIs(self.window.favorites_dialog, self.dialog)
        self.assertEqual(self.dialog.tabs.currentIndex(), 1)

    def test_store_is_unowned_and_preview_restores_and_activates_main_window(self):
        self.window.show()
        self.search_ready()
        self.assertIsNone(self.dialog.parentWidget())
        self.assertEqual(self.dialog.windowModality(), Qt.NonModal)
        self.assertFalse(self.dialog.windowFlags() & Qt.WindowStaysOnTopHint)
        get_owner = ctypes.windll.user32.GetWindow
        get_owner.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        get_owner.restype = ctypes.c_void_p
        self.assertFalse(get_owner(int(self.dialog.winId()), 4))
        if not desktop_input_available():
            self.skipTest('Native owner checks passed; foreground mouse input needs an interactive Windows desktop')
        self.window.showMinimized()
        self.dialog.raise_()
        self.dialog.activateWindow()
        foreground = ctypes.windll.user32.GetForegroundWindow
        foreground.restype = ctypes.c_void_p
        activate_test_window(self.dialog)
        native_click(self.dialog.preview_button)
        self.wait_until(lambda: not self.window.isMinimized() and self.window.isActiveWindow()
                        and foreground() == int(self.window.winId()))
        self.assertTrue(self.dialog.isVisible())
        self.assertEqual(foreground(), int(self.window.winId()))

    def test_header_account_entry_is_before_update_and_uses_shared_login_state(self):
        self.window.show()
        self.app.processEvents()
        self.assertLess(self.window.account_button.geometry().right(), self.window.update_button.geometry().left())
        self.assertFalse(hasattr(self.dialog, 'login_button'))
        self.window.account_button.click()
        self.wait_until(lambda: self.dialog.login_dialog and self.dialog.login_dialog.token)
        self.assertIs(self.dialog.login_dialog.parentWidget(), self.window)
        self.service.scan_state = 'SUCCESS'
        self.dialog.login_dialog.poll_status()
        self.wait_until(lambda: self.dialog.client.account and not self.dialog.has_jobs())
        self.assertIn('账号：', self.window.account_button.text())
        self.assertIn('已登录', self.dialog.account_label.text())
        self.window.account_button.click()
        self.assertTrue(any(action.text() == '退出登录' for action in self.window.account_menu.actions()))
        self.dialog.clear_session()
        self.assertEqual(self.window.account_button.text(), '账号登录')

    def test_password_tab_authenticates_natively_and_clears_the_secret_on_close(self):
        self.window.account_button.click()
        login = self.dialog.login_dialog
        login.login_tabs.setCurrentIndex(1)
        self.assertFalse(login.password_visible.isChecked())
        self.assertEqual(login.password_input.echoMode(), QLineEdit.Password)
        login.account_input.setText('OFFLINE')
        login.password_input.setText('fixture-password')
        login.password_submit.click()
        self.wait_until(lambda: self.dialog.client.account and not self.dialog.has_jobs())
        self.assertFalse(login.isVisible())
        self.assertFalse(login.password_input.text())
        self.assertIn('账号：', self.window.account_button.text())

    def test_login_network_failure_shows_details_and_can_be_retried(self):
        with tempfile.TemporaryDirectory() as folder, \
                patch.dict('os.environ', {'LOCALAPPDATA': folder}), \
                patch('urllib.request.OpenerDirector.open',
                      side_effect=http.client.IncompleteRead(b'', 16)):
            self.dialog.open_login()
            self.wait_until(lambda: not self.dialog.has_jobs())
            login = self.dialog.login_dialog
            self.assertIn('网络响应异常或不完整', login.status.text())
            self.assertFalse(login.token)
            self.assertTrue(login.refresh_button.isEnabled())
            login.login_tabs.setCurrentIndex(2)
            login.phone_input.setText('13800000000')
            login.sms_send_button.click()
            self.wait_until(lambda: not self.dialog.has_jobs())
            self.assertIn('网络响应异常或不完整', login.status.text())
            self.assertTrue(login.sms_send_button.isEnabled())
            self.assertFalse(login.sms_sent_phone)
            self.assertEqual(self.service.sms_sends, 0)
        login.login_tabs.setCurrentIndex(0)
        self.wait_until(lambda: bool(login.token))
        self.assertIn('等待扫码', login.status.text())

    def test_sms_tab_sends_once_during_cooldown_and_authenticates(self):
        self.window.account_button.click()
        login = self.dialog.login_dialog
        login.login_tabs.setCurrentIndex(2)
        login.phone_input.setText('13800000000')
        login.sms_send_button.click()
        self.wait_until(lambda: bool(login.sms_sent_phone))
        self.assertFalse(login.sms_send_button.isEnabled())
        login.credential_action('sms-send')
        self.assertEqual(self.service.sms_sends, 1)
        login.sms_input.setText('123456')
        login.sms_submit.click()
        self.wait_until(lambda: self.dialog.client.account and not self.dialog.has_jobs())
        self.assertFalse(login.sms_input.text())
        self.assertFalse(login.phone_input.text())

    def test_password_risk_challenge_uses_native_image_input_and_continues(self):
        self.service.risk_required = True
        self.window.account_button.click()
        login = self.dialog.login_dialog
        login.login_tabs.setCurrentIndex(1)
        login.account_input.setText('OFFLINE')
        login.password_input.setText('fixture-password')
        login.password_submit.click()
        self.wait_until(lambda: bool(login.captcha_ticket_code))
        self.assertTrue(login.captcha_panel.isVisible())
        self.assertFalse(login.findChildren(QWebEngineView))
        login.captcha_input.setText('1234')
        login.captcha_verify_button.click()
        self.wait_until(lambda: self.dialog.client.account and not self.dialog.has_jobs())

    def test_cancel_collect_removes_only_the_account_entry_and_keeps_download_queue(self):
        self.login()
        self.wait_until(lambda: self.dialog.current_product and self.dialog.current_product.get('details_complete'))
        self.assertFalse(self.dialog.favorite_checked)
        self.dialog.favorite_table.item(0, 0).setCheckState(Qt.Checked)
        self.dialog.import_selected()
        before = self.window.ids[:]
        self.dialog.uncollect_button.click()
        self.wait_until(lambda: not self.dialog.collecting and 'C2040' not in self.dialog.items)
        self.assertEqual(self.window.ids, before)
        self.assertFalse(self.dialog.client.favorite_state('2392'))

    def test_cancelling_a_password_attempt_discards_its_late_success(self):
        self.window.account_button.click()
        login = self.dialog.login_dialog
        login.login_tabs.setCurrentIndex(1)
        started, release = threading.Event(), threading.Event()
        original = login.client.login_password
        def delayed(username, password, stop, **kwargs):
            started.set()
            release.wait(3)
            return original(username, password, stop, **kwargs)
        login.client.login_password = delayed
        login.account_input.setText('OFFLINE')
        login.password_input.setText('fixture-password')
        login.password_submit.click()
        self.wait_until(started.is_set)
        login.reject()
        release.set()
        self.wait_until(lambda: not self.dialog.has_jobs())
        self.assertIsNone(self.dialog.client.account)
        self.assertEqual(self.window.account_button.text(), '账号登录')

    def test_preview_leaves_the_store_and_its_selection_open(self):
        self.search_ready()
        preview = []
        self.dialog.preview_requested.connect(preview.append)
        before = self.dialog.selected_product()['part']
        self.dialog.preview_button.click()
        self.assertTrue(self.dialog.isVisible())
        self.assertEqual(preview, [before])
        self.assertEqual(self.dialog.selected_product()['part'], before)

    def test_pagination_preserves_cross_page_choices_and_only_displays_one_page(self):
        rows = [(f'C{1000+i}', f'MCU{i}') for i in range(75)]
        for page in range(1, 4):
            self.service.search_pages[('单片机', page)] = fixture_search(rows[(page-1)*30:page*30], page, 75)
        self.search_ready('单片机')
        self.assertEqual(self.dialog.search_table.rowCount(), 50)
        self.assertFalse(self.dialog.selected_items())
        self.assertFalse(self.dialog.previous_page_button.isEnabled())
        self.dialog.search_table.item(0, 0).setCheckState(Qt.Checked)
        self.dialog.next_page_button.click()
        self.wait_until(lambda: not self.dialog.searching and self.dialog.search_page == 2)
        self.assertEqual(self.dialog.search_table.rowCount(), 25)
        self.assertFalse(self.dialog.next_page_button.isEnabled())
        self.assertTrue(all(self.dialog.search_table.item(r, 0).checkState() == Qt.Unchecked for r in range(25)))
        self.dialog.search_table.item(0, 0).setCheckState(Qt.Checked)
        self.assertIn('75', self.dialog.search_hint.text())
        self.assertEqual([p['part'] for p in self.dialog.selected_items()], ['C1000', 'C1050'])
        self.dialog.previous_page_button.click()
        self.wait_until(lambda: not self.dialog.searching and self.dialog.search_page == 1)
        self.assertEqual(self.dialog.search_table.item(0, 0).checkState(), Qt.Checked)
        self.dialog.import_selected()
        self.assertEqual(self.window.ids, ['C1000', 'C1050'])
        self.search_ready('C2040')
        self.assertFalse(self.dialog.search_selected)
        self.assertFalse(self.dialog.selected_items())

    def test_stopped_page_navigation_keeps_existing_page_and_discards_late_response(self):
        release = threading.Event()
        self.dialog.search_keyword = 'test'
        self.dialog.search_loaded({'items':[{'part':'C2','title':'Kept','details_complete':True}], 'page':1,'total':75,'pages':2})
        self.dialog.search_table.item(0, 0).setCheckState(Qt.Checked)
        def search(keyword, page, stop):
            release.wait(3)
            return {'items':[{'part':'C3','title':'Late'}], 'page':2,'total':75,'pages':2}
        self.dialog.catalog.search_results_page = search
        self.dialog.next_page_button.click()
        self.dialog.search_stop_button.click()
        release.set()
        self.wait_until(lambda: not self.dialog.has_jobs())
        self.assertEqual([p['part'] for p in self.dialog.search_items], ['C2'])
        self.assertTrue(self.dialog.import_button.isEnabled())

    def test_clicking_the_photo_opens_all_original_images_with_zoom_and_navigation(self):
        self.search_ready()
        QTest.mouseClick(self.dialog.image_label, Qt.LeftButton)
        self.wait_until(lambda: bool(self.dialog.galleries) and self.dialog.galleries[0].view.photo)
        gallery = self.dialog.galleries[0]
        self.assertEqual(gallery.thumbnails.count(), 3)
        self.assertEqual((gallery.cache[0].width(), gallery.cache[0].height()), (640, 480))
        gallery.actual.click()
        self.assertAlmostEqual(gallery.view.zoom, 1)
        gallery.plus.click()
        self.assertGreater(gallery.view.zoom, 1)
        wheel = QWheelEvent(QPointF(100, 100), QPointF(100, 100), QPoint(), QPoint(0, 120),
                            Qt.NoButton, Qt.NoModifier, Qt.NoScrollPhase, False)
        QApplication.sendEvent(gallery.view.viewport(), wheel)
        self.assertGreater(gallery.view.zoom, 1.25)
        gallery.next.click()
        self.wait_until(lambda: gallery.thumbnails.currentRow() == 1 and gallery.view.photo)
        self.assertEqual(gallery.position.text(), '2 / 3')
        gallery.thumbnails.setCurrentRow(2)
        self.wait_until(lambda: gallery.view.photo and gallery.thumbnails.currentRow() == 2)
        self.assertFalse(gallery.next.isEnabled())
        gallery.close()
        self.wait_until(lambda: not self.dialog.galleries)
        self.assertTrue(self.dialog.isVisible())

    def test_search_product_can_be_saved_to_the_account_and_seen_in_favorites(self):
        self.login()
        self.search_ready('C2')
        self.dialog.collect_button.click()
        self.wait_until(lambda: 'C2' in self.dialog.items and not self.dialog.collecting)
        self.assertIn('账号收藏', self.dialog.status.text())
        self.assertFalse(self.dialog.collect_button.isEnabled())
        self.assertTrue(self.dialog.client.favorite_state('102'))
        self.dialog.set_mode('favorites')
        self.assertIn('C2', [self.dialog.favorite_table.item(r, 1).text()
                             for r in range(self.dialog.favorite_table.rowCount())])

    def test_click_during_detail_loading_waits_for_the_complete_gallery(self):
        started, release = threading.Event(), threading.Event()
        factory = self.dialog.factory
        def slow_factory():
            client = factory()
            original = client.product
            def product(row, stop):
                started.set()
                release.wait(3)
                return original(row, stop)
            client.product = product
            return client
        self.dialog.factory = slow_factory
        self.dialog.set_mode('favorites')
        self.dialog.page_loaded(1, [{'part':'C2040', 'product_id':'2392', 'title':'RP2040',
                                    'image':'https://alimg.szlcsc.com/offline.png'}])
        self.dialog.favorite_table.selectRow(0)
        self.wait_until(started.is_set)
        self.dialog.gallery_button.click()
        self.assertFalse(self.dialog.galleries)
        self.assertEqual(self.dialog.pending_gallery_part, 'C2040')
        release.set()
        self.wait_until(lambda: bool(self.dialog.galleries) and self.dialog.galleries[0].view.photo)
        self.assertEqual(self.dialog.galleries[0].thumbnails.count(), 3)

    def test_logout_discards_an_inflight_favorite_response_from_the_previous_account(self):
        self.login()
        self.search_ready('C2')
        started, release = threading.Event(), threading.Event()
        def add(product, stop):
            started.set()
            release.wait(3)
            return {'product': product, 'added': True}
        self.dialog.client.add_favorite = add
        self.dialog.collect_selected()
        self.wait_until(started.is_set)
        self.dialog.clear_session()
        release.set()
        self.wait_until(lambda: not self.dialog.has_jobs())
        self.assertFalse(self.dialog.items)
        self.assertIsNone(self.dialog.client.account)
        self.assertFalse(self.dialog.collecting)

    def test_highlighting_and_previewing_do_not_check_new_rows(self):
        self.search_ready()
        self.assertTrue(self.dialog.preview_button.isEnabled())
        self.dialog.preview_button.click()
        self.assertFalse(self.dialog.selected_items())
        self.assertEqual(self.dialog.search_table.item(0, 0).checkState(), Qt.Unchecked)

    def test_login_is_confirmed_and_favorites_are_automatically_fetched(self):
        self.login()
        self.assertEqual(list(self.dialog.items), ['C2040','C20197','C163691'])
        self.assertEqual(self.dialog.pages_read, 2)
        self.assertIn('已登录', self.dialog.account_label.text())
        self.assertFalse(self.dialog.login_dialog.isVisible())

    def test_selected_import_preserves_queue_results_checkboxes_and_preview_row(self):
        self.window.input.setPlainText('C2040\nC1')
        self.window.load_queue()
        self.window.table.item(0, main.DOWNLOAD_COLUMN).setCheckState(Qt.Unchecked)
        old = SimpleNamespace(title='RP2040', model='', status='成功')
        self.window.results['C2040'] = old
        self.window.table.item(0, main.RESULT_COLUMN).setText('成功')
        self.window.table.selectRow(1)
        self.login()
        self.dialog.table.item(0,0).setCheckState(Qt.Checked)
        self.dialog.table.item(1,0).setCheckState(Qt.Checked)
        self.dialog.import_selected()
        self.assertEqual(self.window.ids, ['C2040','C1','C20197'])
        self.assertEqual(self.window.table.currentRow(), 1)
        self.assertEqual(self.window.table.item(0,main.DOWNLOAD_COLUMN).checkState(), Qt.Unchecked)
        self.assertIs(self.window.results['C2040'], old)
        self.dialog.import_selected()
        self.assertEqual(len(self.window.ids), 3)

    def test_pending_manual_input_is_merged_and_invalid_input_is_preserved(self):
        self.window.input.setPlainText('C1')
        self.window.load_queue()
        self.window.input.setPlainText('C2')
        self.assertEqual(self.window.import_favorites([{'part':'C3'}]), 1)
        self.assertEqual(self.window.ids, ['C1','C2','C3'])
        self.window.input.setPlainText('C4\nbad-input')
        self.assertEqual(self.window.import_favorites([{'part':'C5'}]), 0)
        self.assertEqual(self.window.input.toPlainText(), 'C4\nbad-input')

    def test_downloading_disables_import_but_leaves_search_available(self):
        self.dialog.search_loaded({'items':[{'part':'C2040','title':'RP2040','parameters':[]}], 'page':1,'total':1,'pages':1})
        self.dialog.search_table.item(0, 0).setCheckState(Qt.Checked)
        self.window.set_running(True)
        self.assertFalse(self.dialog.import_button.isEnabled())
        self.assertTrue(self.dialog.search_button.isEnabled())
        self.window.import_favorites([{'part':'C20197'}])
        self.assertEqual(self.window.ids, [])
        self.window.set_running(False)
        self.assertTrue(self.dialog.import_button.isEnabled())

    def test_filter_selects_and_imports_only_visible_favorites(self):
        self.login()
        self.dialog.filter_input.setText('C163691')
        self.dialog.favorite_table.item(2, 0).setCheckState(Qt.Checked)
        self.assertEqual([p['part'] for p in self.dialog.selected_items()], ['C163691'])
        self.dialog.import_selected()
        self.assertEqual(self.window.ids, ['C163691'])

    def test_cancelling_login_discards_late_qr_response(self):
        self.dialog.open_login()
        self.dialog.login_dialog.reject()
        self.wait_until(lambda: not self.dialog.login_dialog.jobs.workers)
        self.assertFalse(self.dialog.login_dialog.token)
        self.assertIsNone(self.dialog.client.account)

    def test_expired_qr_stops_polling_and_can_be_refreshed(self):
        self.dialog.open_login()
        login = self.dialog.login_dialog
        self.wait_until(lambda: bool(login.token))
        login.expires_at = 1
        login.tick()
        self.assertFalse(login.token)
        self.assertFalse(login.poll.isActive())
        login.begin()
        self.wait_until(lambda: bool(login.token))

    def test_old_search_response_cannot_replace_the_new_search(self):
        release = threading.Event()
        original = self.dialog.catalog.search_results_page
        def search(keyword, page, stop):
            if keyword == 'old':
                release.wait(3)
                return {'items':[{'part':'C1','title':'Old','parameters':[]}],'page':1,'total':1,'pages':1}
            return original(keyword, page, stop)
        self.dialog.catalog.search_results_page = search
        self.dialog.search_input.setText('old')
        self.dialog.start_search()
        self.dialog.search_input.setText('C2040')
        self.dialog.start_search()
        self.wait_until(lambda: bool(self.dialog.search_items))
        release.set()
        self.wait_until(lambda: not self.dialog.has_jobs())
        self.assertEqual([p['part'] for p in self.dialog.search_items], ['C2040'])

    def test_stop_keeps_completed_favorites_and_discards_late_pages(self):
        release = threading.Event()
        self.dialog.client.account = {'code':'OFFLINE','name':'Test'}
        def favorites(stop, progress):
            progress(1,[{'part':'C2040','title':'RP2040'}])
            release.wait(3)
            progress(2,[{'part':'C2','title':'Late'}])
            return []
        self.dialog.client.favorites = favorites
        self.dialog.set_mode('favorites')
        self.dialog.start_read()
        self.wait_until(lambda: bool(self.dialog.items))
        self.dialog.favorite_table.item(0, 0).setCheckState(Qt.Checked)
        self.dialog.stop_read()
        release.set()
        self.wait_until(lambda: not self.dialog.has_jobs())
        self.assertEqual(list(self.dialog.items), ['C2040'])
        self.assertTrue(self.dialog.import_button.isEnabled())

    def test_logout_clears_account_favorites_but_keeps_download_queue(self):
        self.login()
        self.dialog.favorite_table.item(0, 0).setCheckState(Qt.Checked)
        self.dialog.import_selected()
        before = self.window.ids[:]
        old = self.dialog.client
        self.dialog.clear_session()
        self.wait_until(lambda: not self.dialog.has_jobs())
        self.assertEqual(self.dialog.items, {})
        self.assertIsNone(self.dialog.client.account)
        self.assertFalse(list(old.session.cookies))
        self.assertEqual(self.window.ids, before)

    def test_parent_close_waits_for_cancelled_requests_to_finish(self):
        started, release = threading.Event(), threading.Event()
        def search(keyword, page, stop):
            started.set()
            release.wait(3)
            return {'items':[], 'page':1,'pages':0,'total':0}
        self.dialog.catalog.search_results_page = search
        self.window.show()
        self.dialog.search_input.setText('C2040')
        self.dialog.start_search()
        self.wait_until(started.is_set)
        self.window.close()
        self.assertTrue(self.window.close_when_finished)
        self.assertTrue(self.dialog.has_jobs())
        release.set()
        self.wait_until(lambda: not self.dialog.has_jobs() and not self.window.isVisible())

    def test_persisted_session_restores_without_creating_another_qr(self):
        with tempfile.TemporaryDirectory() as folder:
            vault = SessionVault(Path(folder)/'session.bin', domains=('127.0.0.1',))
            client = self.service.client()
            client.finish_login('offline-token')
            vault.save(client, vault.generation)
            before = sum(r[1].endswith('get-official-qrcode') for r in self.service.requests)
            old = self.dialog
            old.close()
            self.dialog = FavoritesDialog(self.window, client_factory=self.service.client, vault=vault)
            self.window.favorites_dialog = self.dialog
            self.dialog.activity_finished.connect(self.window.store_activity_finished)
            self.dialog.set_mode('favorites')
            self.dialog.show()
            self.wait_until(lambda: bool(self.dialog.client.account) and bool(self.dialog.items) and not self.dialog.has_jobs())
            self.assertEqual(sum(r[1].endswith('get-official-qrcode') for r in self.service.requests), before)
            self.dialog.clear_session()
            self.wait_until(lambda: not self.dialog.has_jobs())
            self.assertFalse(vault.exists())

    def test_invalid_saved_session_is_cleared_and_login_is_available(self):
        with tempfile.TemporaryDirectory() as folder:
            vault = SessionVault(Path(folder)/'session.bin', domains=('127.0.0.1',))
            client = self.service.client()
            client.finish_login('offline-token')
            vault.save(client, vault.generation)
            self.service.account_valid = False
            self.dialog.close()
            self.dialog = FavoritesDialog(self.window, client_factory=self.service.client, vault=vault)
            self.window.favorites_dialog = self.dialog
            self.dialog.show()
            self.wait_until(lambda: not self.dialog.has_jobs() and not vault.exists())
            self.assertIsNone(self.dialog.client.account)
            self.assertFalse(self.dialog.restoring)
