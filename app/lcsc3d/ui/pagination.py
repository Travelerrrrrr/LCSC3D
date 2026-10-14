"""Compact numbered navigation shared by catalog and order lists."""
from PySide6.QtCore import Qt, Signal, QEvent, QTimer, QSize
from PySide6.QtWidgets import QWidget, QHBoxLayout, QSizePolicy, QLayout

from lcsc3d.core.i18n import text as tr, message as msg
from lcsc3d.core.app_logging import log_event
from lcsc3d.ui.localized_widgets import QLabel, QPushButton
from lcsc3d.ui.ui_components import IconButton


def visible_pages(current, total, slots=8):
    """Bound the number of controls independently of the result count."""
    slots = max(5, slots)
    if total <= slots:
        return list(range(1, total + 1))
    edge = slots - 2
    if current < edge:
        numbers = [*range(1, edge + 1), total]
    elif current > total - edge + 1:
        numbers = [1, *range(total - edge + 1, total + 1)]
    else:
        count = slots - 4
        start = current - (count - 1) // 2
        numbers = [1, *range(start, start + count), total]
    result = []
    for page in numbers:
        if result and page - result[-1] > 1:
            result.append(page - 1 if page - result[-1] == 2 else None)
        result.append(page)
    return result


class PaginationBar(QWidget):
    page_requested = Signal(int)

    def __init__(self, *, view='catalog'):
        super().__init__()
        self.view = view
        self.current = self.total = 0
        self.busy = False
        self.number_buttons = {}
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.layout_row = QHBoxLayout(self)
        self.layout_row.setSizeConstraint(QLayout.SetNoConstraint)
        self.layout_row.setContentsMargins(0, 2, 0, 2)
        self.layout_row.setSpacing(6)
        self.layout_row.addStretch()
        self.previous = IconButton('', 'chevron-left', role='pageButton')
        self.previous.setAccessibleName(tr('上一页'))
        self.previous.setToolTip(tr('上一页'))
        self.previous.clicked.connect(lambda: self.request(self.current - 1))
        self.layout_row.addWidget(self.previous)
        self.numbers = QWidget()
        self.numbers_layout = QHBoxLayout(self.numbers)
        self.numbers_layout.setContentsMargins(0, 0, 0, 0)
        self.numbers_layout.setSpacing(6)
        self.layout_row.addWidget(self.numbers)
        self.next = IconButton('', 'chevron-right', role='pageButton')
        self.next.setAccessibleName(tr('下一页'))
        self.next.setToolTip(tr('下一页'))
        self.next.clicked.connect(lambda: self.request(self.current + 1))
        self.layout_row.addWidget(self.next)
        self.layout_row.addStretch()
        self.reflow = QTimer(self)
        self.reflow.setSingleShot(True)
        self.reflow.timeout.connect(self.rebuild)
        self.set_state(0, 0)

    def set_state(self, current, total, busy=False):
        self.total = max(1, total) if current > 0 else 0
        self.current = min(max(0, current), self.total)
        self.busy = busy
        self.rebuild()

    def request(self, page):
        for number, button in self.number_buttons.items():
            button.setChecked(number == self.current)
        self.reflow.start(0)
        if self.busy or not 1 <= page <= self.total or page == self.current:
            return
        log_event('INFO', 'pagination.requested', view=self.view, page=page,
                  previous_page=self.current, pages=self.total)
        self.page_requested.emit(page)

    def minimumSizeHint(self):
        return QSize(0, max(34, self.fontMetrics().height() + 16) + 4)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if event.size().width() != event.oldSize().width():
            self.reflow.start(0)

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() in (QEvent.FontChange, QEvent.ApplicationFontChange, QEvent.StyleChange) and hasattr(self, 'reflow'):
            self.reflow.start(0)

    def rebuild(self):
        edge = max(34, self.fontMetrics().height() + 16)
        width = max(edge, self.fontMetrics().horizontalAdvance(str(self.total)) + 18)
        arrows = int(self.current > 1) + int(0 < self.current < self.total)
        slots = max(5, min(8, (max(self.width(), 240) - arrows * (edge + 6)) // (width + 6)))
        tokens = visible_pages(self.current, self.total, slots)
        if self.total and self.width() < len(tokens) * (width + 6) + arrows * (edge + 6):
            tokens = [self.current]
        signature = (tuple(tokens), self.current, self.busy, edge, width)
        self.previous.setVisible(self.current > 1)
        self.next.setVisible(0 < self.current < self.total)
        self.previous.setEnabled(not self.busy and self.current > 1)
        self.next.setEnabled(not self.busy and 0 < self.current < self.total)
        for button in (self.previous, self.next):
            button.ensurePolished()
            button.setFixedSize(edge, edge)
        for button in self.number_buttons.values():
            button.ensurePolished()
            button.setFixedSize(width, edge)
        if getattr(self, '_signature', None) == signature:
            return
        self._signature = signature
        while self.numbers_layout.count():
            widget = self.numbers_layout.takeAt(0).widget()
            widget.hide()
            widget.deleteLater()
        self.number_buttons = {}
        for page in tokens:
            if page is None:
                widget = QLabel('…')
                widget.setObjectName('muted')
                widget.setAlignment(Qt.AlignCenter)
            else:
                widget = QPushButton(str(page))
                widget.setObjectName('pageButton')
                widget.setCheckable(True)
                widget.setChecked(page == self.current)
                widget.setEnabled(not self.busy)
                widget.setCursor(Qt.PointingHandCursor)
                widget.setAutoDefault(False)
                widget.setAccessibleName(msg('第 {0} 页', page))
                widget.setToolTip(msg('跳转到第 {0} 页', page))
                widget.clicked.connect(lambda checked=False, number=page: self.request(number))
                self.number_buttons[page] = widget
            self.numbers_layout.addWidget(widget)
            widget.ensurePolished()
            widget.setFixedSize(width, edge)
