"""Preferences, diagnostics and project support in the settings window."""
from i18n import text as ui_text, message as ui_message
from pathlib import Path
import sys

from PySide6.QtCore import Qt, QUrl, QThread
from PySide6.QtGui import QColor, QDesktopServices, QPixmap, QIcon, QFont, QFontDatabase
from PySide6.QtWidgets import (QHBoxLayout, QVBoxLayout, QScrollArea, QWidget, QSpinBox, QAbstractSpinBox, QStackedWidget, QButtonGroup, QFrame)
from localized_widgets import (QComboBox, QDialog, QFormLayout, QGroupBox, QLabel, QPushButton)

from app_settings import (LOG_LEVELS, PROXY_OPTIONS, Preferences, LANGUAGES,
                          THEME_MODES, ACCENT_COLORS, DEFAULT_ACCENT, DEFAULT_FONT_FAMILY,
                          DEFAULT_FONT_SIZE, MIN_FONT_SIZE, MAX_FONT_SIZE)
from app_theme import effective_font_family, foreground
from ui_components import IconButton, title_block, scroll_page
from shell_ui import AnchoredComboBox as NativeComboBox, choose_color, notify
from shiboken6 import isValid
from app_logging import (get_log_directory, log_event, record_error, logging_health,
                         contextual, new_context)
from log_support import package_logs, clear_logs


REPOSITORY_URL = 'https://github.com/Travelerrrrrr/LCSC3D'
SPONSORSHIP_DIRECTORY = (Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parents[1]))
                         / 'docs' / 'images' / 'sponsorship')


class SponsorshipDialog(QDialog):
    def __init__(self, parent):
        super().__init__(parent)
        self.setWindowTitle(ui_text('赞助作者'))
        self.setWindowModality(Qt.WindowModal)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 18)
        layout.setSpacing(14)
        heading = QLabel(ui_text('感谢你支持 LCSC3D 的开发与维护！'))
        heading.setObjectName('section')
        heading.setWordWrap(True)
        layout.addWidget(heading)
        hint = QLabel(ui_text('使用支付宝或微信扫描对应收款码。自愿赞助，金额随意。'))
        hint.setObjectName('muted')
        hint.setWordWrap(True)
        layout.addWidget(hint)

        content = QWidget()
        codes = QHBoxLayout(content)
        codes.setContentsMargins(0, 0, 0, 0)
        codes.setSpacing(16)
        self.code_labels = []
        for title, filename in ((ui_text('支付宝'), 'alipay.png'), (ui_text('微信支付'), 'wechat.png')):
            card = QGroupBox(title)
            card_layout = QVBoxLayout(card)
            card_layout.setContentsMargins(12, 22, 12, 12)
            code = QLabel()
            code.setAlignment(Qt.AlignCenter)
            code.setAccessibleName(title + ui_text('赞助收款码'))
            pixmap = QPixmap(str(SPONSORSHIP_DIRECTORY / filename))
            if pixmap.isNull():
                code.setText(ui_text('收款码加载失败，请重新下载完整程序。'))
                code.setWordWrap(True)
                code.setMinimumSize(280, 420)
            else:
                ratio = self.devicePixelRatioF()
                preview = pixmap.scaled(round(280 * ratio), round(420 * ratio),
                                        Qt.KeepAspectRatio, Qt.SmoothTransformation)
                preview.setDevicePixelRatio(ratio)
                code.setPixmap(preview)
            self.code_labels.append(code)
            card_layout.addWidget(code)
            codes.addWidget(card)
        scroll = QScrollArea()
        scroll.setFrameShape(QScrollArea.NoFrame)
        scroll.setWidgetResizable(True)
        scroll.setWidget(content)
        layout.addWidget(scroll, 1)

        buttons = QHBoxLayout()
        buttons.addStretch()
        self.close_button = QPushButton(ui_text('关闭'))
        self.close_button.setDefault(True)
        self.close_button.clicked.connect(self.accept)
        buttons.addWidget(self.close_button)
        layout.addLayout(buttons)
        available = self.screen().availableGeometry()
        self.resize(min(680, available.width() - 40), min(620, available.height() - 40))


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
        self.sponsorship_dialog = None
        self.setWindowTitle(ui_text('设置'))
        self.setWindowModality(Qt.WindowModal)
        self.setMinimumSize(680, 480)
        self.resize(900, min(800, self.screen().availableGeometry().height() - 60))
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 18)
        layout.setSpacing(18)
        layout.addWidget(title_block(ui_text('设置'), ui_text('让工作台适合你的使用习惯'), large=True))
        body = QHBoxLayout()
        body.setSpacing(20)
        navigation = QVBoxLayout()
        navigation.setSpacing(6)
        self.pages = QStackedWidget()
        self.section_buttons = []
        self.section_group = QButtonGroup(self)
        page_layouts = []
        for index, (title, symbol) in enumerate(((ui_text('外观'), 'workspace'), (ui_text('网络'), 'store'),
                                                 (ui_text('日志与诊断'), 'settings'), (ui_text('关于与支持'), 'help'))):
            button = IconButton(title, symbol, role='navButton')
            button.setCheckable(True)
            button.setChecked(index == 0)
            button.clicked.connect(lambda checked=False, page=index: self.pages.setCurrentIndex(page))
            self.section_group.addButton(button)
            self.section_buttons.append(button)
            navigation.addWidget(button)
            content = QWidget()
            sections = QVBoxLayout(content)
            sections.setContentsMargins(2, 0, 10, 4)
            sections.setSpacing(18)
            page = scroll_page(content)
            self.pages.addWidget(page)
            page_layouts.append(sections)
            if index == 0:
                self.scroll = page
        navigation.addStretch()
        body.addLayout(navigation)
        body.addWidget(self.pages, 1)
        layout.addLayout(body, 1)

        theme_group = QGroupBox(ui_text('主题设置'))
        theme_form = QFormLayout(theme_group)
        theme_form.setContentsMargins(16, 22, 16, 16)
        theme_form.setSpacing(12)
        self.language_combo = QComboBox()
        for title, value in LANGUAGES:
            self.language_combo.addItem(title, value)
        self.language_combo.setCurrentIndex(self.language_combo.findData(preferences.language))
        theme_form.addRow(ui_text('语言 / Language'), self.language_combo)
        self.theme_mode_combo = QComboBox()
        for title, value in THEME_MODES:
            self.theme_mode_combo.addItem(title, value)
        self.theme_mode_combo.setCurrentIndex(self.theme_mode_combo.findData(preferences.theme_mode))
        theme_form.addRow(ui_text('明暗模式'), self.theme_mode_combo)
        accent_row = QHBoxLayout()
        self.accent_combo = QComboBox()
        for title, value in ACCENT_COLORS:
            swatch = QPixmap(18, 18)
            swatch.fill(QColor(value))
            self.accent_combo.addItem(QIcon(swatch), title, value)
        self.accent_combo.addItem(ui_text('自定义'), preferences.accent_color)
        selected = next((i for i, (_, value) in enumerate(ACCENT_COLORS) if value == preferences.accent_color), len(ACCENT_COLORS))
        self.accent_combo.setCurrentIndex(selected)
        self.color_button = QPushButton(ui_text('选择颜色…'))
        self.color_button.setAutoDefault(False)
        self.color_button.clicked.connect(self.choose_accent)
        accent_row.addWidget(self.accent_combo, 1)
        accent_row.addWidget(self.color_button)
        theme_form.addRow(ui_text('APP 配色'), accent_row)

        text_color_row = QHBoxLayout()
        self.text_color_combo = QComboBox()
        for title, value in ((ui_text('自动'), 'auto'), (ui_text('白色'), '#ffffff'), (ui_text('黑色'), '#000000')):
            self.text_color_combo.addItem(title, value)
        self.text_color_combo.addItem(ui_text('自定义'), preferences.accent_text_color if preferences.accent_text_color != 'auto' else '#ffffff')
        selected = self.text_color_combo.findData(preferences.accent_text_color)
        self.text_color_combo.setCurrentIndex(selected if selected >= 0 else 3)
        self.text_color_button = QPushButton(ui_text('选择文字颜色…'))
        self.text_color_button.setAutoDefault(False)
        self.text_color_button.clicked.connect(self.choose_text_color)
        text_color_row.addWidget(self.text_color_combo, 1)
        text_color_row.addWidget(self.text_color_button)
        theme_form.addRow(ui_text('按钮文字颜色'), text_color_row)
        text_hint = QLabel(ui_text('用于彩色按钮和选中项；自动模式按配色选择黑字或白字。'))
        text_hint.setObjectName('muted')
        text_hint.setWordWrap(True)
        theme_form.addRow(text_hint)

        font_row = QHBoxLayout()
        # Keep the full font list compact; the native font picker widens its
        # preview popup and Fusion's menu mode ignores maxVisibleItems.
        self.font_combo = NativeComboBox()
        self.font_combo.addItems(QFontDatabase.families())
        self.font_combo.setSizeAdjustPolicy(NativeComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.font_combo.setMinimumContentsLength(18)
        self.font_combo.setMaxVisibleItems(8)
        self.font_combo.setStyleSheet('QComboBox { combobox-popup: 0; } '
                                     'QComboBox QAbstractItemView { padding:0; } '
                                     'QComboBox QAbstractItemView::item { min-height:24px; padding:0 4px; margin:0; }')
        self.font_combo.view().setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.font_combo.setCurrentText(effective_font_family(preferences.font_family))
        self.reset_font_button = QPushButton(ui_text('恢复默认'))
        self.reset_font_button.setAutoDefault(False)
        self.reset_font_button.clicked.connect(lambda: self.font_combo.setCurrentText(effective_font_family(DEFAULT_FONT_FAMILY)))
        font_row.addWidget(self.font_combo, 1)
        font_row.addWidget(self.reset_font_button)
        theme_form.addRow(ui_text('界面字体'), font_row)

        size_row = QHBoxLayout()
        self.font_size_spin = QSpinBox()
        self.font_size_spin.setRange(MIN_FONT_SIZE, MAX_FONT_SIZE)
        self.font_size_spin.setSuffix(' px')
        self.font_size_spin.setValue(preferences.font_size)
        self.font_size_spin.setButtonSymbols(QAbstractSpinBox.NoButtons)
        self.smaller_font_button = QPushButton('−')
        self.larger_font_button = QPushButton('+')
        self.smaller_font_button.setToolTip(ui_text('减小字号'))
        self.larger_font_button.setToolTip(ui_text('增大字号'))
        for button in (self.smaller_font_button, self.larger_font_button):
            button.setAutoDefault(False)
        self.smaller_font_button.clicked.connect(self.font_size_spin.stepDown)
        self.larger_font_button.clicked.connect(self.font_size_spin.stepUp)
        self.reset_size_button = QPushButton(ui_text('恢复默认'))
        self.reset_size_button.setAutoDefault(False)
        self.reset_size_button.clicked.connect(lambda: self.font_size_spin.setValue(DEFAULT_FONT_SIZE))
        size_row.addWidget(self.font_size_spin, 1)
        size_row.addWidget(self.smaller_font_button)
        size_row.addWidget(self.larger_font_button)
        size_row.addWidget(self.reset_size_button)
        theme_form.addRow(ui_text('界面字号'), size_row)

        self.color_preview = QLabel()
        self.color_preview.setAlignment(Qt.AlignCenter)
        self.color_preview.setWordWrap(True)
        self.accent_combo.currentIndexChanged.connect(self.update_color_preview)
        self.text_color_combo.currentIndexChanged.connect(self.update_color_preview)
        self.font_combo.currentTextChanged.connect(self.update_color_preview)
        self.font_size_spin.valueChanged.connect(self.update_color_preview)
        self.update_color_preview()
        theme_form.addRow(self.color_preview)
        theme_hint = QLabel(ui_text('保存后立即生效。English 使用 LCSC 国际商城。'))
        theme_hint.setObjectName('muted')
        theme_hint.setWordWrap(True)
        theme_form.addRow(theme_hint)
        page_layouts[0].addWidget(theme_group)

        update_group = QGroupBox(ui_text('软件更新'))
        update_layout = QHBoxLayout(update_group)
        update_layout.setContentsMargins(16, 22, 16, 16)
        from PySide6.QtWidgets import QApplication
        update_layout.addWidget(QLabel(ui_text('当前版本：') + QApplication.applicationVersion()))
        update_layout.addStretch()
        self.update_button = QPushButton(ui_text('检查更新'))
        self.update_button.setAutoDefault(False)
        self.update_button.setToolTip(ui_text('使用已保存的更新代理设置检查新版本'))
        self.update_button.clicked.connect(parent.check_updates)
        update_layout.addWidget(self.update_button)
        page_layouts[3].addWidget(update_group)

        proxy_group = QGroupBox(ui_text('代理设置'))
        proxy_form = QFormLayout(proxy_group)
        proxy_form.setContentsMargins(16, 22, 16, 16)
        proxy_form.setSpacing(12)
        self.store_proxy_combo = self.proxy_combo(preferences.store_proxy)
        self.update_proxy_combo = self.proxy_combo(preferences.update_proxy)
        proxy_form.addRow(ui_text('立创商城'), self.store_proxy_combo)
        proxy_form.addRow(ui_text('检查更新'), self.update_proxy_combo)
        hint = QLabel(ui_text('两项独立生效，保存后用于后续请求。'))
        hint.setObjectName('muted')
        proxy_form.addRow(hint)
        page_layouts[1].addWidget(proxy_group)

        log_group = QGroupBox(ui_text('日志'))
        log_form = QFormLayout(log_group)
        log_form.setContentsMargins(16, 22, 16, 16)
        log_form.setSpacing(12)
        self.log_level_combo = QComboBox()
        for level in LOG_LEVELS:
            self.log_level_combo.addItem(level.title(), level)
        self.log_level_combo.setCurrentIndex(self.log_level_combo.findData(preferences.log_level))
        log_form.addRow(ui_text('输出等级'), self.log_level_combo)
        log_hint = QLabel(ui_text('Debug 记录最详细，适合排查问题。'))
        log_hint.setObjectName('muted')
        log_form.addRow(log_hint)
        self.open_log_button = QPushButton(ui_text('打开日志'))
        self.open_log_button.clicked.connect(self.open_log_directory)
        self.package_log_button = QPushButton(ui_text('打包日志'))
        self.package_log_button.clicked.connect(self.start_log_package)
        self.clear_log_button = QPushButton(ui_text('清除日志'))
        self.clear_log_button.clicked.connect(self.confirm_clear_logs)
        log_buttons = QHBoxLayout()
        for button in (self.open_log_button, self.package_log_button, self.clear_log_button):
            button.setAutoDefault(False)
            log_buttons.addWidget(button)
        log_form.addRow(log_buttons)
        page_layouts[2].addWidget(log_group)

        support_group = QGroupBox(ui_text('赞助与支持'))
        support_layout = QHBoxLayout(support_group)
        support_layout.setContentsMargins(16, 22, 16, 16)
        support_layout.setSpacing(12)
        self.star_button = QPushButton(ui_text('⭐点个Star⭐'))
        self.star_button.setToolTip(ui_text('在浏览器中打开 LCSC3D 仓库首页'))
        self.star_button.clicked.connect(self.open_repository)
        self.sponsor_button = QPushButton(ui_text('🍔赞助作者🍔'))
        self.sponsor_button.setToolTip(ui_text('查看支付宝与微信收款码'))
        self.sponsor_button.clicked.connect(self.show_sponsorship)
        for button in (self.star_button, self.sponsor_button):
            button.setAutoDefault(False)
            support_layout.addWidget(button)
        page_layouts[3].addWidget(support_group)
        credit = QLabel('LCSC3D · AGPL-3.0<br>UI: <a href="https://github.com/UN-GCPDS/qt-material">UN-GCPDS / qt-material</a> · BSD-2-Clause')
        credit.setObjectName('muted')
        credit.setOpenExternalLinks(True)
        credit.setWordWrap(True)
        page_layouts[3].addWidget(credit)
        for page_layout in page_layouts:
            page_layout.addStretch()

        self.status = QLabel('')
        self.status.setTextFormat(Qt.PlainText)
        self.status.setWordWrap(True)
        self.status.hide()
        if logging_health():
            self.status.setText(ui_text('日志目前无法写入，请检查应用数据目录的权限或磁盘空间。'))
            self.status.show()
        layout.addWidget(self.status)
        buttons = QHBoxLayout()
        buttons.addStretch()
        self.cancel_button = QPushButton(ui_text('取消'))
        self.cancel_button.clicked.connect(self.reject)
        buttons.addWidget(self.cancel_button)
        self.save_button = QPushButton(ui_text('保存'))
        self.save_button.setObjectName('primary')
        self.save_button.setDefault(True)
        self.save_button.clicked.connect(self.save_preferences)
        buttons.addWidget(self.save_button)
        layout.addLayout(buttons)

    def choose_accent(self):
        def selected(value):
            index = len(ACCENT_COLORS)
            self.accent_combo.setItemData(index, value.name())
            self.accent_combo.setCurrentIndex(index)
            self.update_color_preview()
        self.color_page = choose_color(self, QColor(self.accent_combo.currentData()), ui_text('选择 APP 配色'), selected)

    def choose_text_color(self):
        current = self.text_color_combo.currentData()
        if current == 'auto':
            current = foreground(self.accent_combo.currentData())
        def selected(value):
            self.text_color_combo.setItemData(3, value.name())
            self.text_color_combo.setCurrentIndex(3)
            self.update_color_preview()
        self.color_page = choose_color(self, QColor(current), ui_text('选择按钮文字颜色'), selected)

    def update_color_preview(self, *_):
        value = self.accent_combo.currentData() or DEFAULT_ACCENT
        text_color = self.text_color_combo.currentData()
        if text_color == 'auto':
            text_color = foreground(value)
        font = QFont(self.font_combo.currentText())
        font.setPixelSize(self.font_size_spin.value())
        self.color_preview.setFont(font)
        self.color_preview.setText(ui_text('配色与字体预览 · ') + 'LCSC3D Aa 123 · ' + value.upper())
        family = self.font_combo.currentText().replace('\\', '\\\\').replace('"', '\\"')
        self.color_preview.setStyleSheet(ui_message('background:{0};color:{1};font-size:{2}px;padding:7px;border-radius:6px;font-family:"{3}";',
                                                    value, text_color, self.font_size_spin.value(), family))

    def open_repository(self):
        if QDesktopServices.openUrl(QUrl(REPOSITORY_URL)):
            log_event('INFO', 'navigation.repository_opened')
            return
        log_event('WARNING', 'navigation.repository_open_failed')
        self.status.setText(ui_text('无法打开浏览器，请手动访问：') + REPOSITORY_URL)
        self.status.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.status.show()

    def show_sponsorship(self):
        if self.sponsorship_dialog is None:
            self.sponsorship_dialog = SponsorshipDialog(self)
        self.sponsorship_dialog.show()
        self.sponsorship_dialog.raise_()
        self.sponsorship_dialog.activateWindow()

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
                                  log_level=self.log_level_combo.currentData(),
                                  language=self.language_combo.currentData(),
                                  theme_mode=self.theme_mode_combo.currentData(),
                                  accent_color=self.accent_combo.currentData(),
                                  accent_text_color=self.text_color_combo.currentData(),
                                  font_family=self.font_combo.currentText(), font_size=self.font_size_spin.value())
        if self.save(preferences):
            self.accept()
        else:
            self.status.setText(ui_text('无法保存设置，请检查本机应用数据目录的写入权限后重试。'))
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
        self.status.setText(ui_text('无法打开日志目录，请检查本机目录权限。'))
        self.status.show()

    def start_log_package(self):
        if self.worker is not None:
            return
        self.set_log_busy(True)
        self.status.setText(ui_text('正在打包日志…'))
        self.status.show()
        self.worker = LogPackageWorker(self)
        self.worker.finished.connect(self.finish_log_package)
        self.worker.start()

    def set_log_busy(self, busy):
        for widget in (self.package_log_button, self.clear_log_button, self.open_log_button,
                       self.save_button, self.cancel_button, self.store_proxy_combo,
                       self.update_proxy_combo, self.log_level_combo, self.language_combo,
                       self.theme_mode_combo, self.accent_combo, self.color_button,
                       self.text_color_combo, self.text_color_button, self.font_combo, self.reset_font_button,
                       self.font_size_spin, self.reset_size_button, self.smaller_font_button, self.larger_font_button):
            widget.setEnabled(not busy)

    def finish_log_package(self):
        worker, self.worker = self.worker, None
        self.set_log_busy(False)
        if worker.result is None:
            self.status.setText(ui_text('打包失败，请检查应用数据目录权限和磁盘空间后重试。'))
        else:
            result = worker.result
            path = result['path']
            prefix = ui_text('日志已打包。')
            if result['issues']:
                prefix = ui_text('打包完成，部分日志未能完整收集，详见 ZIP 内 diagnostics.json。')
            elif not result['log_count']:
                prefix = ui_text('暂无日志文件，已打包当前诊断信息；请复现问题后再次打包。')
            self.status.setText(ui_message('{0}\n{1}\n反馈时请附上操作步骤、发生时间和报错截图。', prefix, path))
            try:
                opened = QDesktopServices.openUrl(QUrl.fromLocalFile(str(path.parent)))
                log_event('INFO' if opened else 'WARNING', 'navigation.log_package_directory', opened=opened)
                if not opened:
                    self.status.setText(self.status.source_text() + ui_text('\n无法自动打开目录，请按上方路径取出 ZIP。'))
            except OSError as exc:
                record_error(exc, 'navigation.log_package_directory_failed')
                self.status.setText(self.status.source_text() + ui_text('\n无法自动打开目录，请按上方路径取出 ZIP。'))
        self.status.show()
        worker.deleteLater()

    def confirm_clear_logs(self):
        if self.worker is not None:
            return
        previous = getattr(self, 'confirmation_notice', None)
        if previous is not None and isValid(previous):
            previous.close()
        self.confirmation_notice = notify(self, ui_text('清除日志'),
            ui_text('将清除当前及历史日志（包括更新、崩溃日志），无法恢复。\n'
            '清除后会继续记录，已打包的 ZIP 会保留。建议先打包需要反馈的日志。'),
            severity='warning', actions=((ui_text('取消'), None), (ui_text('清除日志'), self.clear_confirmed)), persistent=True)

    def clear_confirmed(self):
        if self.worker is not None:
            return
        try:
            result = clear_logs()
            if result['failures']:
                self.status.setText(ui_message('已清除 {0} 个日志文件；{1} 个文件被占用或无法清除，请关闭其他 LCSC3D 后重试。', len(result["cleared"]), len(result["failures"])))
            else:
                self.status.setText(ui_text('日志已清除，后续操作会继续记录。已打包的 ZIP 保留。'))
        except OSError as exc:
            record_error(exc, 'logs.clear_failed')
            self.status.setText(ui_text('清除失败，请检查日志目录权限后重试。'))
        self.status.show()

    def done(self, result):
        if self.worker is None:
            super().done(result)

    def closeEvent(self, event):
        if self.worker is not None:
            event.ignore()
        else:
            super().closeEvent(event)
