"""An integrated title bar with native Windows moving, sizing and taskbar behavior."""
import sys

from PySide6.QtCore import QEvent, QObject, QPoint, QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QFrame, QHBoxLayout, QSizePolicy

from app_theme import theme_manager
from i18n import text as ui_text
from localized_widgets import QMainWindow, QPushButton


class WindowButton(QPushButton):
    """Font-independent window glyphs that follow the current application theme."""
    def __init__(self, action, caption, parent):
        super().__init__('', parent)
        self.action = action
        self.setObjectName('windowControl')
        self.setAccessibleName(ui_text(caption))
        self.setToolTip(ui_text(caption))
        self.setFixedWidth(46)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Expanding)
        self.setMinimumHeight(44)
        theme_manager().changed.connect(self.update)

    def paintEvent(self, event):
        tokens = theme_manager().tokens
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        hot = self.underMouse() or self.isDown()
        if hot:
            background = ('#c42b1c' if self.isDown() else '#e81123') if self.action == 'close' else tokens['border' if self.isDown() else 'hover']
            painter.fillRect(self.rect(), QColor(background))
        color = '#ffffff' if hot and self.action == 'close' else tokens['text' if self.isEnabled() else 'disabled']
        painter.setPen(QPen(QColor(color), 1.2))
        x, y = self.width() / 2, self.height() / 2
        if self.action == 'minimize':
            painter.drawLine(QPoint(round(x - 5), round(y)), QPoint(round(x + 5), round(y)))
        elif self.action == 'close':
            painter.drawLine(QPoint(round(x - 5), round(y - 5)), QPoint(round(x + 5), round(y + 5)))
            painter.drawLine(QPoint(round(x + 5), round(y - 5)), QPoint(round(x - 5), round(y + 5)))
        elif self.action == 'restore':
            painter.drawRect(QRectF(x - 2, y - 5, 8, 8))
            painter.setBrush(QColor(tokens['border' if self.isDown() else 'hover'] if hot else tokens['bg']))
            painter.drawRect(QRectF(x - 5, y - 2, 8, 8))
        else:
            painter.drawRect(QRectF(x - 5, y - 5, 10, 10))
        if self.hasFocus():
            painter.setPen(QPen(QColor(tokens['accent_ink']), 1, Qt.DotLine))
            painter.setBrush(Qt.NoBrush)
            painter.drawRect(self.rect().adjusted(4, 4, -5, -5))


class TitleBar(QFrame):
    def __init__(self, owner, back, caption, account):
        super().__init__(owner)
        self.owner, self.caption = owner, caption
        self.setObjectName('titleBar')
        self.setMinimumHeight(52)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(24, 0, 0, 0)
        layout.setSpacing(8)
        layout.addWidget(back, 0, Qt.AlignVCenter)
        caption.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        caption.setMinimumWidth(40)
        caption.setAttribute(Qt.WA_TransparentForMouseEvents)
        layout.addWidget(caption, 1)
        layout.addWidget(account, 0, Qt.AlignVCenter)
        layout.addSpacing(12)
        controls = QHBoxLayout()
        controls.setContentsMargins(0, 0, 0, 0)
        controls.setSpacing(0)
        self.minimize_button = WindowButton('minimize', '最小化', self)
        self.maximize_button = WindowButton('maximize', '最大化', self)
        self.close_button = WindowButton('close', '关闭', self)
        for button in (self.minimize_button, self.maximize_button, self.close_button):
            controls.addWidget(button)
        layout.addLayout(controls)
        self.minimize_button.clicked.connect(owner.showMinimized)
        self.maximize_button.clicked.connect(owner.toggle_maximized)
        # Use the existing closeEvent path, including cancellation of workers.
        self.close_button.clicked.connect(owner.close)

    def is_drag_position(self, point):
        return self.isVisible() and self.owner.childAt(point) is self

    def sync_state(self):
        restored = self.owner.isMaximized() or self.owner.isFullScreen()
        button = self.maximize_button
        button.action = 'restore' if restored else 'maximize'
        caption = ui_text('还原' if restored else '最大化')
        button.setAccessibleName(caption)
        button.setToolTip(caption)
        button.update()

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton and self.owner.windowHandle():
            self.owner.windowHandle().startSystemMove()
            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.owner.toggle_maximized()
            event.accept()
        else:
            super().mouseDoubleClickEvent(event)


class FramelessMainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowFlag(Qt.FramelessWindowHint)
        self._native_frame = WindowsFrame(self) if sys.platform == 'win32' else None

    def toggle_maximized(self):
        if self.isMaximized() or self.isFullScreen():
            self.showNormal()
        else:
            self.showMaximized()

    def showEvent(self, event):
        if self._native_frame is not None:
            self._native_frame.install()
        super().showEvent(event)

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() == QEvent.WindowStateChange and hasattr(self, 'title_bar'):
            self.title_bar.sync_state()

    def nativeEvent(self, event_type, message):
        frame = getattr(self, '_native_frame', None)
        if frame is not None and event_type == b'windows_generic_MSG':
            result = frame.handle(message)
            if result is not None:
                return True, result
        return super().nativeEvent(event_type, message)


class WindowsFrame(QObject):
    """Keep native sizing/snap/system menu, while replacing only frame painting.

    Qt must regard the window as frameless so its client geometry agrees with
    WM_NCCALCSIZE. The native styles retain Windows' move/resize behavior.
    https://learn.microsoft.com/windows/win32/dwm/customframe
    """
    def __init__(self, window):
        super().__init__(window)
        import ctypes as c
        from ctypes import wintypes as wt
        self.c, self.wt, self.window = c, wt, window
        self.hwnd = None
        self.user = c.WinDLL('user32', use_last_error=True)
        self.dwm = c.WinDLL('dwmapi')
        signatures = {
            'GetWindowLongPtrW': ([wt.HWND, c.c_int], c.c_ssize_t),
            'SetWindowLongPtrW': ([wt.HWND, c.c_int, c.c_ssize_t], c.c_ssize_t),
            'SetWindowPos': ([wt.HWND, wt.HWND, c.c_int, c.c_int, c.c_int, c.c_int, wt.UINT], wt.BOOL),
            'GetWindowRect': ([wt.HWND, c.POINTER(wt.RECT)], wt.BOOL),
            'ScreenToClient': ([wt.HWND, c.POINTER(wt.POINT)], wt.BOOL),
            'IsZoomed': ([wt.HWND], wt.BOOL),
            'MonitorFromWindow': ([wt.HWND, wt.DWORD], wt.HANDLE),
            'GetMonitorInfoW': ([wt.HANDLE, c.c_void_p], wt.BOOL),
            'GetDpiForWindow': ([wt.HWND], wt.UINT),
            'GetSystemMetricsForDpi': ([c.c_int, wt.UINT], c.c_int),
        }
        for name, (arguments, result) in signatures.items():
            method = getattr(self.user, name)
            method.argtypes, method.restype = arguments, result
        self.dwm.DwmExtendFrameIntoClientArea.argtypes = [wt.HWND, c.c_void_p]
        self.dwm.DwmSetWindowAttribute.argtypes = [wt.HWND, wt.DWORD, c.c_void_p, wt.DWORD]

        class MonitorInfo(c.Structure):
            _fields_ = [('cbSize', wt.DWORD), ('rcMonitor', wt.RECT), ('rcWork', wt.RECT), ('dwFlags', wt.DWORD)]
        self.MonitorInfo = MonitorInfo
        theme_manager().changed.connect(self.refresh_theme)

    def install(self):
        hwnd = int(self.window.winId())
        if hwnd == self.hwnd:
            return
        self.hwnd = hwnd
        # WS_CAPTION | WS_THICKFRAME | WS_SYSMENU | WS_MINIMIZEBOX | WS_MAXIMIZEBOX.
        style = self.user.GetWindowLongPtrW(hwnd, -16)
        self.user.SetWindowLongPtrW(hwnd, -16, style | 0x00CF0000)
        self.user.SetWindowPos(hwnd, None, 0, 0, 0, 0, 0x0037)
        margins = (self.c.c_int * 4)(1, 1, 1, 1)
        self.dwm.DwmExtendFrameIntoClientArea(hwnd, self.c.byref(margins))
        self.refresh_theme()

    def refresh_theme(self):
        if self.hwnd is not None:
            dark = self.c.c_int(theme_manager().dark)
            self.dwm.DwmSetWindowAttribute(self.hwnd, 20, self.c.byref(dark), self.c.sizeof(dark))

    def handle(self, address):
        c, wt, window = self.c, self.wt, self.window
        message = wt.MSG.from_address(int(address))
        if message.message == 0x0083:  # WM_NCCALCSIZE
            if self.user.IsZoomed(message.hWnd) and not window.isFullScreen():
                # Maximized native frames extend past rcWork. Clip the client
                # area to this monitor's work area, including non-bottom taskbars.
                info = self.MonitorInfo()
                info.cbSize = c.sizeof(info)
                monitor = self.user.MonitorFromWindow(message.hWnd, 2)
                if self.user.GetMonitorInfoW(monitor, c.byref(info)):
                    rect = wt.RECT.from_address(message.lParam)
                    rect.left, rect.top = max(rect.left, info.rcWork.left), max(rect.top, info.rcWork.top)
                    rect.right, rect.bottom = min(rect.right, info.rcWork.right), min(rect.bottom, info.rcWork.bottom)
            return 0
        if message.message != 0x0084:  # WM_NCHITTEST
            return None
        # lParam contains signed physical screen coordinates (negative on
        # monitors to the left/above the primary). Qt widgets use logical pixels.
        x, y = c.c_short(message.lParam & 0xffff).value, c.c_short((message.lParam >> 16) & 0xffff).value
        rect = wt.RECT()
        if not self.user.GetWindowRect(message.hWnd, c.byref(rect)):
            return None
        if not window.isMaximized() and not window.isFullScreen():
            dpi = self.user.GetDpiForWindow(message.hWnd)
            border = self.user.GetSystemMetricsForDpi(32, dpi) + self.user.GetSystemMetricsForDpi(92, dpi)
            left, right = x < rect.left + border, x >= rect.right - border
            top, bottom = y < rect.top + border, y >= rect.bottom - border
            if top:
                return 13 if left else 14 if right else 12  # HTTOPLEFT/RIGHT/TOP
            if bottom:
                return 16 if left else 17 if right else 15
            if left or right:
                return 10 if left else 11
        point = wt.POINT(x, y)
        if self.user.ScreenToClient(message.hWnd, c.byref(point)):
            ratio = window.devicePixelRatioF()
            local = QPoint(round(point.x / ratio), round(point.y / ratio))
            bar = getattr(window, 'title_bar', None)
            if bar is not None and bar.is_drag_position(local):
                return 2  # HTCAPTION: native drag, double click and system menu.
        return 1  # HTCLIENT: account/navigation/window buttons keep normal clicks.
