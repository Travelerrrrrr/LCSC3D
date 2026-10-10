"""Qt Material themes, accessible application tokens and live appearance updates."""
import hashlib
import json
import tempfile
from pathlib import Path
from xml.sax.saxutils import escape
from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QColor, QPalette, QFont, QFontDatabase
from PySide6.QtWidgets import QApplication

from app_settings import Preferences, DEFAULT_FONT_FAMILY, DEFAULT_FONT_SIZE
from app_paths import data_directory


def material_stylesheet(c):
    """Build upstream Qt Material with all generated assets in local app data.

    Each palette gets immutable assets so two running copies cannot remove one
    another's icons. Absolute URLs also avoid Qt's global icon search path
    selecting icons left over from a previously applied palette.
    """
    from qt_material import build_stylesheet
    theme = dict(primaryColor=c['accent'], primaryLightColor=c['accent_hover'],
                 secondaryColor=c['field'], secondaryLightColor=c['border'],
                 secondaryDarkColor=c['surface'], primaryTextColor=c['text'],
                 secondaryTextColor=c['text'])
    extra = dict(font_family=c['font_family'], font_size=c['font_size'],
                 density_scale='0', danger=c['error'], warning=c['warning'], success=c['success'])
    key = hashlib.sha256(json.dumps([theme, extra], sort_keys=True).encode()).hexdigest()[:20]
    root = data_directory() / 'cache' / 'qt-material-2.17'
    root.mkdir(parents=True, exist_ok=True)
    cache = root / key
    qss = cache / 'material.qss'
    if qss.is_file():
        return qss.read_text(encoding='utf-8')
    with tempfile.TemporaryDirectory(prefix='build-', dir=root) as directory:
        stage = Path(directory) / key
        stage.mkdir()
        xml = stage / 'theme.xml'
        xml.write_text('<resources>' + ''.join(
            f'<color name="{name}">{escape(value)}</color>' for name, value in theme.items()) + '</resources>', encoding='utf-8')
        # Upstream's explicit-output prefix is a leading dot, followed by the
        # full path. export=True skips Roboto in favor of the chosen system font.
        result = build_stylesheet(str(xml), extra=extra, parent='.' + str(stage / 'icons'), export=True)
        result = result.replace('icon:/', (cache / 'icons').as_posix() + '/')
        (stage / 'material.qss').write_text(result, encoding='utf-8')
        try:
            stage.rename(cache)
        except FileExistsError:
            # Another process completed the same immutable palette first.
            return qss.read_text(encoding='utf-8')
    return result


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
    c['control_height'] = max(20, round(c['font_size'] * 1.5))
    result = (assets / 'material-overrides.qss').read_text(encoding='utf-8') % c
    # Upstream supplies selected/focused table indicators with more specific
    # selectors. Cover those combinations so high contrast ticks also survive
    # a selected row, keyboard focus and disabled download controls.
    for state, image_key in (('unchecked', None), ('checked', 'image'), ('indeterminate', 'partial_image')):
        for enabled in ('enabled', 'disabled'):
            prefix = 'disabled_check' if enabled == 'disabled' else 'check'
            image = 'none' if image_key is None else 'url("' + c[prefix + '_' + image_key] + '")'
            selectors = [f'{widget}::indicator:{state}:{enabled}{selected}{focus}{active}'
                         for widget in ('QCheckBox', 'QTableView', 'QTableWidget', 'QListView')
                         for selected in ('', ':selected') for focus in ('', ':focus') for active in ('', ':active')]
            result += '\n' + ', '.join(selectors) + ' { image:' + image + '; }'
    return result


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
        if self.applied and c == self.tokens and self.app.styleSheet() == self._stylesheet:
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
        self._stylesheet = material_stylesheet(c) + '\n' + stylesheet(c)
        self.app.setPalette(palette)
        self.app.setStyleSheet(self._stylesheet)
        self.changed.emit()


def theme_manager():
    app = QApplication.instance()
    if not hasattr(app, '_theme_manager'):
        app._theme_manager = ThemeManager(app)
    return app._theme_manager
