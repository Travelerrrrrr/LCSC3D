"""Lazy official commerce view and a compact, editable batch-add page."""
from __future__ import annotations

import http.cookiejar

from PySide6.QtCore import QDateTime, QTimer, QUrl, Qt, Signal
from PySide6.QtNetwork import QNetworkCookie, QNetworkProxy, QNetworkProxyFactory
from PySide6.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget, QSpinBox, QHeaderView
from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile, QWebEngineSettings
from PySide6.QtWebEngineWidgets import QWebEngineView

from lcsc3d.core.app_logging import log_event
from lcsc3d.core.app_paths import data_directory
from lcsc3d.core.app_settings import proxy_mode
from lcsc3d.core.i18n import text as tr, message as msg
from lcsc3d.services.commerce import CART_URL, ORDERS_URL, MAX_CART_QUANTITY
from lcsc3d.services.store import quantity
from lcsc3d.ui.localized_widgets import QDialog, QLabel, QPushButton, QTableWidget, QTableWidgetItem
from lcsc3d.ui.ui_components import title_block, surface


def cookie_key(cookie):
    return (cookie.domain().lstrip('.'), cookie.path() or '/', bytes(cookie.name()))


def browser_cookie(cookie):
    result = QNetworkCookie(cookie.name.encode(), cookie.value.encode())
    result.setDomain(cookie.domain)
    result.setPath(cookie.path or '/')
    result.setSecure(cookie.secure)
    result.setHttpOnly(cookie.has_nonstandard_attr('HttpOnly'))
    if cookie.expires is not None:
        result.setExpirationDate(QDateTime.fromSecsSinceEpoch(cookie.expires))
    return result


def native_cookie(cookie):
    return http.cookiejar.Cookie(0, bytes(cookie.name()).decode('utf-8'), bytes(cookie.value()).decode('utf-8'),
                                 None, False, cookie.domain(), bool(cookie.domain()), cookie.domain().startswith('.'),
                                 cookie.path() or '/', True, cookie.isSecure(),
                                 None if cookie.isSessionCookie() else cookie.expirationDate().toSecsSinceEpoch(),
                                 cookie.isSessionCookie(), None, None,
                                 {'HttpOnly': None} if cookie.isHttpOnly() else {})


def commerce_host(host):
    return host == 'szlcsc.com' or host.endswith('.szlcsc.com') or host == 'passport.jlc.com'


class CommercePage(QWebEnginePage):
    blocked = Signal()
    login_required = Signal()

    def acceptNavigationRequest(self, url, kind, main_frame):
        if url.toString() == 'about:blank':
            return True
        if main_frame and url.host() == 'passport.jlc.com' and url.path().startswith('/login'):
            self.login_required.emit()
            return False
        if url.scheme() == 'https' and commerce_host(url.host()) and not url.userName() and not url.password():
            return True
        if main_frame:
            self.blocked.emit()
            log_event('WARNING', 'commerce.navigation_blocked')
        return False

    def javaScriptConsoleMessage(self, *args):
        # Site console output can include account/checkout data.
        pass


class CommerceBrowser(QWidget):
    """One memory-only browser shared by Cart and Orders, created on first use."""
    login_required = Signal()
    def __init__(self, parent=None):
        super().__init__(parent)
        self.cookies = {}
        self.pending = set()
        self.ready = False
        self.seed_cookies = None
        self.target = CART_URL
        self.profile = QWebEngineProfile(self)
        self.profile.setPersistentStoragePath(str(data_directory() / 'commerce'))
        self.profile.setCachePath(str(data_directory() / 'commerce' / 'cache'))
        self.profile.setPersistentCookiesPolicy(QWebEngineProfile.NoPersistentCookies)
        self.profile.setHttpCacheType(QWebEngineProfile.MemoryHttpCache)
        self.profile.setHttpCacheMaximumSize(8 * 1024 * 1024)
        self.profile.downloadRequested.connect(lambda download: download.cancel())
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        tools = QHBoxLayout()
        self.back = QPushButton(tr('返回'))
        self.refresh = QPushButton(tr('刷新'))
        self.home = QPushButton(tr('返回购物车'))
        for button in (self.back, self.refresh, self.home):
            tools.addWidget(button)
        self.hint = QLabel(tr('正在同步商城登录…'))
        self.hint.setWordWrap(True)
        tools.addWidget(self.hint, 1)
        layout.addLayout(tools)
        self.view = QWebEngineView(self)
        self.page = CommercePage(self.profile, self.view)
        self.view.setPage(self.page)
        self.page.settings().setAttribute(QWebEngineSettings.LocalContentCanAccessRemoteUrls, False)
        self.page.settings().setAttribute(QWebEngineSettings.JavascriptCanOpenWindows, True)
        self.page.newWindowRequested.connect(self.open_window)
        self.page.blocked.connect(lambda: self.hint.setText(tr('此链接需在浏览器中处理；请返回商城继续。')))
        self.page.login_required.connect(self.login_required)
        self.view.loadFinished.connect(self.loaded)
        self.page.renderProcessTerminated.connect(self.crashed)
        self.page.certificateError.connect(self.certificate_failed)
        self.view.loadStarted.connect(lambda: self.hint.setText(tr('正在加载商城页面…')))
        self.back.clicked.connect(self.view.back)
        self.refresh.clicked.connect(self.reload)
        self.home.clicked.connect(lambda: self.navigate(CART_URL))
        layout.addWidget(self.view, 1)
        jar = self.profile.cookieStore()
        jar.cookieAdded.connect(self.cookie_added)
        jar.cookieRemoved.connect(self.cookie_removed)
        self.sync_timer = QTimer(self)
        self.sync_timer.setSingleShot(True)
        self.sync_timer.timeout.connect(self.sync_failed)

    def initialize(self, cookies, target):
        self.target = target
        values = [browser_cookie(c) for c in cookies if commerce_host(c.domain.lstrip('.'))]
        self.pending = {cookie_key(c) for c in values}
        if not self.pending:
            self.finish_sync()
            return
        self.sync_timer.start(8000)
        # Chromium creates its network context on the first navigation. Seed
        # only after that context exists, before visiting an authenticated URL.
        self.seed_cookies = values
        self.view.load(QUrl('about:blank'))

    def cookie_added(self, cookie):
        if not commerce_host(cookie.domain().lstrip('.')):
            return
        key = cookie_key(cookie)
        self.cookies[key] = native_cookie(cookie)
        self.pending.discard(key)
        if not self.ready and not self.pending:
            self.finish_sync()

    def cookie_removed(self, cookie):
        # Send an expired tombstone back to native requests too.
        value = native_cookie(cookie)
        value.expires = 1
        self.cookies[cookie_key(cookie)] = value

    def finish_sync(self):
        self.sync_timer.stop()
        self.ready = True
        self.navigate(self.target)

    def sync_failed(self):
        self.hint.setText(tr('商城登录同步超时，请切回搜索后重新打开此页。'))
        log_event('WARNING', 'commerce.session_sync_failed', count=len(self.pending))

    def navigate(self, target):
        self.target = target
        if self.ready:
            self.apply_proxy()
            log_event('INFO', 'commerce.open', kind='orders' if target == ORDERS_URL else 'cart')
            self.view.load(QUrl(target))

    def reload(self):
        if self.ready:
            self.apply_proxy()
            self.view.reload()

    def apply_proxy(self):
        system = proxy_mode('store') == 'system'
        QNetworkProxyFactory.setUseSystemConfiguration(system)
        QNetworkProxy.setApplicationProxy(QNetworkProxy(QNetworkProxy.DefaultProxy if system else QNetworkProxy.NoProxy))

    def open_window(self, request):
        # Official checkout uses window.open; keep it in this same page.
        if request.isUserInitiated():
            request.openIn(self.page)

    def loaded(self, ok):
        if self.seed_cookies is not None:
            values, self.seed_cookies = self.seed_cookies, None
            if ok:
                for cookie in values:
                    self.profile.cookieStore().setCookie(cookie)
            return
        if not self.ready:
            return
        self.hint.setText(tr('商城官方页面 · 核对数量、地址与金额后确认下单') if ok else
                          tr('商城页面加载失败，请刷新重试。'))
        log_event('INFO' if ok else 'WARNING', 'commerce.page_loaded', success=ok)

    def crashed(self, status, code):
        self.hint.setText(tr('商城页面已停止，请刷新重试。'))
        log_event('ERROR', 'commerce.renderer_failed', exit_code=code)

    def certificate_failed(self, error):
        error.rejectCertificate()
        log_event('WARNING', 'commerce.certificate_failed')

    def dispose(self):
        self.sync_timer.stop()
        self.view.stop()
        self.cookies.clear()
        self.hide()
        self.setParent(None)
        # Profile must outlive its pages.
        self.page.destroyed.connect(self.deleteLater)
        self.page.deleteLater()


class CartQuantityDialog(QDialog):
    submitted = Signal(object)

    def __init__(self, products, parent=None):
        super().__init__(parent)
        self.products = products
        self.setWindowTitle(tr('添加到购物车'))
        self.resize(780, 500)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 16)
        layout.setSpacing(14)
        layout.addWidget(title_block(tr('添加到购物车'), msg('已选择 {0} 个器件', len(products)), large=True))
        card, body = surface()
        hint = QLabel(tr('填写本次增加的数量；购物车已有数量会累加。价格和库存以商城结算时为准。'))
        hint.setWordWrap(True)
        hint.setObjectName('muted')
        body.addWidget(hint)
        self.table = QTableWidget(len(products), 4)
        self.table.setHorizontalHeaderLabels([tr('C 编号'), tr('型号'), tr('起订量'), tr('增加数量')])
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setShowGrid(False)
        self.table.setAlternatingRowColors(True)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.table.setColumnWidth(3, 155)
        self.table.verticalHeader().hide()
        self.inputs = []
        for row, product in enumerate(products):
            minimum = max(1, min(MAX_CART_QUANTITY, quantity(product.get('min_quantity')) or 1))
            for col, value in enumerate((product['part'], product.get('title', ''),
                                         str(minimum) if product.get('min_quantity') else '—')):
                self.table.setItem(row, col, QTableWidgetItem(value))
            spin = QSpinBox()
            spin.setRange(minimum, MAX_CART_QUANTITY)
            spin.setValue(minimum)
            spin.ensurePolished()
            self.table.setRowHeight(row, max(52, spin.sizeHint().height() + 12))
            self.table.setCellWidget(row, 3, spin)
            self.inputs.append(spin)
        body.addWidget(self.table, 1)
        layout.addWidget(card, 1)
        self.status = QLabel('')
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        actions = QHBoxLayout()
        actions.addStretch()
        self.cancel = QPushButton(tr('取消'))
        self.cancel.clicked.connect(self.reject)
        actions.addWidget(self.cancel)
        self.submit = QPushButton(tr('添加到购物车'))
        self.submit.setObjectName('primary')
        self.submit.clicked.connect(self.send)
        actions.addWidget(self.submit)
        layout.addLayout(actions)

    def send(self):
        self.submit.setEnabled(False)
        self.cancel.setText(tr('关闭'))
        self.table.setEnabled(False)
        self.status.setText(tr('正在加入购物车…'))
        self.submitted.emit([(dict(p), spin.value()) for p, spin in zip(self.products, self.inputs)])

    def completed(self, result):
        good, failed, unattempted = result['added'], result['failed'], result['unattempted']
        self.status.setText(msg('加购成功 {0} 项；未确认 {1} 项；未发送 {2} 项。', len(good), len(failed), len(unattempted)))
        if failed:
            self.status.setText(self.status.text() + '\n' + tr('请先核对购物车再重试，避免重复增加数量。') + '\n' + failed[0][1])
        self.cancel.setText(tr('完成'))
