"""Native search, product detail, QR login and account favorite widgets."""
from __future__ import annotations

from i18n import text as ui_text, message as ui_message
import threading
import time

from PySide6.QtCore import QObject, Qt, QThread, QTimer, QUrl, Signal, Slot, QEvent
from PySide6.QtGui import QDesktopServices, QPixmap
from PySide6.QtWidgets import (QAbstractItemView, QFrame, QHBoxLayout, QHeaderView, QScrollArea, QSizePolicy, QSplitter, QVBoxLayout, QWidget)
from localized_widgets import (QCheckBox, QComboBox, QDialog, QLabel, QLineEdit, QPushButton, QTableWidget, QTableWidgetItem, QTabWidget)

from errors import Cancelled
from store import (FAVORITES_URL, SEARCH_PAGE_SIZE, StoreClient, StoreError, SessionExpired,
                   CaptchaRequired, is_favorites_url, normalize_items)
from store_session import SessionVault, MemoryOnlyVault
from international_store import catalog_client, HOME
from app_theme import theme_manager
from ui_components import IconButton, title_block, scroll_page, surface, ColumnSplitter
from shell_ui import notify
from shiboken6 import isValid
from store_images import ImageGallery, ProductImage
from store_diagnostics import record_request_error
from app_logging import (log_event, new_context, log_context, contextual, record_error, traced, safe_part)


class RequestWorker(QThread):
    loaded = Signal(object, object)
    progress = Signal(int, object)

    def __init__(self, channel, revision, operation, parent):
        super().__init__(parent)
        self.channel, self.revision, self.operation = channel, revision, operation
        self.cancelled = threading.Event()
        self.log_context = new_context(operation=channel, revision=revision, feature='store')

    @contextual
    def run(self):
        if self.cancelled.is_set():
            return
        try:
            log_event('DEBUG', 'store.job_started', operation=self.channel)
            value = self.operation(self.cancelled, self.progress.emit)
            if not self.cancelled.is_set():
                log_event('DEBUG', 'store.job_completed', operation=self.channel)
                self.loaded.emit(value, None)
        except Cancelled:
            log_event('DEBUG', 'store.job_cancelled', operation=self.channel)
        except Exception as exc:
            if not self.cancelled.is_set():
                if isinstance(exc, StoreError):
                    if isinstance(exc, CaptchaRequired):
                        log_event('INFO', 'store.challenge_required', operation=self.channel)
                    else:
                        record_request_error(exc, self.channel)
                    error = exc
                else:
                    recorded = record_request_error(exc, self.channel)
                    detail = ui_text('，诊断已保存到日志，可在「设置」中打开') if recorded else ''
                    error = StoreError(ui_message('请求处理异常（{0}）{1}，请重试。', type(exc).__name__, detail))
                self.loaded.emit(None, error)


class Jobs(QObject):
    idle = Signal()

    def __init__(self, parent):
        super().__init__(parent)
        self.workers, self.revisions = {}, {}

    def cancel(self, channel):
        self.revisions[channel] = self.revisions.get(channel, 0) + 1
        for worker in self.workers:
            if worker.channel == channel:
                with log_context(worker.log_context):
                    log_event('INFO', 'store.cancel_requested')
                worker.cancelled.set()

    def cancel_all(self, except_channels=()):
        for channel in set(self.revisions):
            if channel not in except_channels:
                self.cancel(channel)

    def start(self, channel, operation, success, failure, progress=None):
        self.cancel(channel)
        worker = RequestWorker(channel, self.revisions[channel], operation, self)
        self.workers[worker] = (success, failure, progress)
        worker.loaded.connect(self._loaded)
        worker.progress.connect(self._progress)
        worker.finished.connect(self._finished)
        worker.start()
        return worker

    def current(self, worker):
        return worker in self.workers and not worker.cancelled.is_set() and self.revisions.get(worker.channel) == worker.revision

    @Slot(object, object)
    def _loaded(self, value, error):
        worker = self.sender()
        if self.current(worker):
            success, failure, _ = self.workers[worker]
            with log_context(worker.log_context):
                try:
                    (failure if error is not None else success)(error if error is not None else value)
                except Exception as exc:
                    record_error(exc, 'store.ui_callback_failed')
                    raise
        elif worker is not None:
            with log_context(worker.log_context):
                log_event('DEBUG', 'store.stale_result_ignored')

    @Slot(int, object)
    def _progress(self, page, items):
        worker = self.sender()
        if self.current(worker) and self.workers[worker][2]:
            with log_context(worker.log_context):
                self.workers[worker][2](page, items)

    @Slot()
    def _finished(self):
        worker = self.sender()
        self.workers.pop(worker, None)
        worker.deleteLater()
        if not self.workers:
            self.idle.emit()


def plain_label(value='', role=None):
    label = QLabel(value)
    label.setTextFormat(Qt.PlainText)
    label.setWordWrap(True)
    if role:
        label.setObjectName(role)
    return label


class DetailLabel(QLabel):
    """Recompute wrapped text height from its actual scroll-area width."""
    def __init__(self, value='', role=None):
        super().__init__(value)
        self.setTextFormat(Qt.PlainText)
        self.setWordWrap(True)
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        self.setTextInteractionFlags(Qt.TextSelectableByMouse)
        if role:
            self.setObjectName(role)
        self.reflow = QTimer(self)
        self.reflow.setSingleShot(True)
        self.reflow.timeout.connect(self.fit_text)
        self.reflow.start(0)

    def setText(self, value):
        super().setText(value)
        self.reflow.start(0)

    def clear(self):
        self.setText('')

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if event.size().width() != event.oldSize().width():
            self.reflow.start(0)

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() in (QEvent.FontChange, QEvent.ApplicationFontChange, QEvent.StyleChange) and hasattr(self, 'reflow'):
            self.reflow.start(0)

    def fit_text(self):
        height = max(0, self.heightForWidth(self.width())) if self.text() else 0
        if self.minimumHeight() != height or self.maximumHeight() != height:
            self.setFixedHeight(height)


class ParameterTable(QTableWidget):
    """Wrap every value and let the surrounding details area scroll the table."""
    def __init__(self):
        super().__init__(0, 2)
        self.setHorizontalHeaderLabels([ui_text('参数'), ui_text('数值')])
        self.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.setWordWrap(True)
        self.setTextElideMode(Qt.ElideNone)
        self.verticalHeader().hide()
        self.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.reflow = QTimer(self)
        self.reflow.setSingleShot(True)
        self.reflow.timeout.connect(self.fit_rows)
        self.horizontalHeader().sectionResized.connect(lambda *args: self.reflow.start(0))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if event.size().width() != event.oldSize().width():
            self.reflow.start(0)

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() in (QEvent.FontChange, QEvent.ApplicationFontChange, QEvent.StyleChange) and hasattr(self, 'reflow'):
            self.reflow.start(0)

    def fit_rows(self):
        self.resizeRowsToContents()
        height = self.horizontalHeader().height() + self.verticalHeader().length() + 2 * self.frameWidth() + 2
        if self.height() != height:
            self.setFixedHeight(height)


class LoginDialog(QDialog):
    logged_in = Signal(object)
    activity_finished = Signal()

    def __init__(self, parent, client_factory=StoreClient):
        super().__init__(parent)
        self.factory = client_factory
        self.jobs = Jobs(self)
        self.jobs.idle.connect(self.activity_finished)
        self.active, self.token, self.client = False, '', None
        self.expires_at = 0
        self.credential_busy = False
        self.pending_action = ''
        self.captcha_scene, self.captcha_ticket_code = '', ''
        self.sms_sent_phone = ''
        self.sms_expires_at = 0
        self.setWindowTitle(ui_text('登录立创商城'))
        self.setWindowModality(Qt.WindowModal)
        self.resize(780, 680)
        self.setMinimumSize(640, 520)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 22, 24, 18)
        outer.setSpacing(18)
        outer.addWidget(title_block(ui_text('立创商城账号登录'), ui_text('登录后同步收藏，快速加入下载列表'), large=True))
        body = QHBoxLayout()
        body.setSpacing(20)
        intro, intro_layout = surface()
        intro_layout.addWidget(title_block('LCSC3D', ui_text('连接你的元件收藏')))
        intro_layout.addSpacing(20)
        intro_layout.addWidget(plain_label(ui_text('扫码、密码或短信\n选择适合你的登录方式。'), 'muted'))
        intro_layout.addStretch()
        intro.setMaximumWidth(200)
        body.addWidget(intro)
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(4, 0, 12, 4)
        layout.setSpacing(14)
        body.addWidget(scroll_page(content), 1)
        outer.addLayout(body, 1)
        self.login_tabs = QTabWidget()
        self.login_tabs.setDocumentMode(True)
        qr_page = QWidget()
        qr_layout = QVBoxLayout(qr_page)
        qr_layout.addWidget(plain_label(ui_text('使用微信扫一扫，在手机上确认登录。'), 'muted'))
        self.qr = plain_label(ui_text('正在获取二维码…'))
        self.qr.setFixedSize(260, 260)
        self.qr.setAlignment(Qt.AlignCenter)
        self.qr.setStyleSheet('background:white;color:#23354a;border:12px solid white;border-radius:8px;')
        qr_layout.addWidget(self.qr, 0, Qt.AlignHCenter)
        self.login_tabs.addTab(qr_page, ui_text('扫码登录'))
        password_page = QWidget()
        password_layout = QVBoxLayout(password_page)
        password_layout.addWidget(plain_label(ui_text('账号')))
        self.account_input = QLineEdit()
        self.account_input.setMaxLength(160)
        self.account_input.setPlaceholderText(ui_text('手机号 / 客户编号 / 邮箱'))
        password_layout.addWidget(self.account_input)
        password_layout.addWidget(plain_label(ui_text('密码')))
        self.password_input = QLineEdit()
        self.password_input.setMaxLength(128)
        self.password_input.setEchoMode(QLineEdit.Password)
        self.password_input.setPlaceholderText(ui_text('请输入登录密码'))
        self.password_input.returnPressed.connect(self.submit_password)
        password_layout.addWidget(self.password_input)
        self.password_visible = QCheckBox(ui_text('显示密码'))
        self.password_visible.toggled.connect(lambda checked: self.password_input.setEchoMode(QLineEdit.Normal if checked else QLineEdit.Password))
        password_layout.addWidget(self.password_visible)
        self.password_submit = QPushButton(ui_text('登录'))
        self.password_submit.setObjectName('primary')
        self.password_submit.clicked.connect(self.submit_password)
        password_layout.addWidget(self.password_submit)
        password_layout.addStretch()
        self.login_tabs.addTab(password_page, ui_text('账号密码'))
        sms_page = QWidget()
        sms_layout = QVBoxLayout(sms_page)
        sms_layout.addWidget(plain_label(ui_text('手机号码')))
        self.phone_input = QLineEdit()
        self.phone_input.setMaxLength(20)
        self.phone_input.setPlaceholderText(ui_text('请输入绑定账号的手机号码'))
        sms_layout.addWidget(self.phone_input)
        sms_layout.addWidget(plain_label(ui_text('短信验证码')))
        code_row = QHBoxLayout()
        self.sms_input = QLineEdit()
        self.sms_input.setMaxLength(8)
        self.sms_input.setPlaceholderText(ui_text('请输入收到的验证码'))
        self.sms_input.returnPressed.connect(self.submit_sms)
        code_row.addWidget(self.sms_input, 1)
        self.sms_send_button = QPushButton(ui_text('获取验证码'))
        self.sms_send_button.clicked.connect(lambda: self.credential_action('sms-send'))
        code_row.addWidget(self.sms_send_button)
        sms_layout.addLayout(code_row)
        self.sms_submit = QPushButton(ui_text('登录'))
        self.sms_submit.setObjectName('primary')
        self.sms_submit.clicked.connect(self.submit_sms)
        sms_layout.addWidget(self.sms_submit)
        sms_layout.addStretch()
        self.login_tabs.addTab(sms_page, ui_text('手机验证码'))
        layout.addWidget(self.login_tabs, 1)
        self.captcha_panel = QWidget()
        captcha_layout = QVBoxLayout(self.captcha_panel)
        captcha_layout.setContentsMargins(0, 0, 0, 0)
        captcha_layout.addWidget(plain_label(ui_text('图片验证码'), 'section'))
        captcha_row = QHBoxLayout()
        self.captcha_image_label = ProductImage()
        self.captcha_image_label.setFixedSize(170, 64)
        self.captcha_image_label.clicked.connect(self.refresh_captcha)
        captcha_row.addWidget(self.captcha_image_label)
        self.captcha_input = QLineEdit()
        self.captcha_input.setMaxLength(12)
        self.captcha_input.setPlaceholderText(ui_text('输入图中字符'))
        self.captcha_input.returnPressed.connect(self.verify_captcha)
        captcha_row.addWidget(self.captcha_input, 1)
        self.captcha_refresh = QPushButton(ui_text('换一张'))
        self.captcha_refresh.clicked.connect(self.refresh_captcha)
        captcha_row.addWidget(self.captcha_refresh)
        captcha_layout.addLayout(captcha_row)
        self.captcha_verify_button = QPushButton(ui_text('验证并继续'))
        self.captcha_verify_button.clicked.connect(self.verify_captcha)
        captcha_layout.addWidget(self.captcha_verify_button)
        self.captcha_panel.hide()
        layout.addWidget(self.captcha_panel)
        self.status = plain_label('')
        self.status.setMinimumHeight(40)
        layout.addWidget(self.status)
        self.remember_box = QCheckBox(ui_text('记住登录，下次启动自动恢复'))
        self.remember_box.setChecked(True)
        layout.addWidget(self.remember_box)
        buttons = QHBoxLayout()
        self.refresh_button = QPushButton(ui_text('刷新二维码'))
        self.refresh_button.clicked.connect(self.begin)
        buttons.addWidget(self.refresh_button)
        cancel = QPushButton(ui_text('取消'))
        cancel.clicked.connect(self.reject)
        buttons.addStretch()
        buttons.addWidget(cancel)
        layout.addLayout(buttons)
        self.poll = QTimer(self)
        self.poll.setSingleShot(True)
        self.poll.timeout.connect(self.poll_status)
        self.countdown = QTimer(self)
        self.countdown.setInterval(1000)
        self.countdown.timeout.connect(self.tick)
        self.sms_cooldown = QTimer(self)
        self.sms_cooldown.setInterval(1000)
        self.sms_cooldown.timeout.connect(self.tick_sms)
        self.login_tabs.currentChanged.connect(self.mode_changed)

    def begin(self):
        self.active = True
        self.jobs.cancel_all()
        self.poll.stop()
        self.countdown.stop()
        self.sms_cooldown.stop()
        self.token = ''
        self.expires_at = 0
        self.client = self.factory()
        self.pending_action = ''
        self.captcha_ticket_code = ''
        self.captcha_panel.hide()
        self.sms_sent_phone = ''
        self.sms_expires_at = 0
        self.phone_input.setReadOnly(False)
        self.password_visible.setChecked(False)
        self.set_credential_busy(False)
        self.refresh_button.setVisible(self.login_tabs.currentIndex() == 0)
        if self.login_tabs.currentIndex() != 0:
            self.status.setText(ui_text('输入账号和密码后登录。') if self.login_tabs.currentIndex() == 1 else ui_text('获取短信验证码后登录。'))
            return
        client = self.client
        self.refresh_button.setEnabled(False)
        self.remember_box.setEnabled(True)
        self.qr.clear()
        self.qr.setText(ui_text('正在获取二维码…'))
        self.status.setText(ui_text('正在连接立创商城…'))
        self.jobs.start('qr', lambda stop, progress: client.start_qr(stop), self.qr_loaded, self.failed)

    def mode_changed(self):
        self.password_input.clear()
        self.sms_input.clear()
        self.captcha_input.clear()
        if self.active:
            self.begin()

    def set_credential_busy(self, busy):
        self.credential_busy = busy
        self.login_tabs.setEnabled(not busy)
        self.remember_box.setEnabled(not busy)
        self.password_submit.setEnabled(not busy)
        self.sms_submit.setEnabled(not busy)
        self.captcha_verify_button.setEnabled(not busy)
        self.captcha_refresh.setEnabled(not busy)
        self.tick_sms()

    def submit_password(self):
        self.credential_action('password')

    def submit_sms(self):
        if not self.sms_sent_phone:
            self.status.setText(ui_text('请先点击「获取验证码」。'))
            return
        self.credential_action('sms-login')

    def credential_action(self, action, captcha_ticket=''):
        if not self.active or self.credential_busy:
            return
        client, remember = self.client, self.remember_box.isChecked()
        username, password = self.account_input.text(), self.password_input.text()
        phone, code = self.phone_input.text(), self.sms_input.text()
        if action == 'password' and (not username.strip() or not password):
            self.status.setText(ui_text('请输入账号和密码。'))
            return
        if action.startswith('sms'):
            try:
                phone = client._phone_number(phone)
            except StoreError as error:
                self.status.setText(str(error))
                return
            if action == 'sms-send' and time.monotonic() < self.sms_expires_at:
                return
            if action == 'sms-login' and phone != self.sms_sent_phone:
                self.status.setText(ui_text('手机号已修改，请重新获取验证码。'))
                return
        self.pending_action = action
        self.set_credential_busy(True)
        self.status.setText(ui_text('正在发送短信验证码…') if action == 'sms-send' else ui_text('正在验证账号并登录商城…'))
        if action == 'password':
            operation = lambda stop, progress: client.login_password(username, password, stop, remember=remember, captcha_ticket=captcha_ticket)
        elif action == 'sms-send':
            operation = lambda stop, progress: client.send_sms(phone, stop, captcha_ticket=captcha_ticket)
        else:
            operation = lambda stop, progress: client.login_sms(phone, code, stop, remember=remember, captcha_ticket=captcha_ticket)
        success = (lambda value: self.sms_sent(phone)) if action == 'sms-send' else (lambda account: self.success(client))
        self.jobs.start('credential', operation, success, self.failed)

    def sms_sent(self, phone):
        self.pending_action = ''
        self.captcha_panel.hide()
        self.sms_sent_phone = phone
        self.sms_expires_at = time.monotonic() + 60
        self.set_credential_busy(False)
        self.phone_input.setReadOnly(True)
        self.sms_cooldown.start()
        self.tick_sms()
        self.status.setText(ui_text('短信已发送，请输入收到的验证码。'))
        self.sms_input.setFocus()

    def tick_sms(self):
        seconds = max(0, int(self.sms_expires_at - time.monotonic()) + 1) if self.sms_expires_at else 0
        self.sms_send_button.setText(ui_message('{0} 秒后重试', seconds) if seconds else ui_text('获取验证码'))
        self.sms_send_button.setEnabled(not self.credential_busy and seconds == 0)
        if not seconds:
            self.phone_input.setReadOnly(False)
            self.sms_cooldown.stop()

    def refresh_captcha(self):
        if not self.active or not self.captcha_scene or self.credential_busy:
            return
        self.captcha_ticket_code = ''
        self.captcha_input.clear()
        self.captcha_image_label.clear()
        self.captcha_image_label.setText(ui_text('正在加载…'))
        client, scene = self.client, self.captcha_scene
        self.jobs.start('captcha', lambda stop, progress: client.captcha_image(scene, stop), self.captcha_loaded, self.failed)

    def captcha_loaded(self, value):
        pixmap = QPixmap()
        if not pixmap.loadFromData(value['image']):
            log_event('ERROR', 'login.qr_image_decode_failed', bytes=len(value['image']))
            self.status.setText(ui_text('验证码图片加载失败，请点击「换一张」。'))
            return
        self.captcha_ticket_code = value['ticket_code']
        self.captcha_image_label.set_image(pixmap)
        self.captcha_input.setFocus()

    def verify_captcha(self):
        if not self.captcha_ticket_code or not self.captcha_input.text().strip() or self.credential_busy:
            self.status.setText(ui_text('请输入图片验证码。'))
            return
        self.set_credential_busy(True)
        client, scene, ticket, code = self.client, self.captcha_scene, self.captcha_ticket_code, self.captcha_input.text()
        self.jobs.start('captcha-check', lambda stop, progress: client.check_captcha(scene, ticket, code, stop),
                        self.captcha_verified, self.failed)

    def captcha_verified(self, ticket):
        self.set_credential_busy(False)
        self.captcha_panel.hide()
        self.credential_action(self.pending_action, ticket)

    def qr_loaded(self, value):
        pixmap = QPixmap()
        if not pixmap.loadFromData(value['image']):
            self.failed(StoreError(ui_text('二维码图片加载失败，请刷新重试。')))
            return
        self.token = value['token']
        self.expires_at = time.monotonic() + value['expires']
        self.qr.setPixmap(pixmap.scaled(242, 242, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        self.refresh_button.setEnabled(True)
        self.status.setText(ui_text('等待扫码，请在手机上确认登录。'))
        self.countdown.start()
        self.poll.start(1500)

    def tick(self):
        if self.active and self.expires_at and time.monotonic() >= self.expires_at:
            self.expired()

    def expired(self):
        log_event('INFO', 'login.qr_expired')
        self.poll.stop()
        self.countdown.stop()
        self.jobs.cancel('poll')
        self.token = ''
        self.qr.clear()
        self.qr.setText(ui_text('二维码已过期\n请点击「刷新二维码」'))
        self.status.setText(ui_text('登录未完成，可以刷新后重新扫码。'))
        self.refresh_button.setEnabled(True)

    def poll_status(self):
        if not self.active or not self.token:
            return
        client, token = self.client, self.token
        self.jobs.start('poll', lambda stop, progress: client.scan_status(token, stop), self.scanned, self.failed)

    def scanned(self, status):
        if status == 'EXPIRED':
            self.expired()
        elif status in ('LOGIN_SUCCESS', 'REGISTER_SUCCESS'):
            self.countdown.stop()
            self.poll.stop()
            self.refresh_button.setEnabled(False)
            self.remember_box.setEnabled(False)
            self.status.setText(ui_text('扫码已确认，正在登录商城…'))
            client, token, remember = self.client, self.token, self.remember_box.isChecked()
            self.jobs.start('login', lambda stop, progress: client.finish_login(token, stop, remember=remember),
                            lambda account: self.success(client), self.failed)
        else:
            self.status.setText(ui_text('已扫码，请在手机上确认登录。') if status == 'SCANNED' else ui_text('等待扫码，请在手机上确认登录。'))
            self.poll.start(2500)

    def success(self, client):
        if not self.active:
            return
        self.token = ''
        self.logged_in.emit(client)
        self.accept()

    def failed(self, error):
        self.poll.stop()
        self.countdown.stop()
        self.set_credential_busy(False)
        self.status.setText(str(error))
        self.refresh_button.setEnabled(True)
        self.remember_box.setEnabled(True)
        if isinstance(error, CaptchaRequired):
            self.captcha_scene = error.scene
            self.captcha_panel.show()
            self.refresh_captcha()

    def done(self, result):
        self.active = False
        self.poll.stop()
        self.countdown.stop()
        self.sms_cooldown.stop()
        self.jobs.cancel_all()
        self.token = ''
        self.qr.clear()
        self.password_input.clear()
        self.account_input.clear()
        self.phone_input.clear()
        self.sms_input.clear()
        self.captcha_input.clear()
        self.captcha_ticket_code = ''
        self.sms_sent_phone = ''
        self.pending_action = ''
        self.client = None
        self.set_credential_busy(False)
        super().done(result)

    def closeEvent(self, event):
        self.reject()
        event.accept()


class FavoritesDialog(QDialog):
    """Persistent in-app page for public search and account favorites."""
    import_requested = Signal(object)
    preview_requested = Signal(str)
    activity_finished = Signal()
    account_changed = Signal()

    def __init__(self, parent=None, *, client_factory=None, vault=None):
        super().__init__(parent)
        self.app_window = parent
        if parent is not None:
            self.setWindowIcon(parent.windowIcon())
        self.setWindowModality(Qt.NonModal)
        self.setWindowFlag(Qt.WindowStaysOnTopHint, False)
        self.factory = client_factory or catalog_client
        self.client = self.factory()
        self.catalog = self.factory()
        self.international = getattr(self.client, 'international', False)
        self.vault = MemoryOnlyVault() if vault is False or self.international else vault if vault is not None else SessionVault()
        self.remember_session = True
        self.jobs = Jobs(self)
        self.jobs.idle.connect(self.activity_finished)
        self.login_dialog = None
        self.closing = False
        self.busy = False
        self.import_enabled = True
        self.searching = False
        self.restoring = False
        self.collecting = False
        self.search_total, self.search_pages = 0, 0
        self.search_page = 0
        self.search_keyword = ''
        self.search_selected = {}
        self.price_choices = {}
        self.favorite_checked = set()
        self.galleries = []
        self.items, self.search_items = {}, []
        self.current_product = None
        self.pending_gallery_part = ''
        self.pages_read = 0
        self.setWindowTitle(ui_text('立创商城 · 搜索与收藏'))
        self.resize(1420, 860)
        self.setMinimumSize(1100, 680)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 16)
        layout.setSpacing(12)
        account_row = QHBoxLayout()
        account_row.addWidget(title_block(ui_text('立创商城'), ui_text('发现元件 · 查看资料 · 同步收藏'), large=True), 1)
        self.account_label = plain_label(ui_text('未登录'), 'muted')
        self.account_label.setObjectName('badge')
        if self._page_host is None:
            account_row.addWidget(self.account_label)
        else:
            self.account_label.setParent(self)
            self.account_label.hide()
        self.login_button = IconButton(ui_text('账号登录'), 'user')
        self.login_button.clicked.connect(lambda: self.clear_session() if self.client.account else self.open_login())
        self.account_changed.connect(self.refresh_account_action)
        if self._page_host is None:
            account_row.addWidget(self.login_button)
        else:
            self.login_button.setParent(self)
            self.login_button.hide()
        layout.addLayout(account_row)
        splitter = self.product_splitter = ColumnSplitter()
        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.tabs.currentChanged.connect(self.tab_changed)
        search_panel = QWidget()
        search_layout = QVBoxLayout(search_panel)
        search_tools = QHBoxLayout()
        self.search_input = QLineEdit()
        self.search_input.setMaxLength(160)
        self.search_input.setPlaceholderText(ui_text('C 编号、型号或关键词，例如 C2040 / STM32F103C8T6'))
        self.search_input.returnPressed.connect(self.start_search)
        search_tools.addWidget(self.search_input, 1)
        self.search_button = IconButton(ui_text('搜索'), 'search', role='primary')
        self.search_button.clicked.connect(self.start_search)
        search_tools.addWidget(self.search_button)
        self.search_stop_button = QPushButton(ui_text('停止'))
        self.search_stop_button.setEnabled(False)
        self.search_stop_button.clicked.connect(self.stop_search)
        search_tools.addWidget(self.search_stop_button)
        search_layout.addLayout(search_tools)
        self.category_combo = QComboBox()
        self.category_combo.setToolTip('Filter LCSC International search results by product category')
        self.category_combo.hide()
        self.category_combo.currentIndexChanged.connect(self.change_category)
        search_layout.addWidget(self.category_combo)
        self.search_hint = plain_label(ui_text('搜索商品无需登录，选中元件查看详情。'), 'muted')
        search_layout.addWidget(self.search_hint)
        self.search_table = self.make_table(commercial=True)
        search_layout.addWidget(self.search_table, 1)
        pagination = QHBoxLayout()
        self.previous_page_button = QPushButton(ui_text('上一页'))
        self.previous_page_button.clicked.connect(lambda: self.load_search_page(self.search_page - 1))
        pagination.addWidget(self.previous_page_button)
        self.page_label = plain_label(ui_text('尚未搜索 · 每页 50 个'), 'muted')
        self.page_label.setAlignment(Qt.AlignCenter)
        pagination.addWidget(self.page_label, 1)
        self.next_page_button = QPushButton(ui_text('下一页'))
        self.next_page_button.clicked.connect(lambda: self.load_search_page(self.search_page + 1))
        pagination.addWidget(self.next_page_button)
        search_layout.addLayout(pagination)
        self.tabs.addTab(search_panel, ui_text('搜索商品'))
        favorite_panel = QWidget()
        favorite_layout = QVBoxLayout(favorite_panel)
        favorite_tools = QHBoxLayout()
        self.read_button = QPushButton(ui_text('获取收藏'))
        self.read_button.setObjectName('primary')
        self.read_button.clicked.connect(self.start_read)
        favorite_tools.addWidget(self.read_button)
        self.stop_button = QPushButton(ui_text('停止'))
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self.stop_read)
        favorite_tools.addWidget(self.stop_button)
        self.filter_input = QLineEdit()
        self.filter_input.setPlaceholderText(ui_text('筛选已获取的收藏'))
        self.filter_input.textChanged.connect(self.filter_favorites)
        favorite_tools.addWidget(self.filter_input, 1)
        favorite_layout.addLayout(favorite_tools)
        self.favorite_hint = plain_label(ui_text('登录后获取商城账号收藏，软件自动读取后续分页。'), 'muted')
        favorite_layout.addWidget(self.favorite_hint)
        self.favorite_table = self.make_table()
        favorite_layout.addWidget(self.favorite_table, 1)
        self.tabs.addTab(favorite_panel, ui_text('账号收藏'))
        splitter.addWidget(self.tabs)
        inspector, detail = surface()
        inspector.setMinimumWidth(280)
        detail.addWidget(plain_label(ui_text('商品详情'), 'section'))
        self.image_label = ProductImage()
        self.image_label.clicked.connect(self.open_gallery)
        self.image_label.setFixedHeight(185)
        self.image_label.setAlignment(Qt.AlignCenter)
        self.image_label.setObjectName('productImage')

        self.gallery_button = QPushButton(ui_text('查看商品原图'))
        self.gallery_button.clicked.connect(self.open_gallery)

        self.detail_scroll = QScrollArea()
        self.detail_scroll.setFrameShape(QFrame.NoFrame)
        self.detail_scroll.setWidgetResizable(True)
        self.detail_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        content = QWidget()
        content.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Minimum)
        self.detail_body = QVBoxLayout(content)
        self.detail_body.setContentsMargins(0, 0, 0, 0)
        self.detail_body.setSpacing(14)
        self.detail_body.addWidget(self.image_label)
        self.detail_body.addWidget(self.gallery_button)
        self.product_title = DetailLabel(ui_text('选择一个元件'), 'section')
        self.product_meta = DetailLabel('', 'muted')
        self.product_description = DetailLabel('')
        for label in (self.product_title, self.product_meta, self.product_description):
            self.detail_body.addWidget(label)
        self.parameters = ParameterTable()
        self.detail_body.addWidget(self.parameters)
        self.detail_body.addStretch()
        self.detail_scroll.setWidget(content)
        detail.addWidget(self.detail_scroll, 1)
        links = QHBoxLayout()
        self.datasheet_button = QPushButton(ui_text('数据手册'))
        self.datasheet_button.clicked.connect(lambda: self.open_link('datasheet'))
        self.store_button = QPushButton(ui_text('商城商品页'))
        self.store_button.clicked.connect(lambda: self.open_link('store_url'))
        links.addWidget(self.datasheet_button)
        links.addWidget(self.store_button)
        detail.addLayout(links)
        self.collect_button = QPushButton(ui_text('收藏到账号'))
        self.collect_button.clicked.connect(self.collect_selected)
        favorite_actions = QHBoxLayout()
        favorite_actions.addWidget(self.collect_button)
        self.uncollect_button = QPushButton(ui_text('取消收藏'))
        self.uncollect_button.clicked.connect(self.uncollect_selected)
        self.uncollect_button.setProperty('variant', 'danger')
        favorite_actions.addWidget(self.uncollect_button)
        detail.addLayout(favorite_actions)
        self.detail_hint = plain_label(ui_text('资料来自立创商城。'), 'muted')
        detail.addWidget(self.detail_hint)
        splitter.addWidget(inspector)
        splitter.setSizes([1000, 360])
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 1)
        layout.addWidget(splitter, 1)
        actions = QHBoxLayout()
        self.select_button = QPushButton(ui_text('全选 / 取消'))
        self.select_button.clicked.connect(self.toggle_selection)
        actions.addWidget(self.select_button)
        self.count = plain_label(ui_text('已勾选 0 / 0'), 'muted')
        actions.addWidget(self.count, 1)
        self.preview_button = QPushButton(ui_text('预览选中元件'))
        self.preview_button.clicked.connect(self.preview_selected)
        actions.addWidget(self.preview_button)
        self.import_button = IconButton(ui_text('加入下载列表'), 'add', role='primary')
        self.import_button.clicked.connect(self.import_selected)
        actions.addWidget(self.import_button)
        layout.addLayout(actions)
        self.status = plain_label(ui_text('输入关键词搜索，或登录后获取账号收藏。'), 'muted')
        layout.addWidget(self.status)
        self.clear_detail()
        self.update_count()
        theme_manager().changed.connect(self.refresh_appearance)
        self.refresh_appearance()
        if self.international:
            self.setWindowTitle('LCSC International · Search & Products')
            self.account_label.setText('International account: LCSC.com')
            self.favorite_hint.setText('International sign-in and saved favorites are managed on LCSC.com. In-app search is available without signing in.')
            self.read_button.setText('Manage favorites on LCSC.com')
            self.filter_input.setEnabled(False)
            self.status.setText('Search LCSC International. Prices are shown in USD; choose a category for broad keywords.')
        if self.vault.exists():
            QTimer.singleShot(0, self.restore_session)

    @property
    def table(self):
        return self.favorite_table if self.tabs.currentIndex() == 1 else self.search_table

    def refresh_account_action(self):
        self.login_button.setEnabled(not self.restoring)
        self.login_button.setText(ui_text('退出登录') if self.client.account else ui_text('账号登录'))

    def refresh_appearance(self):
        scale = theme_manager().tokens.get('font_size', 13) / 13
        for table in (self.search_table, self.favorite_table):
            table.ensurePolished()
            table.verticalHeader().setDefaultSectionSize(max(round(36 * scale), table.fontMetrics().height() + 14))
            for column, width in ((0, 46), (1, 95), (3, 100), (4, 105), (5, 175), (6, 85)):
                if column < table.columnCount():
                    title = table.horizontalHeaderItem(column).text()
                    table.setColumnWidth(column, max(round(width * scale), table.fontMetrics().horizontalAdvance(title) + 28))

    def make_table(self, commercial=False):
        labels = [ui_text('选择'), ui_text('C 编号'), ui_text('型号'), ui_text('厂商'), ui_text('封装')]
        if commercial:
            labels += [ui_text('单价 / 梯度'), ui_text('库存')]
        table = QTableWidget(0, len(labels))
        table.setHorizontalHeaderLabels(labels)
        table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        table.setSelectionBehavior(QAbstractItemView.SelectRows)
        table.setSelectionMode(QAbstractItemView.SingleSelection)
        table.setWordWrap(False)
        table.setShowGrid(False)
        table.setAlternatingRowColors(True)
        table.verticalHeader().hide()
        table.verticalHeader().setDefaultSectionSize(36)
        table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Fixed)
        table.setColumnWidth(0, 46)
        table.setColumnWidth(1, 95)
        table.setColumnWidth(3, 100)
        table.setColumnWidth(4, 115)
        table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        if commercial:
            table.setColumnWidth(5, 210)
            table.setColumnWidth(6, 85)
        if self.international:
            for column in (0, 1, 3, 4) + ((5, 6) if commercial else ()):
                title = table.horizontalHeaderItem(column).text()
                table.setColumnWidth(column, max(table.columnWidth(column), table.fontMetrics().horizontalAdvance(title) + 28))
        table.currentCellChanged.connect(self.selection_changed)
        table.itemChanged.connect(lambda item, source=table: self.checkbox_changed(source, item))
        return table

    def set_mode(self, mode):
        self.tabs.setCurrentIndex(1 if mode == 'favorites' else 0)

    def tab_changed(self):
        if not hasattr(self, 'parameters'):
            return
        self.update_count()
        self.selection_changed()
        if self.tabs.currentIndex() == 1 and self.isVisible() and self.client.account and not self.items and not self.busy:
            self.start_read()

    def showEvent(self, event):
        super().showEvent(event)
        if self.tabs.currentIndex() == 1 and self.client.account and not self.items and not self.busy:
            self.start_read()

    def start_search(self):
        keyword = self.search_input.text().strip()
        if not keyword:
            self.search_hint.setText(ui_text('请输入 C 编号、型号或关键词。'))
            return
        self.jobs.cancel('search')
        self.searching = False
        self.search_keyword = keyword
        self.category_combo.blockSignals(True)
        self.category_combo.clear()
        self.category_combo.hide()
        self.category_combo.blockSignals(False)
        self.search_selected.clear()
        self.price_choices.clear()
        self.search_page = 0
        self.search_total, self.search_pages = 0, 0
        self.search_items = []
        self.search_table.blockSignals(True)
        self.search_table.setRowCount(0)
        self.search_table.blockSignals(False)
        if self.tabs.currentIndex() == 0:
            self.clear_detail()
        self.load_search_page(1)

    def change_category(self):
        if not self.international or not self.search_keyword:
            return
        self.jobs.cancel('search')
        self.searching = False
        self.search_pages = 0
        self.load_search_page(1)

    def load_search_page(self, page):
        if not self.search_keyword or page < 1 or self.searching:
            return
        if self.search_pages and page > self.search_pages:
            return
        self.searching = True
        self.search_stop_button.setEnabled(True)
        self.search_hint.setText(ui_message('正在搜索 {0} · 第 {1} 页…', self.search_keyword, page))
        self.update_count()
        self.update_pagination()
        client, keyword = self.catalog, self.search_keyword
        extra = {'catalog_id': self.category_combo.currentData()} if self.international else {}
        self.jobs.start('search', lambda stop, progress: client.search_results_page(keyword, page, stop, **extra),
                        self.search_loaded, self.search_failed)

    def search_loaded(self, result):
        self.searching = False
        self.search_stop_button.setEnabled(False)
        self.search_page, self.search_total, self.search_pages = result['page'], result['total'], result['pages']
        self.search_items = result['items']
        if self.international:
            self.category_combo.blockSignals(True)
            self.category_combo.clear()
            for category in result.get('categories', []):
                self.category_combo.addItem(f"{category['name']} ({category['count']:,})", category['id'])
            self.category_combo.setCurrentIndex(self.category_combo.findData(result.get('catalog_id')))
            self.category_combo.setVisible(self.category_combo.count() > 0)
            self.category_combo.blockSignals(False)
        self.search_table.blockSignals(True)
        self.search_table.setRowCount(0)
        for product in self.search_items:
            self.append_row(self.search_table, product)
        self.search_table.blockSignals(False)
        self.search_hint.setText(ui_message('共 {0:,} 个搜索结果 · 本页 {1} 个元件。', self.search_total, len(self.search_items))
                                 if self.search_items else ui_text('没有找到匹配元件，请尝试其他型号或关键词。'))
        self.update_pagination()
        self.update_count()
        if self.tabs.currentIndex() == 0:
            self.clear_detail()
            if self.search_items:
                self.search_table.selectRow(0)

    def update_pagination(self):
        self.previous_page_button.setEnabled(not self.searching and self.search_page > 1)
        self.next_page_button.setEnabled(not self.searching and 0 < self.search_page < self.search_pages)
        self.page_label.setText(ui_message('第 {0} / {1} 页 · 每页 {2} 个', self.search_page, max(1, self.search_pages), SEARCH_PAGE_SIZE)
                                if self.search_page else ui_text('尚未搜索 · 每页 50 个'))

    def search_failed(self, error):
        self.searching = False
        self.search_stop_button.setEnabled(False)
        self.search_hint.setText(str(error) + ui_message(' 已保留 {0:,} 个结果。', len(self.search_items)))
        self.update_pagination()
        self.update_count()

    def stop_search(self, *args):
        self.jobs.cancel('search')
        self.searching = False
        self.search_stop_button.setEnabled(False)
        self.search_hint.setText(ui_message('已停止，本页 {0:,} 个元件及跨页勾选保留。', len(self.search_items)))
        self.update_pagination()
        self.update_count()

    def append_row(self, table, product):
        row = table.rowCount()
        was_blocked = table.blockSignals(True)
        table.insertRow(row)
        check = QTableWidgetItem()
        check.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable | Qt.ItemIsUserCheckable)
        checked = product['part'] in (self.search_selected if table is self.search_table else self.favorite_checked)
        check.setCheckState(Qt.Checked if checked else Qt.Unchecked)
        table.setItem(row, 0, check)
        for column, key in enumerate(('part', 'title', 'manufacturer', 'package'), 1):
            cell = QTableWidgetItem(product.get(key, ''))
            cell.setToolTip(product.get(key, ''))
            table.setItem(row, column, cell)
        table.item(row, 1).setData(Qt.UserRole, product)
        if table is self.search_table:
            self.append_commerce(row, product)
        table.blockSignals(was_blocked)

    def append_commerce(self, row, product):
        stock = product.get('stock')
        cell = QTableWidgetItem(ui_message('{0:,}', stock) if isinstance(stock, int) else '—')
        cell.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
        cell.setToolTip(ui_text('商城现货库存，单位：') + product.get('unit', ui_text('个')))
        self.search_table.setItem(row, 6, cell)
        tiers = product.get('price_tiers') or []
        if not tiers:
            cell = QTableWidgetItem('—')
            cell.setToolTip(ui_text('商城暂未提供可显示的价格。'))
            self.search_table.setItem(row, 5, cell)
            return
        combo = QComboBox()
        combo.setMaxVisibleItems(12)
        unit = product.get('unit', ui_text('个'))
        for tier in tiers:
            combo.addItem(ui_message('{0}+  {1}{2}/{3}', tier['quantity'], product.get('currency_symbol', '¥'), tier['price'], unit), tier['quantity'])
        index = combo.findData(self.price_choices.get(product['part']))
        combo.setCurrentIndex(max(0, index))
        combo.setToolTip(ui_text('选择该商品的实际价格梯度，价格按') + unit + ui_text('计。'))
        self.search_table.setCellWidget(row, 5, combo)
        combo.currentIndexChanged.connect(lambda _: self.price_choices.__setitem__(product['part'], combo.currentData()))

    def checkbox_changed(self, table, item):
        if item.column() == 0:
            cell = table.item(item.row(), 1)
            if cell is not None:
                product = cell.data(Qt.UserRole)
                if table is self.search_table:
                    if item.checkState() == Qt.Checked:
                        self.search_selected[product['part']] = product
                    else:
                        self.search_selected.pop(product['part'], None)
                elif item.checkState() == Qt.Checked:
                    self.favorite_checked.add(product['part'])
                else:
                    self.favorite_checked.discard(product['part'])
        self.update_count()

    def open_login(self):
        if self.international:
            QDesktopServices.openUrl(QUrl(HOME))
            return
        if self.client.account or self.restoring:
            return
        if self.vault.exists():
            self.restore_session()
            return
        if self.login_dialog is None:
            self.login_dialog = LoginDialog(self, self.factory)
            self.login_dialog.logged_in.connect(self.logged_in)
            self.login_dialog.activity_finished.connect(self.activity_finished)
        self.login_dialog.show()
        self.login_dialog.raise_()
        self.login_dialog.activateWindow()
        self.login_dialog.begin()

    def logged_in(self, client):
        if self.closing:
            return
        self.client = client
        self.remember_session = bool(self.login_dialog and self.login_dialog.remember_box.isChecked())
        self.account_label.setText(ui_text('已登录 · ') + client.account['name'])
        self.account_changed.emit()
        if self.remember_session:
            self.save_session()
        else:
            self.vault.delete()
        if self.isVisible() and self.tabs.currentIndex() == 1:
            self.start_read()

    def save_session(self):
        if not self.remember_session or not self.client.account:
            return
        client, ticket = self.client, self.vault.generation
        self.jobs.start('save', lambda stop, progress: self.vault.save(client, ticket, stop),
                        lambda _: None, lambda error: self.status.setText(str(error)))

    def restore_session(self):
        if self.restoring or self.closing:
            return
        self.restoring = True
        self.account_label.setText(ui_text('正在恢复登录…'))
        self.account_changed.emit()
        client = self.factory()
        def restore(stop, progress):
            if not self.vault.load_into(client):
                raise SessionExpired(ui_text('已保存的登录状态不可用，请重新登录。'))
            account = client.restore_account(stop)
            if stop.is_set():
                raise Cancelled()
            client.account = account
            return client
        self.jobs.start('restore', restore, self.session_restored, self.restore_failed)

    def session_restored(self, client):
        self.client = client
        self.restoring = False
        self.remember_session = True
        self.account_label.setText(ui_text('已登录 · ') + client.account['name'])
        self.account_changed.emit()
        self.status.setText(ui_text('已恢复商城登录。'))
        self.save_session()
        if self.isVisible() and self.tabs.currentIndex() == 1 and not self.busy:
            self.start_read()

    def restore_failed(self, error):
        self.restoring = False
        self.account_label.setText(ui_text('未登录'))
        self.account_changed.emit()
        if isinstance(error, SessionExpired):
            self.vault.delete()
        self.status.setText(str(error))

    def clear_session(self):
        self.stop_read()
        self.jobs.cancel('restore')
        self.jobs.cancel('save')
        self.jobs.cancel('collect')
        self.jobs.cancel('uncollect')
        self.restoring = False
        self.collecting = False
        self.vault.delete()
        if self.login_dialog:
            self.login_dialog.reject()
        old = self.client
        self.client = self.factory()
        self.items.clear()
        self.favorite_checked.clear()
        self.favorite_table.setRowCount(0)
        self.pages_read = 0
        self.account_label.setText(ui_text('未登录'))
        self.account_changed.emit()
        self.favorite_hint.setText(ui_text('登录后获取商城账号收藏，软件自动读取后续分页。'))
        if self.tabs.currentIndex() == 1:
            self.clear_detail()
        self.update_count()
        self.update_collect_button()
        self.status.setText(ui_text('已退出登录，账号收藏已清除。'))
        self.jobs.start('logout', lambda stop, progress: old.clear(), lambda _: None, lambda _: None)

    def start_read(self, *args):
        if self.international:
            self.open_login()
            return
        if not self.client.account:
            self.open_login()
            return
        self.jobs.cancel('favorites')
        self.items.clear()
        self.favorite_table.setRowCount(0)
        if self.tabs.currentIndex() == 1:
            self.clear_detail()
        self.pages_read = 0
        self.busy = True
        self.read_button.setEnabled(False)
        self.stop_button.setEnabled(True)
        self.favorite_hint.setText(ui_text('正在获取商城账号收藏…'))
        self.status.setText(ui_text('正在后台读取收藏，无需手动翻页。'))
        self.update_count()
        client = self.client
        self.jobs.start('favorites', lambda stop, progress: client.favorites(stop, progress),
                        self.favorites_loaded, self.favorites_failed, self.page_loaded)

    def page_loaded(self, page, items):
        self.pages_read = page
        valid = {item['part'] for item in normalize_items(items)}
        for product in items:
            if not isinstance(product, dict) or product.get('part') not in valid:
                continue
            if product['part'] not in self.items:
                self.items[product['part']] = product
                self.append_row(self.favorite_table, product)
        self.favorite_hint.setText(ui_message('已获取 {0} 个元件 · 第 {1} 页', len(self.items), page))
        self.filter_favorites()
        self.update_collect_button()

    def favorites_loaded(self, items):
        self.busy = False
        self.read_button.setEnabled(True)
        self.stop_button.setEnabled(False)
        self.favorite_hint.setText(ui_message('账号收藏：{0} 个元件，共 {1} 页。', len(self.items), self.pages_read)
                                   if self.items else ui_text('该账号还没有收藏元件。'))
        self.status.setText(ui_text('收藏获取完成，勾选元件后加入下载列表。'))
        self.save_session()
        self.update_count()
        self.update_collect_button()
        if self.items and self.tabs.currentIndex() == 1:
            self.favorite_table.selectRow(0)

    def favorites_failed(self, error):
        self.stop_read()
        if isinstance(error, SessionExpired):
            self.clear_session()
        self.status.setText(str(error) + (ui_message(' 已保留 {0} 个元件。', len(self.items)) if self.items else ''))

    def stop_read(self, *args):
        self.jobs.cancel('favorites')
        was_busy = self.busy
        self.busy = False
        self.read_button.setEnabled(True)
        self.stop_button.setEnabled(False)
        if was_busy:
            self.status.setText(ui_message('已停止，保留已获取的 {0} 个元件。', len(self.items)))
        self.update_count()

    def filter_favorites(self):
        keyword = self.filter_input.text().strip().casefold()
        for row in range(self.favorite_table.rowCount()):
            value = ' '.join(self.favorite_table.item(row, c).text() for c in (1, 2, 3, 4)).casefold()
            self.favorite_table.setRowHidden(row, bool(keyword and keyword not in value))
        if self.tabs.currentIndex() == 1 and self.favorite_table.isRowHidden(self.favorite_table.currentRow()):
            self.clear_detail()
        self.update_count()

    def selected_items(self):
        if self.tabs.currentIndex() == 0:
            return list(self.search_selected.values())
        return [self.table.item(row, 1).data(Qt.UserRole) for row in range(self.table.rowCount())
                if not self.table.isRowHidden(row) and self.table.item(row, 0).checkState() == Qt.Checked]

    def update_count(self, *args):
        if not hasattr(self, 'import_button'):
            return
        count = len(self.selected_items())
        visible = sum(not self.table.isRowHidden(row) for row in range(self.table.rowCount()))
        if self.tabs.currentIndex() == 0:
            page_checked = sum(self.search_table.item(row, 0).checkState() == Qt.Checked for row in range(self.search_table.rowCount()))
            self.count.setText(ui_message('本页已勾选 {0} / {1} · 跨页共 {2} 个', page_checked, visible, count))
        else:
            self.count.setText(ui_message('已勾选 {0} / {1}', count, visible))
        loading = self.busy if self.tabs.currentIndex() == 1 else self.searching
        self.import_button.setEnabled(self.import_enabled and bool(count) and not loading)
        self.import_button.setToolTip(ui_text('请等待当前下载完成。') if not self.import_enabled else ui_text('按 C 编号去重加入下载列表'))
        self.preview_button.setEnabled(bool(self.selected_product()))
        self.update_pagination()

    def set_import_enabled(self, enabled):
        self.import_enabled = enabled
        self.update_count()

    def toggle_selection(self):
        rows = [row for row in range(self.table.rowCount()) if not self.table.isRowHidden(row)]
        checked = not all(self.table.item(row, 0).checkState() == Qt.Checked for row in rows)
        self.table.blockSignals(True)
        for row in rows:
            self.table.item(row, 0).setCheckState(Qt.Checked if checked else Qt.Unchecked)
        self.table.blockSignals(False)
        if self.tabs.currentIndex() == 0:
            for row in rows:
                product = self.table.item(row, 1).data(Qt.UserRole)
                if checked:
                    self.search_selected[product['part']] = product
                else:
                    self.search_selected.pop(product['part'], None)
        else:
            for row in rows:
                part = self.table.item(row, 1).text()
                (self.favorite_checked.add if checked else self.favorite_checked.discard)(part)
        self.update_count()

    def import_selected(self):
        if self.import_button.isEnabled():
            self.import_requested.emit(normalize_items(self.selected_items()))

    def show_import_result(self, success, message):
        previous = getattr(self, 'import_notice', None)
        if previous is not None and isValid(previous):
            previous.close()
            previous.deleteLater()
        self.import_notice = notify(self, ui_text('加入下载列表成功') if success else ui_text('加入下载列表失败'),
                                    message, severity='success' if success else 'warning')

    def selected_product(self):
        row = self.table.currentRow()
        return self.table.item(row, 1).data(Qt.UserRole) if row >= 0 and not self.table.isRowHidden(row) else None

    def clear_detail(self):
        self.jobs.cancel('detail')
        self.jobs.cancel('image')
        self.current_product = None
        self.pending_gallery_part = ''
        self.product_title.setText(ui_text('选择一个元件'))
        self.product_meta.clear()
        self.product_description.clear()
        self.parameters.setRowCount(0)
        self.parameters.reflow.start(0)
        self.detail_scroll.verticalScrollBar().setValue(0)
        self.image_label.clear()
        self.image_label.setText(ui_text('选择元件查看图片'))
        self.datasheet_button.setEnabled(False)
        self.store_button.setEnabled(False)
        self.gallery_button.setEnabled(False)
        self.gallery_button.setText(ui_text('查看商品原图'))
        self.image_label.setCursor(Qt.ArrowCursor)
        self.image_label.setToolTip('')
        self.detail_hint.setText(ui_text('资料来自立创商城。'))
        self.update_collect_button()

    def selection_changed(self, *args):
        if not hasattr(self, 'parameters'):
            return
        self.clear_detail()
        product = self.selected_product()
        self.update_count()
        if not product:
            return
        self.show_detail(product)
        if not product.get('details_complete'):
            self.request_detail(product)

    def request_detail(self, product):
        client = self.factory()
        self.detail_hint.setText(ui_text('正在查询完整商品资料…'))
        self.jobs.start('detail', lambda stop, progress: client.product(product, stop),
                        self.detail_loaded, self.detail_failed)

    def detail_failed(self, error):
        self.pending_gallery_part = ''
        self.detail_hint.setText(str(error))

    def detail_loaded(self, product):
        if not product:
            self.pending_gallery_part = ''
            self.detail_hint.setText(ui_text('商城及元件目录暂无此商品的详细资料。'))
            return
        selected = self.selected_product()
        if not selected or product['part'] != selected['part']:
            return
        selected.update(product)
        if selected['part'] in self.search_selected:
            self.search_selected[selected['part']] = selected
        row = self.table.currentRow()
        self.table.blockSignals(True)
        self.table.item(row, 1).setData(Qt.UserRole, selected)
        for column, key in ((2, 'title'), (3, 'manufacturer'), (4, 'package')):
            self.table.item(row, column).setText(product.get(key, ''))
            self.table.item(row, column).setToolTip(product.get(key, ''))
        self.table.blockSignals(False)
        self.show_detail(selected)
        if self.pending_gallery_part == selected['part']:
            self.pending_gallery_part = ''
            self.open_gallery()

    def show_detail(self, product):
        self.jobs.cancel('image')
        self.current_product = product
        self.product_title.setText(product.get('title') or product['part'])
        meta = [product['part'], product.get('manufacturer'), product.get('package'), product.get('category')]
        resources = product.get('resources', {})
        available = [label for key, label in (('Symbol', ui_text('符号')), ('Footprint', ui_text('封装')), ('3D Model', ui_text('3D 模型'))) if resources.get(key)]
        self.product_meta.setText(' · '.join(v for v in meta if v) + (ui_text('\n可用资源：') + ' / '.join(available) if available else ''))
        self.product_description.setText(product.get('description', ''))
        params = product.get('parameters', [])
        self.parameters.setRowCount(len(params))
        for row, (key, value) in enumerate(params):
            self.parameters.setItem(row, 0, QTableWidgetItem(key))
            self.parameters.setItem(row, 1, QTableWidgetItem(value))
            self.parameters.item(row, 0).setToolTip(key)
            self.parameters.item(row, 1).setToolTip(value)
        self.parameters.reflow.start(0)
        self.datasheet_button.setEnabled(bool(product.get('datasheet')))
        self.store_button.setEnabled(bool(product.get('store_url')))
        images = product.get('images') or ([product['image']] if product.get('image') else [])
        self.gallery_button.setEnabled(bool(images))
        self.gallery_button.setText(ui_message('查看全部原图（{0} 张）', len(images)) if images else ui_text('暂无商品原图'))
        self.image_label.setCursor(Qt.PointingHandCursor if images else Qt.ArrowCursor)
        self.image_label.setToolTip(ui_message('点击查看 {0} 张商品原图，支持放大和平移', len(images)) if images else '')
        self.detail_hint.setText(product.get('detail_warning') or ui_text('资料来自') + product.get('detail_source', ui_text('立创商城')) + '。')
        self.update_collect_button()
        self.image_label.clear()
        self.image_label.setText(ui_text('暂无商品图片'))
        if product.get('image'):
            url, client = product['image'], self.factory()
            self.image_label.setText(ui_text('正在加载图片…'))
            self.jobs.start('image', lambda stop, progress: client.image(url, stop),
                            self.image_loaded, lambda _: self.image_label.setText(ui_text('图片加载失败')))

    def image_loaded(self, image):
        pixmap = QPixmap()
        if image and pixmap.loadFromData(image):
            self.image_label.set_image(pixmap)
        else:
            log_event('WARNING', 'image.product_decode_failed', bytes=len(image or b''))
            self.image_label.setText(ui_text('暂无商品图片'))

    def open_link(self, key):
        if self.current_product and self.current_product.get(key):
            opened = QDesktopServices.openUrl(QUrl(self.current_product[key]))
            log_event('INFO' if opened else 'WARNING', 'navigation.product_link', opened=opened,
                      kind=key if key in ('datasheet','store_url') else 'product', part=safe_part(self.current_product.get('part')))

    def preview_selected(self):
        product = self.selected_product()
        if product:
            self.preview_requested.emit(product['part'])
            self.status.setText(ui_text('已在主窗口预览 ') + product['part'] + ui_text('，返回商城后可继续选择。'))

    def update_collect_button(self):
        if not hasattr(self, 'collect_button'):
            return
        product = self.current_product
        if self.international:
            self.collect_button.setText('Manage favorite on LCSC.com')
            self.collect_button.setEnabled(bool(product))
            self.collect_button.setToolTip('Open the international product page to manage your account favorite')
            self.uncollect_button.hide()
            return
        known = bool(self.client.account and product and product['part'] in self.items)
        self.collect_button.setText(ui_text('正在收藏…') if self.collecting else ui_text('已在账号收藏') if known else ui_text('收藏到账号'))
        self.collect_button.setEnabled(bool(product and product.get('product_id')) and not self.collecting and not known)
        self.collect_button.setToolTip(ui_text('使用商城账号保存此商品') if self.client.account else ui_text('登录后可加入商城账号收藏'))
        self.uncollect_button.setVisible(self.tabs.currentIndex() == 1 or known)
        self.uncollect_button.setEnabled(known and bool(product.get('product_id')) and not self.collecting and not self.busy)
        self.uncollect_button.setToolTip(ui_text('从商城账号收藏中移除此商品'))

    def collect_selected(self):
        if self.international:
            self.open_link('store_url')
            return
        product = self.current_product
        if not product or self.collecting:
            return
        if not self.client.account:
            self.status.setText(ui_text('请先登录，登录后点击「收藏到账号」保存此商品。'))
            self.open_login()
            return
        self.collecting = True
        self.update_collect_button()
        client, product = self.client, dict(product)
        self.status.setText(ui_text('正在加入账号收藏：') + product['part'])
        self.jobs.start('collect', lambda stop, progress: client.add_favorite(product, stop),
                        self.favorite_added, self.favorite_add_failed)

    def favorite_added(self, result):
        self.collecting = False
        product = result['product']
        if product['part'] not in self.items:
            self.items[product['part']] = product
            self.append_row(self.favorite_table, product)
        self.filter_favorites()
        self.favorite_hint.setText(ui_message('账号收藏已更新 · 已获取 {0} 个元件，可点击「获取收藏」刷新。', len(self.items)))
        self.status.setText(product['part'] + (ui_text(' 已加入商城账号收藏。') if result['added'] else ui_text(' 已在商城账号收藏中。')))
        self.update_collect_button()
        self.save_session()

    def favorite_add_failed(self, error):
        self.collecting = False
        if isinstance(error, SessionExpired):
            self.clear_session()
        self.status.setText(str(error))
        self.update_collect_button()

    def uncollect_selected(self):
        product = self.current_product
        if not product or not self.uncollect_button.isEnabled() or self.collecting:
            return
        self.collecting = True
        self.update_collect_button()
        client, product = self.client, dict(product)
        self.status.setText(ui_text('正在取消账号收藏：') + product['part'])
        self.jobs.start('uncollect', lambda stop, progress: client.remove_favorite(product, stop),
                        self.favorite_removed, self.favorite_add_failed)

    def favorite_removed(self, result):
        self.collecting = False
        product = result['product']
        self.items.pop(product['part'], None)
        self.favorite_checked.discard(product['part'])
        for row in range(self.favorite_table.rowCount()):
            if self.favorite_table.item(row, 1).text() == product['part']:
                self.favorite_table.removeRow(row)
                break
        self.favorite_hint.setText(ui_message('账号收藏已更新 · 已获取 {0} 个元件。', len(self.items)))
        self.status.setText(product['part'] + (ui_text(' 已取消账号收藏。') if result['removed'] else ui_text(' 已不在账号收藏中。')))
        self.update_count()
        self.update_collect_button()
        self.save_session()

    def open_gallery(self):
        product = self.current_product
        if not product or not (product.get('images') or product.get('image')):
            return
        if not product.get('details_complete'):
            self.pending_gallery_part = product['part']
            if not any(w.channel == 'detail' and self.jobs.current(w) for w in self.jobs.workers):
                self.request_detail(product)
            self.detail_hint.setText(ui_text('正在读取完整商品图库，加载完成后打开原图…'))
            return
        existing = next((g for g in self.galleries if g.part == product['part'] and not g._page_finished), None)
        if existing:
            existing.raise_()
            existing.activateWindow()
            return
        gallery = ImageGallery(self, product, self.factory(), Jobs)
        self.galleries.append(gallery)
        gallery.activity_finished.connect(self.gallery_finished)
        gallery.closed.connect(lambda: QTimer.singleShot(0, self.gallery_finished))
        gallery.show()

    def gallery_finished(self):
        if not isValid(self):
            return
        for gallery in self.galleries[:]:
            if not isValid(gallery):
                self.galleries.remove(gallery)
                continue
            if gallery._page_finished and not gallery.jobs.workers:
                self.galleries.remove(gallery)
                gallery.deleteLater()
        self.activity_finished.emit()

    def has_jobs(self):
        return bool(self.jobs.workers or self.login_dialog and self.login_dialog.jobs.workers
                    or any(g.jobs.workers for g in self.galleries))

    def shutdown(self):
        if self.closing:
            return
        self.closing = True
        self.jobs.cancel_all(except_channels=('save',))
        if not any(w.channel == 'save' and self.jobs.current(w) for w in self.jobs.workers):
            self.save_session()
        if self.login_dialog:
            self.login_dialog.reject()
        for gallery in self.galleries[:]:
            gallery.close()

    def closeEvent(self, event):
        self.stop_read()
        if self.searching:
            self.stop_search()
        self.jobs.cancel_all(except_channels=('save',))
        self.pending_gallery_part = ''
        self.searching = False
        self.search_stop_button.setEnabled(False)
        self.collecting = False
        self.update_collect_button()
        if self.login_dialog:
            self.login_dialog.reject()
        for gallery in self.galleries[:]:
            gallery.close()
        event.accept()
