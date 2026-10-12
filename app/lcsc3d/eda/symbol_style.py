"""Appearance presets shared by SchLib export and the SVG preview.

Appearance changes include square corners for AD rectangles and a minimum
pin length. Body bounds, text sizes, pin identities and visibility stay intact.
"""
from dataclasses import dataclass
import re


SYMBOL_STYLES = (('嘉立创风格', 'jlc'), ('AD 风格', 'ad'))


def normalize_symbol_style(value):
    return value if value in ('jlc', 'ad') else 'jlc'


@dataclass(frozen=True)
class SymbolStyle:
    # Calibrated against the user's native AD.SchLib (see the test fixture).
    outline: str = '#800000'
    pin: str = '#000000'
    text: str = '#000080'
    area: str = '#ffffb0'
    font: str = 'Times New Roman'
    line_width: int = 1  # AD Small = 10 mil = one EasyEDA schematic unit.
    light_fill_threshold: int = 192
    square_rectangles: bool = True
    rectangle_line_width: int = 1  # User-selected native AD Border: Small.
    rectangle_filled: bool = True
    rectangle_transparent: bool = True
    min_pin_length: int = 20  # User-selected minimum: 200 mil (10 mil per unit).
    pin_number_text: str = ''
    max_pin_text: int = 0
    preserve_stroke_width: bool = False

    def fill(self, value):
        """Keep transparent shapes and solid polarity/arrow marks distinct."""
        if str(value).strip().casefold() == 'none':
            return value
        value = value or '#000000'
        if re.fullmatch(r'#[0-9a-fA-F]{3}', value):
            value = '#' + ''.join(c * 2 for c in value[1:])
        if not re.fullmatch(r'#[0-9a-fA-F]{6}', value):
            return value  # The exporter still reports unsupported source data.
        return self.area if sum(bytes.fromhex(value[1:])) / 3 >= self.light_fill_threshold else self.outline

    def preview(self):
        # Font size belongs to each source text object, not to the preset.
        return dict(outline=self.outline, pin=self.pin, text=self.text, area=self.area,
                    font=self.font,
                    lineWidth=self.line_width, lightFillThreshold=self.light_fill_threshold,
                    squareRectangles=self.square_rectangles,
                    rectangleLineWidth=self.rectangle_line_width,
                    rectangleFilled=self.rectangle_filled, rectangleTransparent=self.rectangle_transparent,
                    minPinLength=self.min_pin_length, pinNumberText=self.pin_number_text,
                    maxPinText=self.max_pin_text, preserveStrokeWidth=self.preserve_stroke_width)


AD_STYLE = SymbolStyle()
KICAD_STYLE = SymbolStyle(outline='#840000', pin='#840000', text='#008484', area='#ffffc2',
    font='sans-serif', rectangle_transparent=False, square_rectangles=True, min_pin_length=0,
    pin_number_text='#840000', max_pin_text=5, preserve_stroke_width=True)


def symbol_style(value):
    if value == 'kicad':
        return KICAD_STYLE
    return AD_STYLE if normalize_symbol_style(value) == 'ad' else None
