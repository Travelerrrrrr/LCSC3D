"""Render the supplied vector logo at the current display resolution."""
from pathlib import Path
import sys
from PySide6.QtSvgWidgets import QSvgWidget
from PySide6.QtCore import Qt
from app_theme import theme_manager

ROOT = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent)) / 'assets' / 'branding'


class BrandLogo(QSvgWidget):
    def __init__(self, *, compact=False, parent=None):
        super().__init__(parent)
        self.compact = compact
        self.setAccessibleName('LCSC3D')
        self.setFixedSize(52, 52) if compact else self.setFixedSize(240, 78)
        theme_manager().changed.connect(self.refresh)
        self.refresh()

    def refresh(self):
        name = 'lcsc3d-icon.svg' if self.compact else 'lcsc3d-logo-dark.svg' if theme_manager().dark else 'lcsc3d-logo.svg'
        self.load(str(ROOT / name))
        self.renderer().setAspectRatioMode(Qt.KeepAspectRatio)
