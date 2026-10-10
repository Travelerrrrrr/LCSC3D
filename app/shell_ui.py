"""Single-window pages, anchored choices and in-app notifications."""
from PySide6 import QtWidgets as W
from PySide6.QtCore import Qt, QEvent, QPoint, QTimer, QObject, Signal, QPropertyAnimation, QEasingCurve
from PySide6.QtGui import QAction, QColor
from shiboken6 import isValid
from i18n import render, text as ui_text


def page_host(widget):
    while widget is not None:
        host = getattr(widget, '_page_host', None)
        if host is not None:
            return host
        widget = widget.parentWidget()
    return None


class PageMixin:
    """Keep QDialog's signals/validation while displaying it as a child page."""
    def initialize_page(self, owner):
        self._page_owner = owner
        self._page_host = page_host(owner)
        self._page_finished = False
        if self._page_host is not None:
            self.setWindowFlags(Qt.Widget)
            self.setWindowModality(Qt.NonModal)

    def setWindowModality(self, modality):
        super().setWindowModality(Qt.NonModal if getattr(self, '_page_host', None) else modality)

    def show(self):
        if self._page_host is None:
            super().show()
        else:
            self._page_finished = False
            self._page_host.present(self)

    def open(self):
        self.show()

    def exec(self):
        if self._page_host is not None:
            raise RuntimeError('Embedded pages use show() and finished/accepted callbacks')
        return super().exec()

    def done(self, result):
        self._page_finished = True
        # Restore the preceding page before invoking accepted callbacks, which
        # may navigate to a new destination themselves.
        if self._page_host is not None:
            self._page_host.finish(self)
        super().done(result)

    def close(self):
        accepted = super().close()
        if accepted and self._page_host is not None:
            self._page_finished = True
            self._page_host.finish(self)
        return accepted

    def raise_(self):
        if self._page_host is not None:
            self._page_host.present(self)
        else:
            super().raise_()

    def activateWindow(self):
        if self._page_host is None:
            super().activateWindow()


class PageDialog(PageMixin, W.QDialog):
    def __init__(self, parent=None, flags=Qt.WindowFlags()):
        super().__init__(parent, flags)
        self.initialize_page(parent)


class PageHost(W.QStackedWidget):
    changed = Signal(object)

    def __init__(self, owner, workspace):
        super().__init__(owner)
        self.owner, self.workspace = owner, workspace
        self.frames = {}
        self.history = []
        self.addWidget(workspace)
        self.currentChanged.connect(lambda _: self.changed.emit(self.current_page()))

    def current_page(self):
        frame = self.currentWidget()
        return getattr(frame, '_content_page', frame)

    def present(self, page, *, navigation=False):
        if page is self.workspace:
            frame = page
        else:
            frame = self.frames.get(page)
            if frame is None:
                page.setWindowFlags(Qt.Widget)
                page.setWindowModality(Qt.NonModal)
                page.setMinimumSize(0, 0)
                frame = W.QScrollArea()
                frame.setFrameShape(W.QFrame.NoFrame)
                frame.setWidgetResizable(True)
                frame.setWidget(page)
                frame._content_page = page
                self.frames[page] = frame
                self.addWidget(frame)
                page.destroyed.connect(lambda _=None, f=frame: self.forget(f))
            W.QWidget.show(page)
        current = self.currentWidget()
        if navigation:
            self.history = [] if frame is self.workspace else [self.workspace]
        elif frame is not current:
            # Reusing a page should still return to its latest caller. Keep
            # each frame only once so repeated sidebar visits cannot cycle.
            self.history = [p for p in self.history if p is not frame and p is not current]
            if current is not None:
                self.history.append(current)
        self.setCurrentWidget(frame)
        self.changed.emit(page)

    def finish(self, page):
        for child in list(self.frames):
            if not isValid(child) or child is page or child._page_finished:
                continue
            owner = getattr(child, '_page_owner', None)
            while owner is not None and owner is not self.owner:
                if owner is page:
                    child.reject()
                    break
                owner = getattr(owner, '_page_owner', None)
        frame = self.frames.get(page)
        self.history = [p for p in self.history if p is not frame]
        if self.currentWidget() is frame:
            target = self.history.pop() if self.history else self.workspace
            self.setCurrentWidget(target)
            content = getattr(target, '_content_page', None)
            if content is not None:
                W.QWidget.show(content)
        self.changed.emit(self.current_page())

    def forget(self, frame):
        if not isValid(self):
            return
        self.history = [p for p in self.history if p is not frame]
        for page, candidate in list(self.frames.items()):
            if candidate is frame:
                del self.frames[page]
                # Scroll areas own the C++ widgets, so mirror logical ownership
                # when a form is recreated. An update still cancelling its
                # worker stays under the main window until it becomes idle.
                for child in list(self.frames):
                    if isValid(child) and getattr(child, '_page_owner', None) is page:
                        if getattr(child, 'worker', None) is not None:
                            child._page_owner = self.owner
                        else:
                            child.deleteLater()
        if isValid(frame):
            self.removeWidget(frame)
            frame.deleteLater()

    def back(self):
        current = self.current_page()
        if current is not self.workspace:
            current.reject()


class AnchoredPanel(W.QFrame):
    """A dropdown that remains a child of the application, never a Qt popup."""
    def __init__(self, anchor):
        super().__init__(anchor.window(), Qt.Widget)
        self.anchor = anchor
        self.setObjectName('anchoredPanel')
        W.QApplication.instance().installEventFilter(self)
        self.hide()

    def place(self, height):
        self.preferred_height = height
        root = self.parentWidget()
        position = self.anchor.mapTo(root, QPoint(0, self.anchor.height() + 4))
        self.setGeometry(position.x(), position.y(), self.anchor.width(),
                         min(height, max(24, root.height() - position.y() - 8)))

    def eventFilter(self, watched, event):
        if self.isVisible():
            if event.type() == QEvent.MouseButtonPress and hasattr(event, 'globalPosition'):
                point = event.globalPosition().toPoint()
                if (not self.rect().contains(self.mapFromGlobal(point))
                        and not self.anchor.rect().contains(self.anchor.mapFromGlobal(point))):
                    self.hide()
            elif event.type() == QEvent.KeyPress and event.key() == Qt.Key_Escape:
                self.hide()
                self.anchor.setFocus()
                return True
            elif watched is self.parentWidget() and event.type() in (QEvent.Resize, QEvent.Hide):
                self.hide()
            elif watched is self.anchor and event.type() == QEvent.Hide:
                self.hide()
            elif watched is self.anchor and event.type() in (QEvent.Resize, QEvent.Move):
                self.place(self.preferred_height)
        return super().eventFilter(watched, event)


class AccountPanel(AnchoredPanel):
    def __init__(self, anchor):
        super().__init__(anchor)
        self._actions = []
        self.buttons = []
        self.rows = W.QVBoxLayout(self)
        self.rows.setContentsMargins(6, 6, 6, 6)
        self.rows.setSpacing(2)

    def addAction(self, text, callback=None):
        from localized_widgets import QPushButton
        action = QAction(render(text), self)
        button = QPushButton(text)
        button.setProperty('variant', 'text')
        button.setSizePolicy(W.QSizePolicy.Ignored, W.QSizePolicy.Fixed)
        button.setToolTip(text)
        action.changed.connect(lambda: button.setEnabled(action.isEnabled()))
        button.clicked.connect(lambda: (self.hide(), action.trigger()))
        if callback:
            action.triggered.connect(callback)
        self.rows.addWidget(button)
        self._actions.append(action)
        self.buttons.append(button)
        return action

    def actions(self):
        return self._actions

    def popup(self, unused=None):
        self.ensurePolished()
        self.place(self.sizeHint().height())
        self.show()
        self.raise_()


class ComboDropdown(QObject):
    """Shared popup behavior, also used by Qt's embedded file chooser fields."""
    def __init__(self, combo, *, intercept=False):
        super().__init__(combo)
        self.combo = combo
        self.frame = None
        self.view = None
        if intercept:
            combo.installEventFilter(self)

    def show(self):
        combo = self.combo
        if not combo.count():
            return
        if self.frame is not None and self.frame.isVisible():
            self.hide()
            return
        if self.frame is None:
            self.frame = AnchoredPanel(combo)
            combo.destroyed.connect(self.frame.deleteLater)
            layout = W.QVBoxLayout(self.frame)
            layout.setContentsMargins(1, 1, 1, 1)
            self.view = W.QListView(self.frame)
            self.view.setObjectName('comboChoices')
            self.view.setEditTriggers(W.QAbstractItemView.NoEditTriggers)
            self.view.setVerticalScrollMode(W.QAbstractItemView.ScrollPerPixel)
            self.view.setUniformItemSizes(True)
            self.view.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
            self.view.clicked.connect(self.choose_index)
            self.view.installEventFilter(self)
            layout.addWidget(self.view)
        root = combo.window()
        if self.frame.parentWidget() is not root:
            self.frame.setParent(root, Qt.Widget)
        # Bring a field near the bottom of a scrollable page up far enough to
        # fit its list, instead of centering the popup on the selected item.
        ancestor = combo.parentWidget()
        while ancestor is not None:
            if isinstance(ancestor, W.QScrollArea):
                ancestor.ensureWidgetVisible(combo, 0, 240)
                break
            ancestor = ancestor.parentWidget()
        view = self.view
        view.setModel(combo.model())
        view.setModelColumn(combo.modelColumn())
        view.setRootIndex(combo.rootModelIndex())
        row_height = max(24, combo.fontMetrics().height() + 8)
        view.setStyleSheet(f'QListView {{padding:0;margin:0;border:0;}} '
                          f'QListView::item {{min-height:{row_height}px;padding:0 8px;margin:0;}}')
        index = combo.model().index(max(0, combo.currentIndex()), combo.modelColumn(), combo.rootModelIndex())
        view.setCurrentIndex(index)
        view.doItemsLayout()
        row_height = max(row_height, view.sizeHintForRow(0))
        self.frame.place(min(8, combo.maxVisibleItems(), combo.count()) * row_height + 4)
        self.frame.show()
        self.frame.raise_()
        view.scrollTo(index, W.QAbstractItemView.PositionAtCenter)
        view.setFocus(Qt.PopupFocusReason)

    def choose_index(self, index):
        if index.isValid() and index.flags() & Qt.ItemIsEnabled:
            self.combo.setCurrentIndex(index.row())
            self.combo.activated.emit(index.row())
            self.combo.textActivated.emit(self.combo.currentText())
            self.hide()
            self.combo.setFocus()

    def hide(self):
        if self.frame is not None:
            self.frame.hide()

    def eventFilter(self, watched, event):
        if watched is self.combo:
            if event.type() == QEvent.MouseButtonPress and event.button() == Qt.LeftButton:
                self.show()
                return True
            if event.type() == QEvent.KeyPress and (event.key() in (Qt.Key_Space, Qt.Key_F4)
                    or event.key() == Qt.Key_Down and event.modifiers() & Qt.AltModifier):
                self.show()
                return True
        if watched is self.view and event.type() == QEvent.KeyPress:
            if event.key() in (Qt.Key_Return, Qt.Key_Enter):
                self.choose_index(self.view.currentIndex())
                return True
            if event.key() == Qt.Key_Escape:
                self.hide()
                self.combo.setFocus()
                return True
        return super().eventFilter(watched, event)


class AnchoredComboBox(W.QComboBox):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.dropdown = ComboDropdown(self)
        self.setMaxVisibleItems(8)

    @property
    def popup_frame(self):
        return self.dropdown.frame

    def view(self):
        return self.dropdown.view or super().view()

    def showPopup(self):
        self.dropdown.show()

    def hidePopup(self):
        self.dropdown.hide()
        super().hidePopup()

    def hideEvent(self, event):
        self.hidePopup()
        super().hideEvent(event)


class Toast(W.QFrame):
    def __init__(self, center, title, message, severity, actions):
        from localized_widgets import QLabel, QPushButton
        super().__init__(center.parent(), Qt.Widget)
        self.center, self.severity = center, severity
        self.setObjectName('notification')
        self.setProperty('severity', severity)
        self.setWindowTitle(render(title))
        layout = W.QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(10)
        heading = W.QHBoxLayout()
        title_label = QLabel(title)
        title_label.setObjectName('section')
        title_label.setWordWrap(True)
        heading.addWidget(title_label, 1)
        close = QPushButton('×')
        close.setAccessibleName(ui_text('关闭'))
        close.setFixedWidth(32)
        close.setProperty('variant', 'text')
        close.clicked.connect(self.close)
        heading.addWidget(close, 0, Qt.AlignTop)
        layout.addLayout(heading)
        self.message_label = QLabel(message)
        self.message_label.setTextFormat(Qt.PlainText)
        self.message_label.setWordWrap(True)
        self.message_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(self.message_label)
        self.action_buttons = []
        if actions:
            buttons = W.QHBoxLayout()
            buttons.addStretch()
            for text, callback in actions:
                button = QPushButton(text)
                button.clicked.connect(lambda checked=False, fn=callback: self.run_action(fn))
                buttons.addWidget(button)
                self.action_buttons.append(button)
            layout.addLayout(buttons)
        self.animation = QPropertyAnimation(self, b'pos', self)
        self.animation.setDuration(240)
        self.animation.setEasingCurve(QEasingCurve.OutCubic)
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self.close)

    def text(self):
        return self.message_label.text()

    def icon(self):
        return W.QMessageBox.Warning if self.severity in ('warning', 'error') else W.QMessageBox.Information

    def run_action(self, callback):
        self.close()
        if callback is not None:
            callback()

    def accept(self):
        self.close()

    def closeEvent(self, event):
        self.timer.stop()
        self.animation.stop()
        super().closeEvent(event)
        self.center.remove(self)
        self.deleteLater()


class NotificationCenter(QObject):
    def __init__(self, parent):
        super().__init__(parent)
        self.toasts = []
        parent.installEventFilter(self)

    def notify(self, title, message, *, severity='info', actions=(), persistent=False):
        if len(self.toasts) >= 3:
            self.toasts[0].close()
        toast = Toast(self, title, message, severity, actions)
        self.toasts.append(toast)
        self.reflow()
        end = toast.pos()
        toast.move(self.parent().width(), end.y())
        toast.show()
        toast.raise_()
        toast.animation.setStartValue(toast.pos())
        toast.animation.setEndValue(end)
        toast.animation.start()
        if not persistent:
            toast.timer.start(8000)
        return toast

    def remove(self, toast):
        if toast in self.toasts:
            self.toasts.remove(toast)
            self.reflow()

    def reflow(self):
        root = self.parent()
        y = 16
        for toast in self.toasts:
            toast.setFixedWidth(min(440, max(220, root.width() - 32)))
            toast.adjustSize()
            toast.move(root.width() - toast.width() - 16, y)
            y += toast.height() + 12

    def eventFilter(self, watched, event):
        if event.type() == QEvent.Resize:
            for toast in self.toasts:
                toast.animation.stop()
            self.reflow()
        return False


def notify(parent, title, message, **options):
    host = page_host(parent)
    root = host.owner if host is not None else parent.window()
    if not hasattr(root, 'notifications'):
        root.notifications = NotificationCenter(root)
    toast = root.notifications.notify(title, message, **options)
    parent.destroyed.connect(toast.close)
    if options.get('actions') and isinstance(parent, PageMixin):
        parent.finished.connect(toast.close)
    return toast


class ColorPage(PageMixin, W.QColorDialog):
    def __init__(self, color, parent, title):
        super().__init__(color, parent)
        self.setOption(W.QColorDialog.DontUseNativeDialog)
        self.setOption(W.QColorDialog.NoEyeDropperButton)
        self.initialize_page(parent)
        self.setWindowTitle(render(title))


def choose_color(parent, color, title, callback):
    page = ColorPage(color, parent, title)
    page.accepted.connect(lambda: callback(page.currentColor()))
    page.finished.connect(page.deleteLater)
    page.show()
    return page


class FilePage(PageMixin, W.QFileDialog):
    def __init__(self, parent, title, path, filter, directory):
        super().__init__(parent, render(title), path, render(filter))
        self.setOption(W.QFileDialog.DontUseNativeDialog)
        self.setFileMode(W.QFileDialog.Directory if directory else W.QFileDialog.ExistingFile)
        self.setOption(W.QFileDialog.ShowDirsOnly, directory)
        self.setOption(W.QFileDialog.ReadOnly)
        self.initialize_page(parent)
        self.dropdowns = [ComboDropdown(combo, intercept=True) for combo in self.findChildren(W.QComboBox)]
        for view in self.findChildren(W.QAbstractItemView):
            view.setContextMenuPolicy(Qt.NoContextMenu)
        for edit in self.findChildren(W.QLineEdit):
            edit.setCompleter(None)

    def accept(self):
        from pathlib import Path
        paths = self.selectedFiles()
        try:
            valid = bool(paths) and (Path(paths[0]).is_dir() if self.fileMode() == W.QFileDialog.Directory else Path(paths[0]).is_file())
        except OSError:
            valid = False
        if not valid:
            notify(self, ui_text('请选择有效路径'), ui_text('所选文件或文件夹不存在，请重新选择。'), severity='warning')
            return
        super().accept()


def choose_path(parent, title, path, callback, *, filter='', directory=False):
    page = FilePage(parent, title, path, filter, directory)
    page.accepted.connect(lambda: callback(page.selectedFiles()[0]) if page.selectedFiles() else None)
    page.finished.connect(page.deleteLater)
    page.show()
    return page
