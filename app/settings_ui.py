"""Proxy and logging preferences in the native settings window."""
from PySide6.QtCore import Qt, QUrl, QThread
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (QComboBox, QDialog, QFormLayout, QGroupBox, QHBoxLayout,
                               QLabel, QPushButton, QVBoxLayout, QMessageBox)

from app_settings import LOG_LEVELS, PROXY_OPTIONS, Preferences
from app_logging import (get_log_directory, log_event, record_error, logging_health,
                         contextual, new_context)
from log_support import package_logs, clear_logs


class LogPackageWorker(QThread):
    def __init__(self, parent):
        super().__init__(parent)
        self.log_context = new_context(feature='logs')
        self.result = None
        self.error = None

    @contextual
    def run(self):
        try:
            self.result = package_logs()
        except Exception as exc:
            record_error(exc, 'logs.package_failed')
            self.error = type(exc).__name__


class SettingsDialog(QDialog):
    def __init__(self, preferences, save, parent):
        super().__init__(parent)
        self.save = save
        self.worker = None
        self.setWindowTitle('设置')
        self.setWindowModality(Qt.WindowModal)
        self.setMinimumWidth(510)
        self.resize(540, 370)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 18)
        layout.setSpacing(18)

        update_group = QGroupBox('软件更新')
        update_layout = QHBoxLayout(update_group)
        update_layout.setContentsMargins(16, 22, 16, 16)
        from PySide6.QtWidgets import QApplication
        update_layout.addWidget(QLabel('当前版本：' + QApplication.applicationVersion()))
        update_layout.addStretch()
        self.update_button = QPushButton('检查更新')
        self.update_button.setAutoDefault(False)
        self.update_button.setToolTip('使用已保存的更新代理设置检查新版本')
        self.update_button.clicked.connect(parent.check_updates)
        update_layout.addWidget(self.update_button)
        layout.addWidget(update_group)

        proxy_group = QGroupBox('代理设置')
        proxy_form = QFormLayout(proxy_group)
        proxy_form.setContentsMargins(16, 22, 16, 16)
        proxy_form.setSpacing(12)
        self.store_proxy_combo = self.proxy_combo(preferences.store_proxy)
        self.update_proxy_combo = self.proxy_combo(preferences.update_proxy)
        proxy_form.addRow('立创商城', self.store_proxy_combo)
        proxy_form.addRow('检查更新', self.update_proxy_combo)
        hint = QLabel('两项独立生效，保存后用于后续请求。')
        hint.setObjectName('muted')
        proxy_form.addRow(hint)
        layout.addWidget(proxy_group)

        log_group = QGroupBox('日志')
        log_form = QFormLayout(log_group)
        log_form.setContentsMargins(16, 22, 16, 16)
        log_form.setSpacing(12)
        self.log_level_combo = QComboBox()
        for level in LOG_LEVELS:
            self.log_level_combo.addItem(level.title(), level)
        self.log_level_combo.setCurrentIndex(self.log_level_combo.findData(preferences.log_level))
        log_form.addRow('输出等级', self.log_level_combo)
        log_hint = QLabel('Debug 记录最详细，适合排查问题。')
        log_hint.setObjectName('muted')
        log_form.addRow(log_hint)
        self.open_log_button = QPushButton('打开日志')
        self.open_log_button.clicked.connect(self.open_log_directory)
        self.package_log_button = QPushButton('打包日志')
        self.package_log_button.clicked.connect(self.start_log_package)
        self.clear_log_button = QPushButton('清除日志')
        self.clear_log_button.clicked.connect(self.confirm_clear_logs)
        log_buttons = QHBoxLayout()
        for button in (self.open_log_button, self.package_log_button, self.clear_log_button):
            button.setAutoDefault(False)
            log_buttons.addWidget(button)
        log_form.addRow(log_buttons)
        layout.addWidget(log_group)

        self.status = QLabel('')
        self.status.setTextFormat(Qt.PlainText)
        self.status.setWordWrap(True)
        self.status.hide()
        if logging_health():
            self.status.setText('日志目前无法写入，请检查应用数据目录的权限或磁盘空间。')
            self.status.show()
        layout.addWidget(self.status)
        buttons = QHBoxLayout()
        buttons.addStretch()
        self.cancel_button = QPushButton('取消')
        self.cancel_button.clicked.connect(self.reject)
        buttons.addWidget(self.cancel_button)
        self.save_button = QPushButton('保存')
        self.save_button.setObjectName('primary')
        self.save_button.setDefault(True)
        self.save_button.clicked.connect(self.save_preferences)
        buttons.addWidget(self.save_button)
        layout.addLayout(buttons)

    @staticmethod
    def proxy_combo(value):
        combo = QComboBox()
        for title, mode in PROXY_OPTIONS:
            combo.addItem(title, mode)
        combo.setCurrentIndex(combo.findData(value))
        return combo

    def save_preferences(self):
        preferences = Preferences(store_proxy=self.store_proxy_combo.currentData(),
                                  update_proxy=self.update_proxy_combo.currentData(),
                                  log_level=self.log_level_combo.currentData())
        if self.save(preferences):
            self.accept()
        else:
            self.status.setText('无法保存设置，请检查本机应用数据目录的写入权限后重试。')
            self.status.show()

    def open_log_directory(self):
        try:
            directory = get_log_directory()
            directory.mkdir(parents=True, exist_ok=True)
            if QDesktopServices.openUrl(QUrl.fromLocalFile(str(directory))):
                log_event('INFO', 'navigation.logs_opened')
                return
            log_event('WARNING', 'navigation.logs_open_failed', reason='shell_rejected')
        except OSError as exc:
            record_error(exc, 'navigation.logs_open_failed')
        self.status.setText('无法打开日志目录，请检查本机目录权限。')
        self.status.show()

    def start_log_package(self):
        if self.worker is not None:
            return
        self.set_log_busy(True)
        self.status.setText('正在打包日志…')
        self.status.show()
        self.worker = LogPackageWorker(self)
        self.worker.finished.connect(self.finish_log_package)
        self.worker.start()

    def set_log_busy(self, busy):
        for widget in (self.package_log_button, self.clear_log_button, self.open_log_button,
                       self.save_button, self.cancel_button, self.store_proxy_combo,
                       self.update_proxy_combo, self.log_level_combo):
            widget.setEnabled(not busy)

    def finish_log_package(self):
        worker, self.worker = self.worker, None
        self.set_log_busy(False)
        if worker.result is None:
            self.status.setText('打包失败，请检查应用数据目录权限和磁盘空间后重试。')
        else:
            result = worker.result
            path = result['path']
            prefix = '日志已打包。'
            if result['issues']:
                prefix = '打包完成，部分日志未能完整收集，详见 ZIP 内 diagnostics.json。'
            elif not result['log_count']:
                prefix = '暂无日志文件，已打包当前诊断信息；请复现问题后再次打包。'
            self.status.setText(f'{prefix}\n{path}\n反馈时请附上操作步骤、发生时间和报错截图。')
            try:
                opened = QDesktopServices.openUrl(QUrl.fromLocalFile(str(path.parent)))
                log_event('INFO' if opened else 'WARNING', 'navigation.log_package_directory', opened=opened)
                if not opened:
                    self.status.setText(self.status.text() + '\n无法自动打开目录，请按上方路径取出 ZIP。')
            except OSError as exc:
                record_error(exc, 'navigation.log_package_directory_failed')
                self.status.setText(self.status.text() + '\n无法自动打开目录，请按上方路径取出 ZIP。')
        self.status.show()
        worker.deleteLater()

    def confirm_clear_logs(self):
        if self.worker is not None:
            return
        choice = QMessageBox.question(self, '清除日志',
            '将清除当前及历史日志（包括更新、崩溃日志），无法恢复。\n'
            '清除后会继续记录，已打包的 ZIP 会保留。建议先打包需要反馈的日志。',
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if choice != QMessageBox.Yes:
            return
        try:
            result = clear_logs()
            if result['failures']:
                self.status.setText(f'已清除 {len(result["cleared"])} 个日志文件；'
                    f'{len(result["failures"])} 个文件被占用或无法清除，请关闭其他 LCSC3D 后重试。')
            else:
                self.status.setText('日志已清除，后续操作会继续记录。已打包的 ZIP 保留。')
        except OSError as exc:
            record_error(exc, 'logs.clear_failed')
            self.status.setText('清除失败，请检查日志目录权限后重试。')
        self.status.show()

    def done(self, result):
        if self.worker is None:
            super().done(result)

    def closeEvent(self, event):
        if self.worker is not None:
            event.ignore()
        else:
            super().closeEvent(event)
