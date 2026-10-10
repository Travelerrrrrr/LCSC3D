"""Application palette, accent colors and live system appearance updates."""
from pathlib import Path
from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QColor, QPalette, QFont, QFontDatabase
from PySide6.QtWidgets import QApplication

from app_settings import Preferences, DEFAULT_FONT_FAMILY, DEFAULT_FONT_SIZE


def luminance(color):
    rgb = QColor(color).getRgbF()[:3]
    values = [v / 12.92 if v <= .04045 else ((v + .055) / 1.055) ** 2.4 for v in rgb]
    return sum(v * weight for v, weight in zip(values, (.2126, .7152, .0722)))


def contrast(first, second):
    a, b = sorted((luminance(first), luminance(second)))
    return (b + .05) / (a + .05)


def foreground(background):
    return '#ffffff' if contrast(background, '#ffffff') >= contrast(background, '#000000') else '#000000'


def colors(dark=False, accent='#168878', accent_text='auto'):
    result = (dict(bg='#171e28', surface='#222c39', field='#1b2531', text='#e5edf5',
                   muted='#a5b6c8', border='#435368', control_border='#9aacbf', hover='#303e50', disabled='#7f90a3',
                   success='#6cddbc', warning='#f2c47a', error='#ff9c9c') if dark else
              dict(bg='#f1f5f8', surface='#ffffff', field='#fafcfd', text='#23354a',
                   muted='#586b80', border='#cfd9e2', control_border='#66788a', hover='#eaf1f6', disabled='#718196',
                   success='#117767', warning='#9a620b', error='#b13d45'))
    ink = QColor(accent)
    # Small accent text remains legible even for custom near-white/black colors.
    for _ in range(100):
        if contrast(ink, result['surface']) >= 4.5:
            break
        ink = ink.lighter(110) if dark else ink.darker(110)
        if dark and ink.lightness() < 8:
            ink = QColor('#222222')
    result.update(accent=accent, on_accent=foreground(accent) if accent_text == 'auto' else accent_text, accent_ink=ink.name(),
                  accent_hover=QColor(accent).lighter(115).name() if dark else QColor(accent).darker(112).name())
    result['on_accent_hover'] = foreground(result['accent_hover']) if accent_text == 'auto' else accent_text
    return result


def effective_font_family(requested):
    """Portable preferences may name a font not installed on this computer."""
    families = {family.casefold(): family for family in QFontDatabase.families()}
    return families.get(requested.casefold()) or families.get(DEFAULT_FONT_FAMILY.casefold()) or QFontDatabase.systemFont(QFontDatabase.GeneralFont).family()


def typography(size=DEFAULT_FONT_SIZE):
    return dict(font_size=size, small_font_size=round(size * 12 / 13),
                section_font_size=round(size * 16 / 13), title_font_size=round(size * 25 / 13),
                indicator_size=max(16, round(size * 16 / 13)))


def stylesheet(c):
    c = dict(typography(), **c)
    assets = Path(__file__).resolve().parent / 'assets'
    for state, background in (('check', c['accent']), ('disabled_check', c['disabled'])):
        mark = 'white' if foreground(background) == '#ffffff' else 'black'
        c[state + '_color'] = foreground(background)
        c[state + '_image'] = (assets / ('check-' + mark + '.svg')).as_posix()
        c[state + '_partial_image'] = (assets / ('partial-' + mark + '.svg')).as_posix()
    return '''
QWidget { font-size:%(font_size)spx; color:%(text)s; }
QMainWindow, QDialog, QWidget#canvas, QScrollArea, QScrollArea > QWidget > QWidget { background:%(bg)s; }
QFrame#card { background:%(surface)s; border:1px solid %(border)s; border-radius:12px; }
QLabel#title { font-size:%(title_font_size)spx; font-weight:700; }
QLabel#section { font-size:%(section_font_size)spx; font-weight:700; }
QLabel#muted, QLabel#previewHint { color:%(muted)s; font-size:%(small_font_size)spx; }
QLabel#badge { color:%(accent_ink)s; background:%(surface)s; padding:5px 10px; border-radius:6px; font-weight:600; }
QLineEdit, QPlainTextEdit { background:%(field)s; border:1px solid %(border)s; border-radius:7px; padding:9px; selection-background-color:%(accent)s; selection-color:%(on_accent)s; }
QLineEdit:focus, QPlainTextEdit:focus { border:1px solid %(accent_ink)s; }
QLineEdit:disabled { color:%(disabled)s; background:%(bg)s; }
QPushButton { background:%(surface)s; border:1px solid %(border)s; border-radius:7px; padding:8px 13px; }
QPushButton:hover { background:%(hover)s; border-color:%(accent_ink)s; }
QPushButton:pressed { background:%(bg)s; }
QPushButton:disabled { color:%(disabled)s; background:%(bg)s; border-color:%(border)s; }
QPushButton#primary { background:%(accent)s; color:%(on_accent)s; border:1px solid %(accent)s; font-weight:600; }
QPushButton#primary:hover { background:%(accent_hover)s; color:%(on_accent_hover)s; }
QPushButton#primary:disabled { background:%(hover)s; color:%(disabled)s; border-color:%(border)s; }
QPushButton#previewMode:checked { background:%(accent)s; color:%(on_accent)s; border-color:%(accent)s; font-weight:600; }
QComboBox { border:1px solid %(border)s; border-radius:5px; padding:5px; background:%(surface)s; }
QSpinBox { border:1px solid %(border)s; border-radius:5px; padding:5px; background:%(field)s; selection-background-color:%(accent)s; selection-color:%(on_accent)s; }
QComboBox QAbstractItemView, QListWidget, QMenu { background:%(surface)s; color:%(text)s; selection-background-color:%(accent)s; selection-color:%(on_accent)s; }
QMenu::item { padding:7px 18px; }
QMenu::item:selected { background:%(accent)s; color:%(on_accent)s; }
QGroupBox { background:%(surface)s; border:1px solid %(border)s; border-radius:9px; margin-top:10px; font-weight:600; }
QGroupBox::title { subcontrol-origin:margin; left:14px; padding:0 5px; }
QTabWidget::pane { background:%(surface)s; border:1px solid %(border)s; border-radius:7px; }
QTabBar::tab { background:%(bg)s; padding:9px 18px; border:1px solid %(border)s; border-bottom:0; }
QTabBar::tab:selected { background:%(accent)s; color:%(on_accent)s; font-weight:600; }
QCheckBox { spacing:6px; }
QCheckBox::indicator, QTableView::indicator, QListView::indicator { width:%(indicator_size)spx; height:%(indicator_size)spx; border:2px solid %(control_border)s; border-radius:3px; background:%(field)s; }
QCheckBox::indicator:hover, QTableView::indicator:hover, QListView::indicator:hover { border-color:%(accent_ink)s; }
QCheckBox::indicator:checked, QTableView::indicator:checked, QListView::indicator:checked { border-color:%(check_color)s; background:%(accent)s; image:url("%(check_image)s"); }
QCheckBox::indicator:indeterminate, QTableView::indicator:indeterminate, QListView::indicator:indeterminate { border-color:%(check_color)s; background:%(accent)s; image:url("%(check_partial_image)s"); }
QCheckBox::indicator:disabled, QTableView::indicator:disabled, QListView::indicator:disabled { border-color:%(disabled)s; background:%(bg)s; }
QCheckBox::indicator:checked:disabled, QTableView::indicator:checked:disabled, QListView::indicator:checked:disabled { background:%(disabled)s; image:url("%(disabled_check_image)s"); }
QCheckBox::indicator:indeterminate:disabled, QTableView::indicator:indeterminate:disabled, QListView::indicator:indeterminate:disabled { background:%(disabled)s; image:url("%(disabled_check_partial_image)s"); }
QTableWidget { background:%(surface)s; alternate-background-color:%(field)s; border:1px solid %(border)s; border-radius:7px; gridline-color:%(border)s; outline:0; selection-background-color:%(accent)s; selection-color:%(on_accent)s; }
QTableWidget::item { padding:5px; border-bottom:1px solid %(border)s; }
QHeaderView::section { background:%(field)s; color:%(muted)s; border:0; border-bottom:1px solid %(border)s; padding:9px; font-weight:600; }
QTableCornerButton::section { background:%(field)s; border:0; }
QProgressBar { border:0; background:%(hover)s; border-radius:4px; height:8px; text-align:center; }
QProgressBar::chunk { background:%(accent)s; border-radius:4px; }
QScrollBar:vertical { background:%(bg)s; width:9px; border-radius:4px; }
QScrollBar::handle:vertical { background:%(border)s; border-radius:4px; min-height:26px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height:0; }
QScrollBar:horizontal { background:%(bg)s; height:9px; }
QScrollBar::handle:horizontal { background:%(border)s; min-width:26px; }
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width:0; }
QToolTip { background:%(surface)s; color:%(text)s; border:1px solid %(border)s; padding:5px; }
QFrame#previewEmpty, QGraphicsView#photoCanvas { background:%(field)s; border:1px solid %(border)s; border-radius:8px; }
QLabel#previewCube { font-size:64px; color:%(muted)s; }
QLabel#previewStatus { color:%(muted)s; }
QLabel#previewStatus[state="error"] { color:%(error)s; }
QLabel#previewStatus[state="ready"] { color:%(success)s; }
QLabel#productImage { background:%(surface)s; border:1px solid %(border)s; border-radius:8px; }
''' % c


class ThemeManager(QObject):
    changed = Signal()

    def __init__(self, app):
        super().__init__(app)
        self.app = app
        self.preferences = Preferences()
        self.dark = False
        self.tokens = colors()
        self.applied = False
        app.styleHints().colorSchemeChanged.connect(self.system_changed)

    def system_changed(self, scheme):
        if self.preferences.theme_mode == 'system':
            self.apply(self.preferences, scheme)

    def apply(self, preferences, system_scheme=None):
        self.preferences = preferences
        scheme = self.app.styleHints().colorScheme() if system_scheme is None else system_scheme
        self.dark = preferences.theme_mode == 'dark' or (preferences.theme_mode == 'system' and scheme == Qt.ColorScheme.Dark)
        c = colors(self.dark, preferences.accent_color, preferences.accent_text_color)
        c['font_family'] = effective_font_family(preferences.font_family)
        c.update(typography(preferences.font_size))
        if self.applied and c == self.tokens:
            self.changed.emit()
            return
        self.tokens = c
        self.applied = True
        font = QFont(c['font_family'])
        font.setPixelSize(preferences.font_size)
        self.app.setFont(font)
        palette = QPalette()
        roles = {'Window': 'bg', 'WindowText': 'text', 'Base': 'field', 'AlternateBase': 'surface',
                 'ToolTipBase': 'surface', 'ToolTipText': 'text', 'Text': 'text', 'Button': 'surface',
                 'ButtonText': 'text', 'BrightText': 'on_accent', 'Highlight': 'accent',
                 'HighlightedText': 'on_accent', 'Link': 'accent_ink', 'PlaceholderText': 'muted'}
        for role, token in roles.items():
            palette.setColor(getattr(QPalette.ColorRole, role), QColor(c[token]))
        for role in (QPalette.WindowText, QPalette.Text, QPalette.ButtonText):
            palette.setColor(QPalette.Disabled, role, QColor(c['disabled']))
        self.app.setPalette(palette)
        self.app.setStyleSheet(stylesheet(c))
        self.changed.emit()


def theme_manager():
    app = QApplication.instance()
    if not hasattr(app, '_theme_manager'):
        app._theme_manager = ThemeManager(app)
    return app._theme_manager
