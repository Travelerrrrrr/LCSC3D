"""Source-language messages with arguments kept separate from translated UI text."""
import re
from string import Formatter
import weakref
from pathlib import Path
import sys

from translations_en import ENGLISH

_language = 'zh_CN'
_bindings = weakref.WeakValueDictionary()


def _patterns():
    result = []
    for source, target in ENGLISH.items():
        parts = list(Formatter().parse(source))
        if not any(field is not None for _, field, _, _ in parts):
            continue
        pattern, fields = '', []
        for literal, field, _, _ in parts:
            pattern += re.escape(literal)
            if field is not None:
                pattern += '(.*?)'
                fields.append(field)
        result.append((re.compile(pattern, re.DOTALL), fields, target))
    return result


_PATTERNS = _patterns()


class Message(str):
    """Acts as the original string in domain logic; renders only at the UI boundary."""
    def __new__(cls, source, args=(), fragments=None):
        original = ''.join(map(str, fragments)) if fragments is not None else source.format(*args) if args else source
        value = super().__new__(cls, original)
        value.source, value.args, value.fragments = source, args, fragments
        return value

    def render(self):
        if self.fragments is not None:
            return ''.join(render(part) if isinstance(part, Message) else str(part) for part in self.fragments)
        template = ENGLISH.get(self.source, self.source) if _language == 'en_US' else self.source
        if not self.args:
            return render(str(self.source))
        return template.format(*(render(arg) if isinstance(arg, Message) else arg for arg in self.args)) if self.args else template

    def __add__(self, other):
        return Message('', fragments=(self, other)) if isinstance(other, str) else NotImplemented

    def __radd__(self, other):
        return Message('', fragments=(other, self)) if isinstance(other, str) else NotImplemented

    def join(self, values):
        parts = []
        for item in values:
            if parts:
                parts.append(self)
            parts.append(item)
        return Message('', fragments=tuple(parts))


def text(source):
    return Message(source)


def message(source, *args):
    return Message(source, args)


def render(value):
    if isinstance(value, Message):
        return value.render()
    # Strings crossing Qt's str signals lose their Message subtype. Match whole
    # catalog messages/templates, never replace words inside user data or paths.
    if _language != 'en_US' or not isinstance(value, str):
        return value
    if value in ENGLISH:
        return ENGLISH[value]
    for pattern, fields, target in _PATTERNS:
        match = pattern.fullmatch(value)
        if match:
            values = dict(zip(fields, match.groups()))
            return ''.join(literal + (values[field] if field is not None else '')
                           for literal, field, _, _ in Formatter().parse(target))
    return value


def register(widget):
    _bindings[id(widget)] = widget


def set_language(language):
    global _language
    language = language if language in ('zh_CN', 'en_US') else 'zh_CN'
    from PySide6.QtCore import QLocale, QTranslator, QLibraryInfo
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance()
    if app is not None and getattr(app, '_qt_ui_language', None) != language:
        QLocale.setDefault(QLocale(language))
        old = getattr(app, '_qt_ui_translator', None)
        if old is not None:
            app.removeTranslator(old)
            old.deleteLater()
        app._qt_ui_translator = None
        if language == 'zh_CN':
            translator = QTranslator(app)
            bundled = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent)) / 'assets' / 'qtbase_zh_CN.qm'
            path = bundled if bundled.is_file() else Path(QLibraryInfo.path(QLibraryInfo.TranslationsPath)) / 'qtbase_zh_CN.qm'
            if translator.load(str(path)):
                app.installTranslator(translator)
                app._qt_ui_translator = translator
        app._qt_ui_language = language
    if _language == language:
        return
    _language = language
    from shiboken6 import isValid
    for widget in list(_bindings.values()):
        if isValid(widget):
            widget.retranslate()


def language():
    return _language
