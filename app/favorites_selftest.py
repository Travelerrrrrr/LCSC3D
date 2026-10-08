"""Offline HTTP service and native UI smoke check, with no real account requests."""
from __future__ import annotations

import json
import base64
import re
import struct
import sys
import threading
import time
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QApplication

from favorites import FavoritesDialog
from store import MemorySession, StoreClient, FAVORITES_URL
from store_session import SessionVault


def fixture_png(width=64, height=64):
    def chunk(name, body):
        return struct.pack('!I', len(body)) + name + body + struct.pack('!I', zlib.crc32(name + body))
    rows = [b'\0' + bytes(0 if (x//8 + y//8) % 2 else 255 for x in range(width)) for y in range(16)]
    pixels = b''.join(rows[y % 16] for y in range(height))
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('!2I5B', width, height, 8, 0, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(pixels)) + chunk(b'IEND', b''))


def fixture_product(part='C2040', title='RP2040'):
    return {'product_code': part, 'display_title': title,
            'description': '本地离线测试数据，非真实商城账号。',
            'images': ['https://alimg.szlcsc.com/offline.png'],
            'attributes': {'Supplier Part': part, 'Manufacturer Part': title, 'Manufacturer': 'Offline fixture',
                           'Supplier Footprint': 'LQFN-56', 'CPU Core': 'ARM Cortex-M0+', 'RAM Size': '264KB',
                           'Datasheet': 'https://item.szlcsc.com/datasheet/RP2040/2392.html',
                           'Symbol': 'fixture-symbol', 'Footprint': 'fixture-footprint', '3D Model': 'fixture-model'}}


def fixture_favorites(rows, page=1, total=4, size=2):
    return {'code': 200, 'result': {'page': {'currPage': page, 'pageRow': size, 'totalRow': total,
            'totalPage': (total + size - 1)//size if size else 0,
            'dataList': [{'productCode': part, 'productModel': title, 'productId': fixture_identity(part),
                          'brandName': 'Offline fixture', 'standard': 'LQFN-56'} for part, title in rows]}}}


def fixture_identity(part):
    return {'C2040': 2392, 'C20618009': 21993682, 'C5879483': 6813573}.get(part, int(part[1:]) + 100)


def fixture_store_record(part='C2040', title='RP2040'):
    urls = [f'https://alimg.szlcsc.com/upload/public/product/breviary/{part}/{index}.png' for index in range(3)]
    record = {'productCode': part, 'productId': str(fixture_identity(part)), 'productModel': title,
            'productGradePlateName': 'Offline fixture', 'encapsulationModel': 'LQFN-56',
            'productType': '单片机(MCU)', 'productName': '本地离线测试商品',
            'remark': '本地离线测试数据，非真实商城账号。',
            'breviaryImageUrl': urls[0], 'luceneBreviaryImageUrls': '<$>'.join(urls),
            'minBuyNumber': 1, 'productUnit': '个', 'stockNumber': 12345,
            'productPriceList': [
                {'startPurchasedNumber': 1, 'endPurchasedNumber': 9, 'productPrice': '0.012345'},
                {'startPurchasedNumber': 10, 'endPurchasedNumber': 99, 'productPrice': '0.010111'},
                {'startPurchasedNumber': 100, 'endPurchasedNumber': -1, 'productPrice': '0.008765'}]}
    if part == 'C1001':
        record['minBuyNumber'] = 5
        record['stockNumber'] = 0
    elif part == 'C1002':
        record['productPriceList'] = []
        record.pop('stockNumber')
    return record


def fixture_search(rows, page=1, total=None, size=30):
    total = len(rows) if total is None else total
    return {'code': 200, 'result': {'searchResult': {'currePage': page, 'pageSize': size,
            'totalCount': total, 'countPage': (total + size - 1) // size,
            'productRecordList': [{'productVO': fixture_store_record(part, title),
                                   'paramLinkedMap': {'CPU Core': 'ARM Cortex-M0+'}}
                                  for part, title in rows]}}}


def fixture_detail(record):
    data = {'props': {'pageProps': {'webData': {'productRecord': record,
            'paramList': [{'parameterName': 'CPU Core', 'parameterValue': 'ARM Cortex-M0+'},
                          {'parameterName': 'RAM Size', 'parameterValue': '264KB'}],
            'pdfFileDetailVO': {'fileUrl': '/upload/public/pdf/source/offline.pdf'}}}}}
    return '<html><script id="__NEXT_DATA__" type="application/json">' + json.dumps(data, ensure_ascii=False) + '</script></html>'


class OfflineStore:
    def __init__(self):
        self.requests = []
        self.scan_state = 'CREATED'
        self.flow_code = 2017
        self.account_valid = True
        self.sso_valid = False
        self.search_pages = {}
        self.catalog_missing = set()
        self.details = {str(fixture_identity(part)): fixture_store_record(part, title)
                        for part, title in [('C2040', 'RP2040'), ('C20197', '4D03WGJ0102T5E'),
                                            ('C163691', 'SMDRS1275-152N'), ('C2', 'New product')]}
        self.extra_favorites = {}
        self.toggle_code = 200
        self.cancel_code = 200
        self.removed_ids = set()
        from gmalg import SM2
        self.login_secret, self.login_public = SM2().generate_keypair()
        self.transport_secret, self.transport_public = SM2().generate_keypair()
        self.credential_code = 2017
        self.risk_required = False
        self.encrypt_auth_response = False
        self.auth_inputs = []
        self.sms_sends = 0
        self.pages = {
            1: fixture_favorites([('C2040', 'RP2040'), ('C20197', '4D03WGJ0102T5E')]),
            2: fixture_favorites([('C2040', 'RP2040'), ('C163691', 'SMDRS1275-152N')], 2)}
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def handle_request(self):
                parsed = urlsplit(self.path)
                body = self.rfile.read(int(self.headers.get('Content-Length', 0)))
                owner.requests.append((self.command, parsed.path, parse_qs(parsed.query), body))
                headers = {}
                mime = 'application/json'
                if parsed.path == '/api/cas/config/get-public-key':
                    value = {'code': 200, 'data': owner.login_public.hex()}
                elif parsed.path == '/api/cas/secret/update':
                    value = {'code': 200, 'data': {'keyId':'offline-crypto', 'privateHexKey':owner.transport_secret.hex(),
                                                  'publicHexKey':owner.transport_public.hex()}}
                elif parsed.path in ('/api/cas/secure/check-login-risk','/api/cas/secure/check-sms-risk'):
                    value = {'code':102280 if owner.risk_required else 200, 'data':None}
                elif parsed.path in ('/api/cas/login/with-password','/api/cas/login/with-sms/send-code','/api/cas/login/with-sms/check-code'):
                    from gmalg import SM2
                    payload = json.loads(body)
                    plain = dict(payload)
                    for key in ('username','password','phoneNumber'):
                        if key in plain:
                            plain[key] = base64.b64decode(SM2(sk=owner.login_secret).decrypt(bytes.fromhex(plain[key]))).decode('utf-8')
                    owner.auth_inputs.append((parsed.path, plain))
                    if parsed.path.endswith('/send-code'):
                        owner.sms_sends += 1
                        value = {'code':200,'data':None}
                    else:
                        code = owner.credential_code
                        auth = 'offline-auth'
                        if owner.encrypt_auth_response:
                            auth = '{secret}' + SM2(pk=owner.transport_public).encrypt(base64.b64encode(auth.encode())).hex()
                        value = {'code':code,'message':'账号、密码或验证码错误' if code not in (200,2017) else None,
                                 'data':{'authCode':auth} if code in (200,2017) else None}
                elif parsed.path == '/api/cas/captcha/get-static-verify-img':
                    value = {'code':200,'data':{'verifyImg':'data:image/png;base64,'+base64.b64encode(fixture_png()).decode(),
                                               'ticketCode':'offline-image-ticket'}}
                elif parsed.path == '/api/cas/captcha/check-static-verify-img':
                    payload = json.loads(body)
                    valid = payload.get('verifyCode') == '1234'
                    value = {'code':200,'data':{'checkSuccess':valid,'captchaTicket':'offline-captcha' if valid else ''}}
                elif parsed.path == '/api/cas/login/get-official-qrcode':
                    value = {'code': 200, 'data': {'token': 'offline-token', 'url': 'https://mp.weixin.qq.com/offline.png', 'expireSeconds': 300}}
                elif parsed.path == '/api/cas/login/get-official-scan-result':
                    value = {'code': 200, 'data': {'status': owner.scan_state}}
                elif parsed.path == '/api/cas/login/get-init-session':
                    value = {'code': 200, 'data': None}
                elif parsed.path == '/api/cas/sso/check-login':
                    value = {'code': 200, 'data': {'isLogin': owner.sso_valid, 'code': 'offline-sso' if owner.sso_valid else None}}
                elif parsed.path == '/api/cas/login/auto-login-with-cookie':
                    value = {'code': 401, 'data': None}
                elif parsed.path == '/api/cas/login/with-official-qrcode':
                    value = {'code': owner.flow_code, 'data': {'authCode': 'offline-auth'}}
                elif parsed.path == '/cas/login':
                    headers['Set-Cookie'] = 'offline_account=1; Path=/; HttpOnly'
                    if owner.sso_valid:
                        owner.account_valid = True
                    value = {'code': 200}
                elif parsed.path == '/cas/user/info':
                    logged = owner.account_valid and 'offline_account=1' in self.headers.get('Cookie', '')
                    value = {'code': 200 if logged else 401, 'result': {'customerCode': 'OFFLINE', 'customerName': '离线测试账号', 'customerLogin': 1} if logged else None}
                elif parsed.path == '/member/favorite/v3':
                    page = int(parse_qs(parsed.query).get('currentPage', ['1'])[0])
                    if owner.extra_favorites or owner.removed_ids:
                        rows = {r['productCode']: r['productModel'] for p in owner.pages.values()
                                for r in p['result']['page']['dataList'] if str(r['productId']) not in owner.removed_ids}
                        rows.update({r['productCode']: r['productModel'] for identity,r in owner.extra_favorites.items()
                                     if identity not in owner.removed_ids})
                        rows = list(rows.items())
                        value = fixture_favorites(rows[(page-1)*2:page*2], page, len(rows))
                    else:
                        value = owner.pages[page]
                elif parsed.path == '/select/product/favorite/v2':
                    identity = parse_qs(parsed.query).get('productIds', [''])[0]
                    ids = {str(r['productId']) for p in owner.pages.values() for r in p['result']['page']['dataList']}
                    ids.update(owner.extra_favorites)
                    ids.difference_update(owner.removed_ids)
                    value = {'code': 200, 'result': [{'productId': identity}] if identity in ids else None}
                elif parsed.path == '/async/favorite/add/dynamic':
                    identity = parse_qs(parsed.query).get('productId', [''])[0]
                    if owner.toggle_code == 200:
                        if identity in owner.extra_favorites:
                            del owner.extra_favorites[identity]
                        else:
                            owner.removed_ids.discard(identity)
                            owner.extra_favorites[identity] = owner.details[identity]
                    value = {'code': owner.toggle_code, 'result': None}
                elif parsed.path == '/async/favorite/cancel':
                    identity = parse_qs(body.decode()).get('productIds', [''])[0]
                    if owner.cancel_code == 200:
                        owner.removed_ids.add(identity)
                        owner.extra_favorites.pop(identity, None)
                    value = {'code':owner.cancel_code,'result':None}
                elif parsed.path == '/query/product':
                    query = json.loads(body)
                    keyword, page = query['keyword'], query['currentPage']
                    title = {'C20197': '4D03WGJ0102T5E', 'C163691': 'SMDRS1275-152N'}.get(keyword, 'RP2040')
                    value = owner.search_pages.get((keyword, page)) or fixture_search([(keyword if keyword.startswith('C') else 'C2040', title)], page)
                    for row in value['result']['searchResult']['productRecordList']:
                        record = row['productVO']
                        owner.details[str(record['productId'])] = record
                elif re.fullmatch(r'/[0-9]+\.html', parsed.path):
                    record = owner.details.get(parsed.path[1:-5])
                    if record is None:
                        self.send_error(404)
                        return
                    value, mime = fixture_detail(record), 'text/html;charset=UTF-8'
                elif parsed.path == '/api/szlcsc/eda/product/list':
                    keyword = parse_qs(parsed.query).get('wd', [''])[0]
                    title = {'C20197': '4D03WGJ0102T5E', 'C163691': 'SMDRS1275-152N'}.get(keyword, 'RP2040')
                    value = {'success': True, 'code': 0, 'result': [] if keyword in owner.catalog_missing else [fixture_product(keyword if keyword.startswith('C') else 'C2040', title)]}
                elif parsed.path.endswith('.png'):
                    value, mime = (fixture_png(640, 480) if '/product/source/' in parsed.path else fixture_png()), 'image/png'
                else:
                    self.send_error(404)
                    return
                if isinstance(value, (dict, list)):
                    value = json.dumps(value, ensure_ascii=False).encode('utf-8')
                elif isinstance(value, str):
                    value = value.encode('utf-8')
                self.send_response(200)
                self.send_header('Content-Type', mime)
                self.send_header('Content-Length', str(len(value)))
                for key, value_header in headers.items():
                    self.send_header(key, value_header)
                self.end_headers()
                self.wfile.write(value)

            do_GET = handle_request
            do_POST = handle_request

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=lambda: self.server.serve_forever(poll_interval=0.02), daemon=True)
        self.thread.start()
        self.base = f'http://127.0.0.1:{self.server.server_port}'

    def client(self):
        base = self.base

        class OfflineSession(MemorySession):
            def request(self, url, **kwargs):
                p = urlsplit(url)
                # All smoke traffic is explicitly redirected to this local fixture.
                body, _ = super().request(base + p.path + ('?' + p.query if p.query else ''), **kwargs)
                return body, url

        return StoreClient(OfflineSession())

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


def native_click(button):
    """Send a real Windows mouse click so foreground rules match user input."""
    if sys.platform != 'win32':
        button.click()
        return
    import ctypes
    from ctypes import wintypes

    class MouseInput(ctypes.Structure):
        _fields_ = [('dx', wintypes.LONG), ('dy', wintypes.LONG), ('data', wintypes.DWORD),
                    ('flags', wintypes.DWORD), ('time', wintypes.DWORD), ('extra', ctypes.c_void_p)]

    class Input(ctypes.Structure):
        _fields_ = [('type', wintypes.DWORD), ('mouse', MouseInput)]

    user = ctypes.windll.user32
    QApplication.processEvents()
    position = user.SetWindowPos
    position.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
                        ctypes.c_int, ctypes.c_int, ctypes.c_uint]
    window = button.window()
    hwnd = int(window.winId())
    position(hwnd, None, 0, 0, 0, 0, 0x13)  # Raise only this test window without activating.
    QApplication.processEvents()
    point = button.mapTo(window, button.rect().center())
    scale = window.devicePixelRatioF()
    target = wintypes.POINT(round(point.x() * scale), round(point.y() * scale))
    to_screen = user.ClientToScreen
    to_screen.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.POINT)]
    assert to_screen(hwnd, ctypes.byref(target)), 'Cannot locate the test button'
    hit = user.WindowFromPoint
    hit.argtypes, hit.restype = [wintypes.POINT], ctypes.c_void_p
    ancestor = user.GetAncestor
    ancestor.argtypes, ancestor.restype = [ctypes.c_void_p, ctypes.c_uint], ctypes.c_void_p
    assert ancestor(hit(target), 2) == hwnd, 'The test button is covered by another window'
    previous = wintypes.POINT()
    user.GetCursorPos(ctypes.byref(previous))
    try:
        user.SetCursorPos(target.x, target.y)
        events = (Input * 2)(Input(0, MouseInput(flags=2)), Input(0, MouseInput(flags=4)))
        send = user.SendInput
        send.argtypes, send.restype = [ctypes.c_uint, ctypes.POINTER(Input), ctypes.c_int], ctypes.c_uint
        assert send(2, events, ctypes.sizeof(Input)) == 2, 'Cannot send the native test click'
        time.sleep(0.05)  # Let Windows dispatch before restoring the cursor position.
        QApplication.processEvents()
    finally:
        user.SetCursorPos(previous.x, previous.y)


def start(window, destination):
    from main import DOWNLOAD_COLUMN, RESULT_COLUMN, VERSION
    QApplication.instance().setQuitOnLastWindowClosed(False)
    destination = Path(destination).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    service = OfflineStore()
    window.request_component_info = lambda: None
    window.show_preview = lambda *args, **kwargs: None
    window.path_input.setText(str(destination / '下载目录'))
    window.input.setPlainText('C2040')
    window.load_queue()
    window.table.item(0, DOWNLOAD_COLUMN).setCheckState(Qt.Unchecked)
    window.table.item(0, RESULT_COLUMN).setText('成功')
    vault = SessionVault(destination / 'offline-session.bin', domains=('127.0.0.1',))
    vault.delete()
    dialog = FavoritesDialog(window, client_factory=service.client, vault=vault)
    window.bind_store(dialog)
    dialog.status.setText('本地离线验证，非真实商城账号。')
    dialog.show()
    dialog.search_input.setText('C2040')
    dialog.start_search()
    timer = QTimer(window)
    timer.setInterval(50)
    deadline = time.monotonic() + 30
    state = {'phase': 'search', 'finished': False}
    report = {}

    def finish(error=''):
        if state['finished']:
            return
        state['finished'] = True
        timer.stop()
        report.update({'version': VERSION, 'frozen': bool(getattr(sys, 'frozen', False)),
                       'offline_fixture': True, 'success': not error, 'error': error,
                       'native_ui': True, 'queue': window.ids,
                       'existing_unchecked': window.table.item(0, DOWNLOAD_COLUMN).checkState() == Qt.Unchecked,
                       'existing_result': window.table.item(0, RESULT_COLUMN).text(),
                       'requests': [{'method': r[0], 'path': r[1]} for r in service.requests]})
        (destination / 'favorites-verification.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        state['exit_code'] = 1 if error else 0
        dialog.shutdown()
        window.close()
        QTimer.singleShot(50, shutdown_when_idle)

    def shutdown_when_idle():
        if dialog.has_jobs():
            QTimer.singleShot(50, shutdown_when_idle)
            return
        service.close()
        QApplication.instance().exit(state['exit_code'])

    def check():
        nonlocal dialog
        try:
            if time.monotonic() > deadline:
                finish('Timed out in ' + state['phase'] + ': ' + dialog.status.text())
                return
            if (state['phase'] == 'search' and dialog.search_items and dialog.current_product
                    and dialog.current_product.get('details_complete') and dialog.image_label.pixmap()
                    and not dialog.image_label.pixmap().isNull()):
                assert dialog.current_product['part'] == 'C2040'
                assert dialog.parameters.rowCount() >= 2
                report['search_part'] = dialog.current_product['part']
                report['search_parameters'] = dialog.parameters.rowCount()
                native_click(dialog.preview_button)
                state['phase'] = 'preview'
            elif state['phase'] == 'preview':
                assert dialog.isVisible(), 'Preview closed the store window'
                import ctypes
                owner = ctypes.windll.user32.GetWindow
                owner.argtypes, owner.restype = [ctypes.c_void_p, ctypes.c_uint], ctypes.c_void_p
                foreground = ctypes.windll.user32.GetForegroundWindow
                foreground.restype = ctypes.c_void_p
                assert not owner(int(dialog.winId()), 4), 'Store has a native owner'
                if foreground() != int(window.winId()):
                    return
                assert window.isActiveWindow(), 'Main window is not active'
                assert not dialog.selected_items()
                report['independent_store_window'] = True
                report['preview_activates_main'] = True
                report['default_unchecked'] = True
                report['preview_keeps_store_open'] = True
                assert window.market_button.text() == '立创商城'
                report['single_store_entry'] = True
                dialog.grab().save(str(destination / '原生搜索-离线验证.png'))
                dialog.gallery_button.click()
                state['phase'] = 'gallery'
            elif state['phase'] == 'gallery' and dialog.galleries and dialog.galleries[0].view.photo:
                gallery = dialog.galleries[0]
                assert gallery.thumbnails.count() == 3
                assert gallery.cache[0].size().width() == 640
                gallery.actual.click()
                gallery.plus.click()
                assert gallery.view.zoom > 1
                report['gallery_originals'] = 3
                report['gallery_zoom'] = True
                gallery.next.click()
                state['phase'] = 'gallery-next'
            elif (state['phase'] == 'gallery-next' and dialog.galleries
                    and dialog.galleries[0].view.photo and dialog.galleries[0].thumbnails.currentRow() == 1):
                gallery = dialog.galleries[0]
                gallery.grab().save(str(destination / '原图查看与放大-离线验证.png'))
                gallery.close()
                dialog.set_mode('favorites')
                window.account_button.click()
                state['phase'] = 'qr'
            elif state['phase'] == 'qr' and dialog.login_dialog.token:
                report['qr_native'] = True
                dialog.login_dialog.status.setText('本地模拟二维码，不用于真实微信扫码。')
                dialog.login_dialog.grab().save(str(destination / '原生扫码-离线验证.png'))
                service.scan_state = 'SUCCESS'
                dialog.login_dialog.poll_status()
                state['phase'] = 'favorites'
            elif state['phase'] == 'favorites' and dialog.client.account and dialog.items and not dialog.busy and not dialog.has_jobs():
                assert list(dialog.items) == ['C2040', 'C20197', 'C163691']
                assert dialog.pages_read == 2
                report['favorite_ids'] = list(dialog.items)
                report['pages'] = dialog.pages_read
                report['login_confirmed'] = True
                assert not dialog.selected_items()
                dialog.table.item(0, 0).setCheckState(Qt.Checked)
                dialog.table.item(2, 0).setCheckState(Qt.Checked)
                dialog.import_button.click()
                assert window.ids == ['C2040', 'C163691']
                assert window.checked_rows() == [1]
                assert window.table.item(0, RESULT_COLUMN).text() == '成功'
                window.set_running(True)
                assert not dialog.import_button.isEnabled()
                window.set_running(False)
                dialog.import_button.click()
                assert window.ids == ['C2040', 'C163691']
                dialog.status.setText('本地离线验证：分页、勾选导入与去重通过。')
                dialog.grab().save(str(destination / '原生收藏-离线验证.png'))
                window.grab().save(str(destination / '主窗口-导入后.png'))
                assert vault.exists()
                assert b'offline_account' not in vault.path.read_bytes()
                report['encrypted_session'] = True
                state['qr_requests'] = sum(r[1].endswith('get-official-qrcode') for r in service.requests)
                dialog.set_mode('search')
                dialog.search_input.setText('C2')
                dialog.start_search()
                state['phase'] = 'add-search'
            elif (state['phase'] == 'add-search' and dialog.current_product
                    and dialog.current_product.get('details_complete') and not dialog.has_jobs()):
                assert dialog.current_product['part'] == 'C2'
                dialog.collect_button.click()
                state['phase'] = 'add-favorite'
            elif state['phase'] == 'add-favorite' and not dialog.has_jobs():
                assert dialog.client.favorite_state(str(fixture_identity('C2')))
                assert 'C2' in dialog.items and not dialog.collecting
                report['account_favorite_add_verified'] = True
                dialog.uncollect_button.click()
                state['phase'] = 'remove-favorite'
            elif state['phase'] == 'remove-favorite' and not dialog.has_jobs():
                assert 'C2' not in dialog.items and not dialog.client.favorite_state(str(fixture_identity('C2')))
                assert window.ids == ['C2040', 'C163691']
                report['account_favorite_cancel_verified'] = True
                rows = [(f'C{1000+i}', f'MCU{i}') for i in range(75)]
                for page in range(1, 4):
                    service.search_pages[('离线单片机', page)] = fixture_search(rows[(page-1)*30:page*30], page, 75)
                dialog.search_input.setText('离线单片机')
                dialog.start_search()
                state['phase'] = 'search-first'
            elif state['phase'] == 'search-first' and not dialog.searching and not dialog.has_jobs():
                assert len(dialog.search_items) == 50 and dialog.search_table.rowCount() == 50
                assert dialog.search_total == 75 and dialog.search_pages == 2
                assert not dialog.selected_items()
                report['first_search_page'] = 50
                report['search_total'] = 75
                report['search_pages'] = 2
                first, second = dialog.search_table.cellWidget(0, 5), dialog.search_table.cellWidget(1, 5)
                assert first.currentData() == 1 and second.currentData() == 5
                assert '0.012345' in first.currentText()
                assert dialog.search_table.item(0, 6).text() == '12,345'
                assert dialog.search_table.item(1, 6).text() == '0'
                assert dialog.search_table.item(2, 5).text() == '—' and dialog.search_table.item(2, 6).text() == '—'
                first.setCurrentIndex(2)
                assert second.currentData() == 5
                report['independent_price_tiers'] = True
                report['minimum_order_tier'] = 5
                report['price_precision'] = True
                report['stock_column'] = True
                report['unknown_price_and_stock'] = True
                dialog.search_table.item(0, 0).setCheckState(Qt.Checked)
                dialog.next_page_button.click()
                state['phase'] = 'search-second'
            elif state['phase'] == 'search-second' and not dialog.searching and not dialog.has_jobs():
                assert dialog.search_page == 2 and dialog.search_table.rowCount() == 25
                assert len(dialog.search_selected) == 1
                assert dialog.search_table.item(0, 0).checkState() == Qt.Unchecked
                dialog.search_table.item(0, 0).setCheckState(Qt.Checked)
                report['second_search_page'] = 25
                dialog.previous_page_button.click()
                state['phase'] = 'search-return'
            elif state['phase'] == 'search-return' and not dialog.searching and not dialog.has_jobs():
                assert dialog.search_page == 1 and dialog.search_table.item(0, 0).checkState() == Qt.Checked
                assert len(dialog.selected_items()) == 2
                report['cross_page_selection'] = True
                assert dialog.search_table.cellWidget(0, 5).currentData() == 100
                report['price_tier_survives_paging'] = True
                dialog.grab().save(str(destination / '分页搜索与跨页勾选-离线验证.png'))
                dialog.close()
                dialog.deleteLater()
                dialog = FavoritesDialog(window, client_factory=service.client, vault=vault)
                window.bind_store(dialog)
                dialog.set_mode('favorites')
                dialog.show()
                state['phase'] = 'restore'
            elif state['phase'] == 'restore' and dialog.client.account and dialog.items and not dialog.busy and not dialog.has_jobs():
                assert list(dialog.items) == ['C2040', 'C20197', 'C163691']
                assert sum(r[1].endswith('get-official-qrcode') for r in service.requests) == state['qr_requests']
                report['restore_without_qr'] = True
                state['phase'] = 'logout'
                dialog.clear_session()
            elif state['phase'] == 'logout' and not dialog.has_jobs():
                assert not dialog.client.account and not dialog.items
                assert not vault.exists()
                assert window.ids == ['C2040', 'C163691']
                report['logout_clears_account'] = True
                window.account_button.click()
                login = dialog.login_dialog
                login.login_tabs.setCurrentIndex(1)
                login.account_input.setText('OFFLINE')
                login.password_input.setText('fixture-password')
                login.grab().save(str(destination / '账号密码登录-离线验证.png'))
                login.password_submit.click()
                state['phase'] = 'password'
            elif state['phase'] == 'password' and dialog.client.account and not dialog.has_jobs():
                assert not dialog.login_dialog.password_input.text()
                assert '账号：' in window.account_button.text()
                report['native_password_login'] = True
                dialog.clear_session()
                state['phase'] = 'sms-open'
            elif state['phase'] == 'sms-open' and not dialog.has_jobs():
                service.risk_required = True
                window.account_button.click()
                login = dialog.login_dialog
                login.login_tabs.setCurrentIndex(2)
                login.phone_input.setText('13800000000')
                login.sms_send_button.click()
                state['phase'] = 'sms-captcha'
            elif state['phase'] == 'sms-captcha' and dialog.login_dialog.captcha_ticket_code:
                login = dialog.login_dialog
                login.grab().save(str(destination / '短信与图片验证-离线验证.png'))
                login.captcha_input.setText('1234')
                login.captcha_verify_button.click()
                state['phase'] = 'sms-code'
            elif state['phase'] == 'sms-code' and dialog.login_dialog.sms_sent_phone:
                login = dialog.login_dialog
                assert service.sms_sends == 1 and not login.sms_send_button.isEnabled()
                login.sms_input.setText('123456')
                login.sms_submit.click()
                state['phase'] = 'sms-login'
            elif state['phase'] == 'sms-login' and dialog.client.account and not dialog.has_jobs():
                assert not dialog.login_dialog.sms_input.text()
                report['native_sms_login'] = True
                report['native_image_captcha'] = True
                dialog.clear_session()
                state['phase'] = 'final-logout'
            elif state['phase'] == 'final-logout' and not dialog.has_jobs():
                assert not vault.exists() and not dialog.client.account
                report['queue_before_delete'] = window.ids[:]
                file = destination / '下载目录/C163691.step'
                file.parent.mkdir(parents=True, exist_ok=True)
                file.write_text('offline existing downloaded model', encoding='utf-8')
                assert window.remove_checked_button.isEnabled()
                window.remove_checked_button.click()
                assert window.ids == ['C2040'] and window.input.toPlainText() == 'C2040'
                assert file.read_text(encoding='utf-8') == 'offline existing downloaded model'
                assert window.table.item(0, RESULT_COLUMN).text() == '成功'
                report['queue_delete_checked'] = True
                report['queue_delete_preserves_files'] = True
                report['queue_after_delete'] = window.ids[:]
                window.grab().save(str(destination / '删除已勾选器件-离线验证.png'))
                dialog.resize(1180, 700)
                dialog.show_detail({'part': 'C499531', 'title': 'SIC461ED-T1-GE3',
                    'description': ('本地离线长介绍，验证窄窗口自动换行与滚动显示。' * 40) + '介绍结束标记',
                    'parameters': [('普通参数', str(i)) for i in range(12)] +
                        [('功能特性', ('轻载高效模式；外部补偿；逐波限流；可调软启动；' * 12) + '参数结束标记')]})
                state['phase'] = 'long-detail'
            elif state['phase'] == 'long-detail' and dialog.detail_scroll.verticalScrollBar().maximum() > 0:
                label = dialog.product_description
                assert label.height() >= label.heightForWidth(label.width())
                assert dialog.parameters.rowHeight(12) > 60
                assert dialog.parameters.viewport().height() >= dialog.parameters.verticalHeader().length()
                report['full_description'] = True
                report['wrapped_parameter_values'] = True
                dialog.grab().save(str(destination / '长商品介绍-离线验证.png'))
                dialog.detail_scroll.verticalScrollBar().setValue(dialog.detail_scroll.verticalScrollBar().maximum())
                state['phase'] = 'long-detail-capture'
            elif state['phase'] == 'long-detail-capture':
                assert dialog.image_label.isVisible() and dialog.datasheet_button.isVisible()
                dialog.grab().save(str(destination / '完整描述与参数-离线验证.png'))
                finish()
        except Exception as exc:
            finish(state['phase'] + ': ' + (str(exc) or type(exc).__name__))

    timer.timeout.connect(check)
    timer.start()


def start_live(window, destination):
    """Explicit read-only verification with the user's already remembered session."""
    from main import VERSION
    from PySide6.QtWebEngineWidgets import QWebEngineView
    QApplication.instance().setQuitOnLastWindowClosed(False)
    destination = Path(destination).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    window.request_component_info = lambda: None
    window.show_preview = lambda *args, **kwargs: None
    dialog = FavoritesDialog(window)
    window.bind_store(dialog)
    dialog.set_mode('favorites')
    dialog.show()
    timer = QTimer(window)
    timer.setInterval(100)
    deadline = time.monotonic() + 150
    state = {'phase': 'restore', 'done': False}
    report = {'version': VERSION, 'frozen': bool(getattr(sys, 'frozen', False)), 'real_account': True,
              'native_ui': not bool(dialog.findChildren(QWebEngineView))}

    def finish(error=''):
        if state['done']:
            return
        state['done'] = True
        state['code'] = 1 if error else 0
        timer.stop()
        report.update({'success': not error, 'error': error})
        (destination / 'store-live-verification.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        window.close()
        QTimer.singleShot(50, exit_when_idle)

    def exit_when_idle():
        if dialog.has_jobs():
            QTimer.singleShot(50, exit_when_idle)
        else:
            QApplication.instance().exit(state['code'])

    def check():
        try:
            if time.monotonic() > deadline:
                finish('Timed out: ' + dialog.status.text())
            elif not dialog.vault.exists() and not dialog.has_jobs() and not dialog.client.account:
                finish('No valid remembered account session; interactive login is required first.')
            elif state['phase'] == 'restore' and dialog.client.account and not dialog.busy and dialog.pages_read:
                report.update({'restored_without_qr': dialog.login_dialog is None,
                               'favorite_count': len(dialog.items), 'favorite_pages': dialog.pages_read})
                if dialog.items:
                    assert not dialog.selected_items()
                    dialog.table.item(0, 0).setCheckState(Qt.Checked)
                    dialog.import_selected()
                    assert len(window.ids) == 1
                    report['favorite_selected_import'] = True
                dialog.set_mode('search')
                dialog.search_input.setText('C2040')
                dialog.start_search()
                state['phase'] = 'search'
            elif state['phase'] == 'search' and dialog.search_items and not dialog.has_jobs():
                assert dialog.current_product['part'] == 'C2040'
                assert dialog.parameters.rowCount() > 0
                report.update({'search_count': len(dialog.search_items), 'search_exact_part': 'C2040',
                               'search_parameter_count': dialog.parameters.rowCount(),
                               'image_ready': bool(dialog.image_label.pixmap() and not dialog.image_label.pixmap().isNull()),
                               'remembered_session_file': dialog.vault.exists()})
                dialog.search_input.setText('C20618009')
                dialog.start_search()
                state['phase'] = 'oscillator'
            elif state['phase'] == 'oscillator' and not dialog.searching and not dialog.has_jobs():
                product = dialog.current_product
                assert product and product['part'] == 'C20618009' and product.get('details_complete')
                assert product['package'] == 'SMD5032-6P'
                assert len(product['parameters']) == 7 and len(product['images']) == 4
                report['fixed_oscillator'] = {'part':'C20618009', 'parameters':7, 'images':4, 'package':product['package']}
                dialog.search_input.setText('C5879483')
                dialog.start_search()
                state['phase'] = 'resistor'
            elif state['phase'] == 'resistor' and not dialog.searching and not dialog.has_jobs():
                product = dialog.current_product
                assert product and product['part'] == 'C5879483' and product.get('details_complete')
                assert len(product['images']) == 3
                dialog.gallery_button.click()
                state['phase'] = 'gallery'
            elif state['phase'] == 'gallery' and dialog.galleries and dialog.galleries[0].view.photo:
                gallery = dialog.galleries[0]
                assert gallery.thumbnails.count() == 3
                pixmap = gallery.cache[0]
                assert pixmap.width() > 1000 and pixmap.height() > 1000
                gallery.actual.click()
                gallery.plus.click()
                assert gallery.view.zoom > 1
                report['resistor_gallery'] = {'images':3, 'original_size':[pixmap.width(), pixmap.height()], 'zoom':True}
                gallery.grab().save(str(destination / '电阻原图-实网验证.png'))
                gallery.close()
                dialog.search_input.setText('单片机')
                dialog.start_search()
                state['phase'] = 'page-first'
            elif state['phase'] == 'page-first' and not dialog.searching and not dialog.has_jobs():
                assert len(dialog.search_items) == 50 and dialog.search_table.rowCount() == 50
                assert dialog.search_total > 50 and not dialog.selected_items()
                report['mcu_total'] = dialog.search_total
                report['mcu_pages'] = dialog.search_pages
                report['first_mcu_page_count'] = 50
                state['first_part'] = dialog.search_items[0]['part']
                dialog.search_table.item(0, 0).setCheckState(Qt.Checked)
                dialog.next_page_button.click()
                state['phase'] = 'page-second'
            elif state['phase'] == 'page-second' and not dialog.searching and not dialog.has_jobs():
                assert dialog.search_page == 2 and dialog.search_table.rowCount() == 50
                assert len(dialog.search_selected) == 1
                state['second_part'] = dialog.search_items[0]['part']
                assert state['second_part'] != state['first_part']
                dialog.search_table.item(0, 0).setCheckState(Qt.Checked)
                report['second_mcu_page_count'] = 50
                dialog.previous_page_button.click()
                state['phase'] = 'page-return'
            elif state['phase'] == 'page-return' and not dialog.searching and not dialog.has_jobs():
                assert dialog.search_page == 1 and dialog.search_table.item(0, 0).checkState() == Qt.Checked
                assert len(dialog.selected_items()) == 2
                report['cross_page_selection'] = True
                report['header_account_synced'] = '账号：' in window.account_button.text()
                dialog.search_input.setText('C499531')
                dialog.start_search()
                state['phase'] = 'dcdc'
            elif state['phase'] == 'dcdc' and not dialog.searching and not dialog.has_jobs():
                product = dialog.current_product
                assert product and product['part'] == 'C499531' and product.get('details_complete')
                combo = dialog.search_table.cellWidget(0, 5)
                assert combo and combo.currentData() == 1 and combo.count() > 1
                assert dialog.search_table.item(0, 6).text() != '—'
                assert any(k == '功能特性' and '可调限流' in v for k, v in product['parameters'])
                assert dialog.product_description.height() >= dialog.product_description.heightForWidth(dialog.product_description.width())
                report['dcdc_commerce'] = {'part': product['part'], 'tiers': combo.count(),
                    'default_quantity': combo.currentData(), 'stock': product.get('stock'),
                    'description_length': len(product['description']), 'parameter_count': len(product['parameters'])}
                combo.setCurrentIndex(1)
                assert combo.currentData() == product['price_tiers'][1]['quantity']
                report['real_price_tier_switch'] = True
                dialog.account_label.setText('已登录 · 实网验证（账号信息已隐藏）')
                state['phase'] = 'dcdc-description'
            elif state['phase'] == 'dcdc-description':
                dialog.grab().save(str(destination / '商品介绍与阶梯价格-实网验证.png'))
                dialog.detail_scroll.verticalScrollBar().setValue(dialog.detail_scroll.verticalScrollBar().maximum())
                state['phase'] = 'dcdc-capture'
            elif state['phase'] == 'dcdc-capture':
                dialog.grab().save(str(destination / '商品价格库存与完整参数-实网验证.png'))
                finish()
        except Exception as exc:
            finish(str(exc) or type(exc).__name__)

    timer.timeout.connect(check)
    timer.start()
