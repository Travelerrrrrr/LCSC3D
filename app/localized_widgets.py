"""Qt controls that retain source messages for lossless live language switching.

Editable contents, item data, product metadata and file paths stay untouched.
Only explicit Message values and exact catalog messages are rendered.
"""
from PySide6 import QtWidgets as W
from PySide6.QtCore import QSignalBlocker
from i18n import register, render
from shell_ui import PageDialog, AnchoredComboBox


class Localized:
    initial_property = None

    def __init__(self, *args, **kwargs):
        self._messages = {}
        super().__init__(*args, **kwargs)
        register(self)
        if self.initial_property and args and isinstance(args[0], str):
            self._set(self.initial_property, args[0])

    def _set(self, method, value):
        self._messages[method] = value
        getattr(super(), method)(render(value))

    def source_text(self):
        return self._messages.get('setText', '')

    def retranslate(self):
        for method, value in self._messages.items():
            getattr(super(), method)(render(value))

    def setWindowTitle(self, value):
        self._set('setWindowTitle', value)

    def setToolTip(self, value):
        self._set('setToolTip', value)

    def setAccessibleName(self, value):
        self._set('setAccessibleName', value)

    def setPlaceholderText(self, value):
        self._set('setPlaceholderText', value)


class TextControl(Localized):
    initial_property = 'setText'

    def setText(self, value):
        self._set('setText', value)


class QLabel(TextControl, W.QLabel):
    def setPixmap(self, pixmap):
        self._messages.pop('setText', None)
        super().setPixmap(pixmap)

    def clear(self):
        self._messages.pop('setText', None)
        super().clear()


class QPushButton(TextControl, W.QPushButton):
    pass


class QCheckBox(TextControl, W.QCheckBox):
    pass


class QGroupBox(Localized, W.QGroupBox):
    initial_property = 'setTitle'

    def setTitle(self, value):
        self._set('setTitle', value)


class QDialog(Localized, PageDialog):
    pass


class QMainWindow(Localized, W.QMainWindow):
    pass


class QLineEdit(Localized, W.QLineEdit):
    pass


class QPlainTextEdit(Localized, W.QPlainTextEdit):
    pass


class QTableWidgetItem(TextControl, W.QTableWidgetItem):
    def retranslate(self):
        table = self.tableWidget()
        if table is not None:
            with QSignalBlocker(table):
                super().retranslate()
        else:
            super().retranslate()


class QTableWidget(W.QTableWidget):
    def setHorizontalHeaderLabels(self, labels):
        for index, title in enumerate(labels):
            self.setHorizontalHeaderItem(index, QTableWidgetItem(title))


class QComboBox(Localized, AnchoredComboBox):
    def __init__(self, *args, **kwargs):
        self._item_messages = []
        super().__init__(*args, **kwargs)

    def addItem(self, *args):
        index = 0 if isinstance(args[0], str) else 1
        title = args[index]
        self._item_messages.append(title)
        values = list(args)
        values[index] = render(title)
        super().addItem(*values)

    def addItems(self, values):
        for value in values:
            self.addItem(value)

    def setItemText(self, index, text):
        self._item_messages[index] = text
        super().setItemText(index, render(text))

    def clear(self):
        self._item_messages.clear()
        super().clear()

    def retranslate(self):
        super().retranslate()
        with QSignalBlocker(self):
            for index, text in enumerate(self._item_messages):
                super().setItemText(index, render(text))


class QTabWidget(Localized, W.QTabWidget):
    def __init__(self, *args, **kwargs):
        self._tab_messages = []
        super().__init__(*args, **kwargs)
        self.tabBar().setDrawBase(False)

    def setDocumentMode(self, enabled):
        super().setDocumentMode(enabled)
        self.tabBar().setDrawBase(False)

    def addTab(self, widget, title):
        self._tab_messages.append(title)
        return super().addTab(widget, render(title))

    def retranslate(self):
        super().retranslate()
        for index, title in enumerate(self._tab_messages):
            super().setTabText(index, render(title))


class QFormLayout(W.QFormLayout):
    def addRow(self, *args):
        if args and isinstance(args[0], str):
            args = (QLabel(args[0]), *args[1:])
        return super().addRow(*args)


class QMenu(W.QMenu):
    def addAction(self, *args):
        if args and isinstance(args[0], str):
            args = (render(args[0]), *args[1:])
        return super().addAction(*args)


class QMessageBox(TextControl, W.QMessageBox):
    initial_property = None

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if len(args) >= 3 and isinstance(args[1], str) and isinstance(args[2], str):
            self.setWindowTitle(args[1])
            self.setText(args[2])
    @staticmethod
    def question(parent, title, text, *args):
        return W.QMessageBox.question(parent, render(title), render(text), *args)

    @staticmethod
    def information(parent, title, text, *args):
        return W.QMessageBox.information(parent, render(title), render(text), *args)

    @staticmethod
    def warning(parent, title, text, *args):
        return W.QMessageBox.warning(parent, render(title), render(text), *args)

    @staticmethod
    def critical(parent, title, text, *args):
        return W.QMessageBox.critical(parent, render(title), render(text), *args)


class QFileDialog(W.QFileDialog):
    @staticmethod
    def getExistingDirectory(parent, caption, directory='', *args):
        return W.QFileDialog.getExistingDirectory(parent, render(caption), directory, *args)

    @staticmethod
    def getOpenFileName(parent, caption, directory='', filter='', *args):
        return W.QFileDialog.getOpenFileName(parent, render(caption), directory, render(filter), *args)
