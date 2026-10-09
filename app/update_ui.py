"""Asynchronous check, download progress and cancel/retry controls."""
import sys
import threading
from pathlib import Path

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtCore import QUrl
from PySide6.QtWidgets import QDialog, QHBoxLayout, QLabel, QPlainTextEdit, QProgressBar, QPushButton, QVBoxLayout

from errors import Cancelled
from updater import RELEASES_URL, UpdateClient, discard_update
from app_logging import log_event, new_context, contextual, record_error
from store_diagnostics import record_request_error


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
            operation = '检查更新' if self.release is None else '下载更新'
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
            record_request_error(exc, '检查更新' if self.release is None else '下载更新')
            self.failed.emit(str(exc))


class UpdateDialog(QDialog):
    def __init__(self, version, parent, *, source=None):
        super().__init__(parent)
        self.setWindowTitle('检查更新（本地测试）' if source is not None else '检查更新')
        self.setWindowModality(Qt.WindowModal)
        self.resize(540, 360)
        self.version = version
        self.source = source
        self.worker = None
        self.release = None
        self.manifest = None
        self.closing = False
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(f'当前版本：{version}'))
        if source is not None:
            hint = QLabel(f'本地模拟更新源：{source.origin}\n仅本次启动生效，使用直连。')
            if source.current_version is not None:
                hint.setText(hint.text() + f'\n版本比较模拟为 {source.current_version}，程序实际版本仍为 {version}。')
            hint.setWordWrap(True)
            layout.addWidget(hint)
        self.check_message = '正在检查本地模拟更新源…' if source is not None else '正在检查 GitHub Release…'
        self.status = QLabel(self.check_message)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.notes = QPlainTextEdit()
        self.notes.setReadOnly(True)
        self.notes.setPlaceholderText('新版说明将在这里显示')
        layout.addWidget(self.notes, 1)
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        layout.addWidget(self.progress)
        buttons = QHBoxLayout()
        self.install = QPushButton('下载并重启')
        self.install.setObjectName('primary')
        self.install.setEnabled(False)
        self.install.clicked.connect(self.perform_action)
        buttons.addWidget(self.install)
        self.page = QPushButton('打开发布页面')
        self.page.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(self.release.page_url if self.release else
            self.source.origin if self.source is not None else RELEASES_URL)))
        buttons.addWidget(self.page)
        buttons.addStretch()
        self.dismiss = QPushButton('取消')
        self.dismiss.clicked.connect(self.close)
        buttons.addWidget(self.dismiss)
        layout.addLayout(buttons)
        self.start_worker()

    def start_worker(self, release=None):
        self.manifest = None
        self.install.setEnabled(False)
        self.progress.show()
        self.progress.setRange(0, 0)
        self.dismiss.setText('取消')
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
            self.status.setText(f'当前已是最新版本（{self.version}）。')
            self.notes.clear()
        else:
            suffix = '' if getattr(sys, 'frozen', False) else '\n源码运行请打开发布页面下载便携 EXE。'
            self.status.setText(f'发现新版本 {release.version}，下载约 {release.size / 1024**2:.1f} MiB。{suffix}')
            self.notes.setPlainText(release.notes or '此版本没有附加发布说明。')
        self.dismiss.setText('关闭')

    def perform_action(self):
        if self.worker is not None:
            return
        if self.release is None:
            self.status.setText(self.check_message)
            self.start_worker()
            return
        if self.parent().batch_running:
            self.status.setText('请等待当前模型下载完成，再下载并安装更新。')
            return
        self.status.setText('正在下载新版，完成校验后将关闭程序并重新启动…')
        self.start_worker(self.release)

    def on_progress(self, done, total):
        self.progress.setRange(0, 100)
        self.progress.setValue(int(done * 100 / total))
        self.status.setText(f'下载新版：{done / 1024**2:.1f} / {total / 1024**2:.1f} MiB；完成后校验并重启。')

    def on_prepared(self, manifest):
        self.manifest = manifest
        self.status.setText('SHA-256 校验通过，正在准备重启…')

    def on_failed(self, message):
        self.status.setText(message)
        self.progress.hide()
        self.dismiss.setText('关闭')

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
                self.parent().begin_update(self.manifest)
                self.accept()
                return
            except Exception as exc:
                record_error(exc, 'update.helper_launch_failed')
                self.on_failed('无法启动更新：' + str(exc))
                discard_update(self.manifest)
                self.manifest = None
        self.install.setText('重试' if self.release is None else '下载并重启')
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
            self.status.setText('正在取消，等待当前请求结束…')
            event.ignore()
            return
        event.accept()
