"""Render documentation screenshots from local official SVG fixtures."""
import json
from pathlib import Path
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'app'))
import main
from library_preview import build_library_preview
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication


def render():
    QApplication.setAttribute(Qt.AA_ShareOpenGLContexts)
    application = QApplication([])
    application.setStyle('Fusion')
    application.setFont(QFont('Microsoft YaHei UI', 9))
    application.setStyleSheet(main.STYLES)
    output = ROOT / 'docs' / 'images'
    output.mkdir(parents=True, exist_ok=True)
    with patch.object(main.MainWindow, 'request_component_info'):
        window = main.MainWindow(settings_enabled=False)
        for part in ('C2040', 'C20197'):
            data = json.loads((ROOT / 'app/tests/fixtures' / f'{part}_svgs.json').read_text(encoding='utf-8'))
            window.library_cache[part] = build_library_preview(data, part)
        window.component_info = {
            'C2040': {'title': 'RP2040', 'model': 'LQFN-56_L7.0-W7.0-P0.4-EP'},
            'C20197': {'title': '4D03WGJ0102T5E', 'model': 'R0603-8P_L3.2-W1.6-H0.6'},
            'C163691': {'title': 'SMDRS1275-152N', 'model': ''},
        }
        window.path_input.setText('D:/LCSC3DModels')
        window.set_preview_mode('symbol')
        window.input.setPlainText('C2040\nC20197\nC163691')
        window.load_queue()
        window.table.item(2, main.DOWNLOAD_COLUMN).setCheckState(Qt.Unchecked)
        window.show()
        state = {'index': 0, 'done': False}

        def capture():
            name = ('app.png', 'footprint.png')[state['index']]
            if not window.grab().scaledToWidth(1240, Qt.SmoothTransformation).save(str(output / name)):
                raise RuntimeError('Screenshot could not be saved')
            print(output / name, flush=True)
            state['index'] += 1
            if state['index'] == 2:
                state['done'] = True
                window.close()
                application.exit(0)
            else:
                window.set_preview_mode('footprint')
                QTimer.singleShot(50, poll)

        def poll():
            if window.preview_state == 'ready':
                QTimer.singleShot(150, capture)
            elif window.preview_state == 'error':
                print(window.preview_status.text(), file=sys.stderr)
                window.close()
                application.exit(1)
            else:
                QTimer.singleShot(50, poll)

        def timeout():
            if not state['done']:
                window.close()
                application.exit(1)

        QTimer.singleShot(100, poll)
        QTimer.singleShot(10000, timeout)
        return application.exec()


if __name__ == '__main__':
    sys.exit(render())
