"""Final-EXE shopping smoke test; all account and checkout traffic is local."""
import json
from pathlib import Path
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from PySide6.QtCore import QPoint, Qt, QTimer, QUrl
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from lcsc3d.core.app_logging import log_event, get_log_directory
from lcsc3d.services.log_support import package_logs, clear_logs
from lcsc3d.services.store import product_page
from lcsc3d.services.commerce import CART_URL, ORDERS_URL
from lcsc3d.selftests.favorites_selftest import OfflineStore
from lcsc3d.ui import commerce_actions
from lcsc3d.ui.commerce import CommerceBrowser, CommercePage
from lcsc3d.ui.favorites import FavoritesDialog


def start(window, folder):
    main = sys.modules[type(window).__module__]
    root = Path(folder).resolve(); root.mkdir(parents=True, exist_ok=True)
    report = dict(version=main.VERSION, frozen=bool(getattr(sys, 'frozen', False)))
    state = dict(mode='start', deadline=time.monotonic() + 60, done=False)
    service = OfflineStore()
    client = service.client()
    client.session.request('https://www.szlcsc.com/cas/login')
    client.account = client.account_info()
    window.request_component_info = lambda: None
    window.show_preview = lambda *args, **kw: None

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            if self.path == '/cart':
                body = '<h1>购物车 · 离线验收</h1><p>C2861386 × 5</p><a id="checkout" target="_blank" href="/checkout">去结算</a>'
            elif self.path == '/checkout':
                body = '<h1>确认订单 · 离线验收</h1><p>核对数量、收货地址与金额后确认；此测试不创建真实订单。</p>'
            else:
                body = '<h1>我的订单 · 离线验收</h1><p>暂无测试订单</p>'
            body = ('<!doctype html><meta charset="utf-8"><style>body{font:20px sans-serif;padding:36px;background:#f4f7fa}a{display:block;padding:22px;background:#0093e6;color:white;width:180px;text-align:center;border-radius:8px}</style>' + body).encode()
            self.send_response(200); self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(body))); self.end_headers(); self.wfile.write(body)

    web = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=web.serve_forever, daemon=True); thread.start()
    base = f'http://127.0.0.1:{web.server_port}'
    original_navigation = CommercePage.acceptNavigationRequest
    def navigation(page, url, kind, frame):
        return True if url.toString().startswith(base + '/') else original_navigation(page, url, kind, frame)
    CommercePage.acceptNavigationRequest = navigation

    class LocalBrowser(CommerceBrowser):
        def navigate(self, target):
            self.target = target
            if self.ready:
                self.view.load(QUrl(base + ('/orders' if target == ORDERS_URL else '/cart')))

    original_browser = commerce_actions.CommerceBrowser
    commerce_actions.CommerceBrowser = LocalBrowser
    dialog = window.bind_store(FavoritesDialog(window, client_factory=lambda: client, vault=False))
    dialog.request_detail = lambda product: None
    timer = QTimer(window); timer.setInterval(25)

    def finish(error=''):
        if state['done']:
            return
        state['done'] = True; timer.stop()
        report.update(success=not error, error=error, stage=state['mode'])
        (root / 'commerce-verification.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), 'utf-8')
        commerce_actions.CommerceBrowser = original_browser
        CommercePage.acceptNavigationRequest = original_navigation
        dialog.shutdown()
        def close():
            if dialog.has_jobs():
                QTimer.singleShot(50, close)
                return
            service.close(); web.shutdown(); web.server_close(); thread.join(timeout=2)
            window.close(); QApplication.instance().exit(1 if error else 0)
        close()

    def tick():
        try:
            if time.monotonic() > state['deadline']:
                raise AssertionError('Shopping verification timed out')
            mode = state['mode']
            if mode == 'start':
                window.open_favorites()
                assert dialog.commerce_browser is None
                assert [dialog.tabs.tabText(i) for i in range(4)] == ['搜索商品', '我的收藏', '购物车', '我的订单']
                record = {'productCode': 'C2861386', 'productId': 3066063, 'productModel': 'TLV75718PDRVR',
                          'fileTypeVOList': [{'fileType': 'pdf_property', 'detailVOList': [
                              {'linkAddress': 'https://www.ti.com/cn/lit/ds/symlink/tlv757p.pdf'}]}], 'minBuyNumber': 1}
                html = '<script id="__NEXT_DATA__">' + json.dumps({'props': {'pageProps': {'webData': {'productRecord': record}}}}) + '</script>'
                product = product_page(html, 'C2861386')
                dialog.append_row(dialog.search_table, product)
                dialog.search_table.selectRow(0)
                dialog.search_table.item(0, 0).setCheckState(Qt.Checked)
                assert dialog.datasheet_button.isEnabled()
                state['product'] = product
                report['datasheet_new_schema'] = report['lazy_four_tabs'] = True
                menu = dialog.make_product_menu([product], dialog)
                assert len(menu.actions()) == 3
                menu.actions()[0].trigger()
                assert 'C2861386' in window.ids
                report['context_download'] = True
                for toast in list(window.notifications.toasts):
                    toast.close()
                window.grab().save(str(root / 'store.png'))
                dialog.cart_button.click()
                dialog.cart_dialog.inputs[0].setValue(5)
                window.grab().save(str(root / 'add-cart.png'))
                dialog.cart_dialog.submit.click(); dialog.cart_dialog.submit.click()
                state['mode'] = 'added'
            elif mode == 'added' and not dialog.cart_busy:
                assert len(service.cart_adds) == 1, service.cart_adds
                assert service.cart_adds[0]['productNumber'] == ['5']
                report['quantity_and_single_write'] = True
                dialog.cart_dialog.accept()
                dialog.tabs.setCurrentIndex(2)
                state['mode'] = 'cart'
            elif mode == 'cart' and dialog.commerce_browser:
                browser = dialog.commerce_browser
                if browser.view.url().path() != '/cart' or browser.view.page().isLoading():
                    return
                assert browser.profile.isOffTheRecord()
                assert not dialog.inspector.isVisible() and not dialog.product_actions.isVisible()
                report['full_width_memory_browser'] = True
                window.grab().save(str(root / 'cart.png'))
                browser.page.runJavaScript("(()=>{let r=document.querySelector('#checkout').getBoundingClientRect();return JSON.stringify([r.x+r.width/2,r.y+r.height/2])})()", lambda result: state.update(click=json.loads(result) if result else None))
                state['mode'] = 'click'
            elif mode == 'click' and state.get('click'):
                browser = dialog.commerce_browser
                QTest.mouseClick(browser.view.focusProxy(), Qt.LeftButton, Qt.NoModifier, QPoint(*map(int, state['click'])))
                state['mode'] = 'checkout'
            elif mode == 'checkout':
                browser = dialog.commerce_browser
                if browser.view.url().path() != '/checkout' or browser.page.isLoading():
                    return
                report['checkout_new_window_stays_embedded'] = True
                window.grab().save(str(root / 'checkout.png'))
                state['browser'] = browser
                dialog.tabs.setCurrentIndex(3)
                state['mode'] = 'orders'
            elif mode == 'orders':
                browser = dialog.commerce_browser
                if browser.view.url().path() != '/orders' or browser.page.isLoading():
                    return
                assert state['browser'] is browser
                report['orders_reuses_browser'] = True
                window.grab().save(str(root / 'orders.png'))
                dialog.tabs.setCurrentIndex(0)
                assert dialog.inspector.isVisible() and dialog.product_actions.isVisible()
                assert dialog.search_table.item(0, 0).checkState() == Qt.Checked
                dialog.clear_session()
                assert dialog.commerce_browser is None
                report['logout_clears_browser'] = True
                state['mode'] = 'finish'
            elif mode == 'finish' and not dialog.has_jobs():
                events = (get_log_directory() / 'LCSC3D.log').read_text('utf-8')
                assert 'cart.add_confirmed' in events and 'cart.batch_completed' in events
                bundle = package_logs(); assert bundle['path'].is_file()
                assert not clear_logs()['failures']
                assert log_event('INFO', 'verification.commerce_after_clear')
                assert 'verification.commerce_after_clear' in (get_log_directory() / 'LCSC3D.log').read_text('utf-8')
                report['logs_package_and_continue_after_clear'] = True
                report['single_native_window'] = [w for w in QApplication.topLevelWidgets() if w.isVisible()] == [window]
                assert report['single_native_window']
                finish()
        except Exception as exc:
            finish(type(exc).__name__ + ': ' + str(exc))
    timer.timeout.connect(tick); timer.start()
