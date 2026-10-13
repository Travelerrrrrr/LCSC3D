"""Resizable table columns with a flexible default and explicit user overrides."""
from PySide6.QtCore import QEvent, QTimer
from PySide6.QtWidgets import QHeaderView
from lcsc3d.ui.localized_widgets import QTableWidget
from lcsc3d.core.i18n import text as tr


class FlexibleTable(QTableWidget):
    def __init__(self, *args, stretch_column=2):
        super().__init__(*args)
        self.stretch_column = stretch_column
        self.manual_width = False
        self.fitting = False
        header = self.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.Interactive)
        header.setMinimumSectionSize(44)
        header.setToolTip(str(tr('拖动列头分隔线调整宽度；双击分隔线适应内容')))
        self.column_timer = QTimer(self)
        self.column_timer.setSingleShot(True)
        self.column_timer.timeout.connect(self.fit_model_column)
        header.sectionResized.connect(self.column_resized)
        header.sectionHandleDoubleClicked.connect(self.fit_contents)

    def column_resized(self, column, *_):
        if self.fitting:
            return
        if column == self.stretch_column:
            self.manual_width = True
        else:
            self.column_timer.start(0)

    def fit_contents(self, column):
        self.resizeColumnToContents(column)

    def viewportEvent(self, event):
        if event.type() == QEvent.Resize and hasattr(self, 'column_timer'):
            self.column_timer.start(0)
        return super().viewportEvent(event)

    def fit_model_column(self):
        if self.manual_width:
            return
        other = sum(self.columnWidth(col) for col in range(self.columnCount())
                    if col != self.stretch_column and not self.isColumnHidden(col))
        self.fitting = True
        self.setColumnWidth(self.stretch_column, max(150, self.viewport().width() - other))
        self.fitting = False
