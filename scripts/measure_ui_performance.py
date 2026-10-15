"""Measure native table selection and product painting with synthetic data."""
import argparse
import json
import os
from pathlib import Path
import statistics
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]


def measure():
    from PySide6.QtCore import Qt, QRect
    from PySide6.QtGui import QImage, QPainter
    from PySide6.QtWidgets import QApplication, QStyleOptionViewItem
    from lcsc3d.core.app_settings import Preferences
    from lcsc3d.ui.app_theme import theme_manager
    from lcsc3d.ui.commerce import CartPanel

    app = QApplication([])
    theme_manager().apply(Preferences())
    panel = CartPanel()
    rows = [dict(key=str(i), part=f'C{i + 1}', title='STM32F103C8T6', package='LQFP-48',
                 manufacturer='测试品牌', description='32 位低功耗微控制器，支持多种串行接口与定时器。' * 3,
                 parameters=[['Flash', '64KB']], image='', status='', checked=False,
                 price='1.25', total='2.50', quantity=2, minimum=1, currency='¥') for i in range(500)]
    panel.show_rows(rows)
    app.processEvents()
    samples = []
    for _ in range(7):
        start = time.perf_counter()
        panel.toggle_selection()
        samples.append((time.perf_counter() - start) * 1000)
    assert len(panel.selected_rows()) == len(rows)
    assert '1250.00' in panel.summary.text()
    target = QImage(420, 160, QImage.Format_ARGB32_Premultiplied)
    target.fill(Qt.white)
    option = QStyleOptionViewItem()
    option.widget = panel.table
    option.rect = QRect(0, 0, 420, 160)
    option.font = panel.table.font()
    painter = QPainter(target)
    paint_samples = []
    try:
        for _ in range(5):
            start = time.perf_counter()
            for _ in range(500):
                panel.table.summary_delegate.paint(painter, option, panel.table.model().index(0, 2))
            paint_samples.append((time.perf_counter() - start) * 1000)
    finally:
        painter.end()
        panel.close()
    return dict(rows=500, selection_ms=samples, median_selection_ms=statistics.median(samples),
                paints_per_sample=500, paint_ms=paint_samples, median_paint_ms=statistics.median(paint_samples))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    work = ROOT / 'work'
    work.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='ui-measure-', dir=work) as temporary:
        os.environ['LOCALAPPDATA'] = temporary
        sys.path.insert(0, str(ROOT / 'app'))
        result = measure()
        from lcsc3d.core.app_logging import close_logging
        close_logging()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2), encoding='utf-8')
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
