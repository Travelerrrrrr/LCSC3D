"""Asynchronous check, download progress and cancel/retry controls."""
from i18n import text as ui_text, message as ui_message
import sys
import threading
from pathlib import Path

from PySide6.QtCore import Qt, QThread, Signal, QObject, QTimer
from PySide6.QtGui import QDesktopServices
from PySide6.QtCore import QUrl
from PySide6.QtWidgets import (QHBoxLayout, QProgressBar, QVBoxLayout)
from localized_widgets import (QDialog, QLabel, QPlainTextEdit, QPushButton)

from errors import Cancelled
from ui_components import title_block, IconButton
from updater import RELEASES_URL, UpdateClient, discard_update
from app_logging import log_event, new_context, contextual, record_error, log_context
from store_diagnostics import record_request_error


class StartupUpdateCheck(QObject):
    """Check metadata without owning a window or delaying application shutdown."""
    completed = Signal(object)

    def __init__(self, version, parent, *, source=None):
        super().__init__(parent)
        self.cancelled = threading.Event()
        self.state = {'finished': False, 'release': None, 'outcome': 'failed'}
        self.log_context = new_context(feature='update', trigger='startup')
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.poll)
        # The daemon only holds ordinary Python data; it never touches a Qt
        # object after cancellation or while the application is shutting down.
        self.thread = threading.Thread(target=type(self).run,
            args=(version, source, self.cancelled, self.state, self.log_context),
            name='startup-update', daemon=True)

    def start(self):
        self.thread.start()
        self.timer.start(50)

    @staticmethod
    def run(version, source, cancelled, state, context):
        with log_context(context):
            try:
                log_event('INFO', 'update.startup_check_started')
                client = UpdateClient(cancelled) if source is None else UpdateClient(cancelled, source=source)
                release = client.check(version)
                state.update(release=release, outcome='available' if release is not None else 'latest')
            except Cancelled:
                state.update(release=None, outcome='cancelled')
            except Exception as exc:
                record_error(exc, 'update.startup_check_failed')
                state.update(release=None, outcome='failed')
            finally:
                if cancelled.is_set():
                    state.update(release=None, outcome='cancelled')
                log_event('INFO', 'update.startup_check_result', outcome=state['outcome'])
                state['finished'] = True

    def poll(self):
        if self.state['finished']:
            self.timer.stop()
            if not self.cancelled.is_set():
                with log_context(self.log_context):
                    self.completed.emit(dict(self.state))

    def cancel(self):
        self.cancelled.set()
        self.timer.stop()


class UpdateWorker(QThread):
    checked = Signal(object)
    prepared = Signal(object)
    progress = Signal(int, int)
    failed = Signal(str)
    cancelled_download = Signal()

    def __init__(self, version, release=None, parent=None, *, source=None):
        super().__init__(parent)
        self.version, self.release = version, release
        self.source = source
        self.cancelled = threading.Event()
        self.log_context = new_context(feature='update')

    @contextual
    def run(self):
        try:
            operation = ui_text('检查更新') if self.release is None else ui_text('下载更新')
            log_event('INFO', 'update.job_started', operation=operation)
            client = UpdateClient(self.cancelled) if self.source is None else UpdateClient(self.cancelled, source=self.source)
            if self.release is None:
                self.checked.emit(client.check(self.version))
            else:
                manifest = client.download(self.release, Path(sys.executable),
                                           lambda done, total: self.progress.emit(done, total))
                self.prepared.emit(manifest)
            log_event('INFO', 'update.job_completed', operation=operation)
        except Cancelled:
            log_event('INFO', 'update.job_cancelled')
            self.cancelled_download.emit()
        except Exception as exc:
            record_request_error(exc, ui_text('检查更新') if self.release is None else ui_text('下载更新'))
            self.failed.emit(str(exc))


class UpdateDialog(QDialog):
    def __init__(self, version, parent, *, source=None, release=None, controller=None):
        super().__init__(parent)
        self.controller = controller if controller is not None else parent
        self.setWindowTitle(ui_text('检查更新（本地测试）') if source is not None else ui_text('检查更新'))
        self.setWindowModality(Qt.WindowModal)
        self.resize(640, 480)
        self.version = version
        self.source = source
        self.worker = None
        self.release = None
        self.manifest = None
        self.closing = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 18)
        layout.setSpacing(16)
        layout.addWidget(title_block(ui_text('软件更新'), ui_message('当前版本：{0}', version), large=True))
        if source is not None:
            hint = QLabel(ui_message('本地模拟更新源：{0}\n仅本次启动生效，使用直连。', source.origin))
            if source.current_version is not None:
                hint.setText(hint.source_text() + ui_message('\n版本比较模拟为 {0}，程序实际版本仍为 {1}。', source.current_version, version))
            hint.setWordWrap(True)
            layout.addWidget(hint)
        self.check_message = ui_text('正在检查本地模拟更新源…') if source is not None else ui_text('正在检查 GitHub Release…')
        self.status = QLabel(self.check_message)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.notes = QPlainTextEdit()
        self.notes.setReadOnly(True)
        self.notes.setPlaceholderText(ui_text('新版说明将在这里显示'))
        layout.addWidget(self.notes, 1)
        self.progress = QProgressBar()
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(6)
        self.progress.setRange(0, 0)
        layout.addWidget(self.progress)
        buttons = QHBoxLayout()
        self.install = IconButton(ui_text('下载并重启'), 'download', role='primary')
        self.install.setEnabled(False)
        self.install.clicked.connect(self.perform_action)

        self.page = QPushButton(ui_text('打开发布页面'))
        self.page.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(self.release.page_url if self.release else
            self.source.origin if self.source is not None else RELEASES_URL)))
        buttons.addWidget(self.page)
        buttons.addStretch()
        self.dismiss = QPushButton(ui_text('取消'))
        self.dismiss.clicked.connect(self.close)
        buttons.addWidget(self.dismiss)
        buttons.addWidget(self.install)
        layout.addLayout(buttons)
        if release is None:
            self.start_worker()
        else:
            self.on_checked(release)
            self.install.setEnabled(bool(getattr(sys, 'frozen', False)))

    def start_worker(self, release=None):
        self.manifest = None
        self.install.setEnabled(False)
        self.progress.show()
        self.progress.setRange(0, 0)
        self.dismiss.setText(ui_text('取消'))
        worker = UpdateWorker(self.version, release, self, source=self.source)
        self.worker = worker
        worker.checked.connect(self.on_checked)
        worker.prepared.connect(self.on_prepared)
        worker.progress.connect(self.on_progress)
        worker.failed.connect(self.on_failed)
        worker.finished.connect(self.on_finished)
        worker.start()

    def on_checked(self, release):
        self.release = release
        self.progress.hide()
        if release is None:
            self.status.setText(ui_message('当前已是最新版本（{0}）。', self.version))
            self.notes.clear()
        else:
            suffix = '' if getattr(sys, 'frozen', False) else ui_text('\n源码运行请打开发布页面下载便携 EXE。')
            self.status.setText(ui_message('发现新版本 {0}，下载约 {1:.1f} MiB。{2}', release.version, release.size / 1024**2, suffix))
            self.notes.setPlainText(release.notes or ui_text('此版本没有附加发布说明。'))
        self.dismiss.setText(ui_text('关闭'))

    def perform_action(self):
        if self.worker is not None:
            return
        if self.release is None:
            self.status.setText(self.check_message)
            self.start_worker()
            return
        if self.controller.batch_running:
            self.status.setText(ui_text('请等待当前模型下载完成，再下载并安装更新。'))
            return
        self.status.setText(ui_text('正在下载新版，完成校验后将关闭程序并重新启动…'))
        self.start_worker(self.release)

    def on_progress(self, done, total):
        self.progress.setRange(0, 100)
        self.progress.setValue(int(done * 100 / total))
        self.status.setText(ui_message('下载新版：{0:.1f} / {1:.1f} MiB；完成后校验并重启。', done / 1024**2, total / 1024**2))

    def on_prepared(self, manifest):
        self.manifest = manifest
        self.status.setText(ui_text('SHA-256 校验通过，正在准备重启…'))

    def on_failed(self, message):
        self.status.setText(message)
        self.progress.hide()
        self.dismiss.setText(ui_text('关闭'))

    def on_finished(self):
        worker, self.worker = self.worker, None
        if worker:
            worker.deleteLater()
        if self.closing:
            if self.manifest is not None:
                discard_update(self.manifest)
                self.manifest = None
            self.close()
            return
        if self.manifest is not None:
            try:
                self.controller.begin_update(self.manifest)
                self.accept()
                return
            except Exception as exc:
                record_error(exc, 'update.helper_launch_failed')
                self.on_failed(ui_text('无法启动更新：') + str(exc))
                discard_update(self.manifest)
                self.manifest = None
        self.install.setText(ui_text('重试') if self.release is None else ui_text('下载并重启'))
        self.install.setEnabled(self.release is None or bool(getattr(sys, 'frozen', False)))

    def reject(self):
        # Escape and the title-bar close button obey the same cancellation path.
        if self.worker is not None:
            self.close()
        else:
            super().reject()

    def closeEvent(self, event):
        if self.worker is not None:
            self.closing = True
            self.worker.cancelled.set()
            self.dismiss.setEnabled(False)
            self.status.setText(ui_text('正在取消，等待当前请求结束…'))
            event.ignore()
            return
        event.accept()
