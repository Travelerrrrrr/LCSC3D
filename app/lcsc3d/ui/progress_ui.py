"""Animate real processing targets without making progress while work is idle."""
from PySide6.QtCore import QPropertyAnimation, QEasingCurve
from PySide6.QtWidgets import QProgressBar


class SmoothProgressBar(QProgressBar):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setRange(0, 10000)
        self.target = 0
        self.animation = QPropertyAnimation(self, b'value', self)
        self.animation.setDuration(320)
        self.animation.setEasingCurve(QEasingCurve.OutCubic)
        self.setFraction(0, immediate=True)

    def setFraction(self, fraction, *, immediate=False):
        target = round(max(0., min(1., fraction)) * self.maximum())
        if not immediate and target <= self.target:
            return
        self.animation.stop()
        self.target = target
        if immediate or target == self.value():
            self.setValue(target)
            return
        self.animation.setStartValue(max(0, self.value()))
        self.animation.setEndValue(target)
        self.animation.start()
