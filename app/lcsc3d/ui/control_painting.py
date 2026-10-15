"""Antialiased control surfaces with consistent physical-pixel outlines."""
import math

from PySide6.QtCore import Qt, QRectF
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QStyle, QStyleOptionButton


def rounded_surface(painter, rect, background, border=None, radius=6):
    """Keep all four straight edges on the same device-pixel grid."""
    transform = painter.deviceTransform()
    physical = transform.mapRect(QRectF(rect))
    left, top = math.ceil(physical.left()), math.ceil(physical.top())
    right, bottom = math.floor(physical.right()), math.floor(physical.bottom())
    scale = abs(transform.m11()) or 1
    width = max(1, round(scale)) if border else 0
    bounds = QRectF(left, top, right - left, bottom - top)
    bounds.adjust(width / 2, width / 2, -width / 2, -width / 2)
    inverse, valid = transform.inverted()
    if not valid or bounds.isEmpty():
        return
    painter.save()
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setBrush(QColor(background) if background else Qt.NoBrush)
    painter.setPen(QPen(QColor(border), width / scale) if border else Qt.NoPen)
    painter.drawRoundedRect(inverse.mapRect(bounds), radius, radius)
    painter.restore()


def paint_button(button):
    from lcsc3d.ui.app_theme import theme_manager
    c = theme_manager().tokens
    option = QStyleOptionButton()
    button.initStyleOption(option)
    enabled = bool(option.state & QStyle.State_Enabled)
    hover = bool(option.state & QStyle.State_MouseOver)
    pressed = bool(option.state & QStyle.State_Sunken)
    focused = bool(option.state & QStyle.State_HasFocus)
    checked = bool(option.state & QStyle.State_On)
    role, variant = button.objectName(), button.property('variant')
    navigation, page = role == 'navButton', role == 'pageButton'
    primary = role == 'primary' or role == 'previewMode' and checked
    flat = navigation or page or button.isFlat() or variant in ('text', 'danger')
    background, border = None, None if flat else c['border']
    if enabled:
        if primary:
            background = c['accent_hover' if hover or pressed else 'accent']
            border = c['accent_ink'] if focused else background
        elif navigation or page:
            background = c['surface'] if page and checked else c['hover'] if checked or hover else None
            border = c['accent_ink'] if checked or focused else None
        else:
            background = c['border'] if pressed else c['hover'] if hover or checked else None
            if focused or checked or hover and not flat:
                border = c['accent_ink']
    elif primary:
        background, border = c['hover'], c['border']
    painter = QPainter(button)
    rounded_surface(painter, button.rect(), background, border, 7 if page else 6)
    # Keep Qt's text, icon, mnemonic, layout direction and pressed-state layout.
    # Drawing only the label avoids a second style/focus border over our panel.
    option.rect = button.style().subElementRect(QStyle.SE_PushButtonContents, option, button)
    painter.setRenderHint(QPainter.TextAntialiasing)
    button.style().drawControl(QStyle.CE_PushButtonLabel, option, painter, button)
