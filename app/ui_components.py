"""Shared Material surfaces, navigation and scalable line icons."""
from PySide6.QtCore import QByteArray, QSize, Qt, QRectF
from PySide6.QtGui import QIcon, QPainter, QPixmap, QColor
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QFrame, QHBoxLayout, QVBoxLayout, QScrollArea, QWidget, QTextBrowser, QSplitter, QSplitterHandle
from localized_widgets import QLabel, QPushButton, QDialog
from app_theme import theme_manager
from i18n import text as ui_text, render


class ColumnHandle(QSplitterHandle):
    def __init__(self, orientation, parent):
        super().__init__(orientation, parent)
        self.setCursor(Qt.SplitHCursor)
        self.setMouseTracking(True)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.fillRect(self.rect(), QColor(theme_manager().tokens['bg']))
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(theme_manager().tokens['accent_ink' if self.underMouse() else 'border']))
        grip = QRectF((self.width() - 4) / 2, (self.height() - 42) / 2, 4, 42)
        painter.drawRoundedRect(grip, 2, 2)

    def enterEvent(self, event):
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self.update()
        super().leaveEvent(event)


class ColumnSplitter(QSplitter):
    def __init__(self):
        super().__init__(Qt.Horizontal)
        self.setHandleWidth(24)
        self.setChildrenCollapsible(False)

    def createHandle(self):
        return ColumnHandle(self.orientation(), self)


ICON_PATHS = {
    'workspace': '<rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/>',
    'store': '<path d="M3 9h18l-2-6H5L3 9Zm1 0v12h16V9M9 21v-7h6v7M3 9c0 4 6 4 6 0 0 4 6 4 6 0 0 4 6 4 6 0"/>',
    'settings': '<path d="M4 7h16M4 17h16"/><circle cx="9" cy="7" r="3"/><circle cx="15" cy="17" r="3"/>',
    'help': '<circle cx="12" cy="12" r="9"/><path d="M9 9a3 3 0 0 1 6 0c0 2-3 2-3 4m0 3v.5"/>',
    'user': '<circle cx="12" cy="8" r="4"/><path d="M4 21v-2a8 8 0 0 1 16 0v2"/>',
    'folder': '<path d="M3 6h6l2 2h10v12H3V6Zm0 4h18"/>',
    'download': '<path d="M12 3v12m-5-5 5 5 5-5M4 16v5h16v-5"/>',
    'add': '<path d="M12 4v16M4 12h16"/>',
    'search': '<circle cx="10" cy="10" r="6"/><path d="m15 15 6 6"/>',
    'refresh': '<path d="M20 7V2m0 5h-5M4 17v5m0-5h5M20 7a9 9 0 0 0-16 2M4 17a9 9 0 0 0 16-2"/>',
    'cube': '<path d="m12 2 9 5v10l-9 5-9-5V7l9-5Zm0 10v10M3 7l9 5 9-5M8 4l9 5"/>',
}


def icon(name, color, size=20):
    source = (f'<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" '
              f'fill="none" stroke="{color}" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round">'
              + ICON_PATHS[name] + '</svg>')
    # Render at 2x to keep strokes crisp on high DPI monitors.
    pixmap = QPixmap(size * 2, size * 2)
    pixmap.fill(Qt.transparent)
    renderer = QSvgRenderer(QByteArray(source.encode()))
    painter = QPainter(pixmap)
    renderer.render(painter)
    painter.end()
    pixmap.setDevicePixelRatio(2)
    return QIcon(pixmap)


class IconButton(QPushButton):
    def __init__(self, text, symbol, *, role=''):
        super().__init__(text)
        self.symbol = symbol
        if role:
            self.setObjectName(role)
        self.setCursor(Qt.PointingHandCursor)
        theme_manager().changed.connect(self.refresh_icon)
        self.refresh_icon()

    def refresh_icon(self):
        tokens = theme_manager().tokens
        size = max(18, round(tokens.get('font_size', 13) * 1.5))
        self.setIconSize(QSize(size, size))
        self.setIcon(icon(self.symbol, tokens['on_accent'] if self.objectName() == 'primary' else tokens['accent_ink'], size))


def title_block(title, subtitle='', *, large=False):
    widget = QWidget()
    layout = QVBoxLayout(widget)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(5)
    heading = QLabel(title)
    heading.setObjectName('title' if large else 'section')
    heading.setWordWrap(True)
    layout.addWidget(heading)
    if subtitle:
        hint = QLabel(subtitle)
        hint.setObjectName('muted')
        hint.setWordWrap(True)
        layout.addWidget(hint)
    return widget


def surface():
    frame = QFrame()
    frame.setObjectName('card')
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(18, 16, 18, 16)
    layout.setSpacing(12)
    return frame, layout


def scroll_page(content):
    scroll = QScrollArea()
    scroll.setFrameShape(QFrame.NoFrame)
    scroll.setWidgetResizable(True)
    scroll.setWidget(content)
    return scroll


class HelpDialog(QDialog):
    def __init__(self, parent):
        super().__init__(parent)
        self.resize(860, min(720, self.screen().availableGeometry().height() - 60))
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 18)
        layout.setSpacing(18)
        layout.addWidget(title_block(ui_text('使用说明'), ui_text('下载、预览与元件库导出'), large=True))
        self.browser = QTextBrowser()
        self.browser.setOpenExternalLinks(True)
        layout.addWidget(self.browser, 1)
        close = QPushButton(ui_text('关闭'))
        close.setObjectName('primary')
        close.clicked.connect(self.accept)
        layout.addWidget(close, 0, Qt.AlignRight)

    def setText(self, text):
        self.browser.setHtml(render(text))
