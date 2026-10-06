"""Display the official SVGs used by the domestic LCSC storefront."""
from __future__ import annotations

import base64
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree as ET

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile
from PySide6.QtWebEngineWidgets import QWebEngineView


SYMBOL_BACKGROUND = '#ffffff'
FOOTPRINT_BACKGROUND = '#000000'
SVG_NAMESPACE = 'http://www.w3.org/2000/svg'
RESOURCE_ROOT = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent))


@dataclass(frozen=True)
class VectorPreview:
    svg: bytes = b''
    name: str = ''
    count: int = 0
    error: str = ''


@dataclass(frozen=True)
class LibraryPreview:
    title: str
    symbols: tuple[VectorPreview, ...]
    footprint: VectorPreview
    source_url: str = ''


def _read_svg(entry: dict, kind: str) -> VectorPreview:
    label = '符号' if kind == 'symbol' else '封装'
    text = entry.get('svg')
    if not isinstance(text, str) or not text.strip():
        return VectorPreview(error=f'官方库中没有{label} SVG 数据')
    try:
        root = ET.fromstring(text)
        if root.tag != f'{{{SVG_NAMESPACE}}}svg':
            raise ValueError('文档不是 SVG')
        geometry = {'path', 'rect', 'circle', 'ellipse', 'polygon', 'polyline', 'line', 'text', 'use', 'image'}
        if not any(node.tag.rsplit('}', 1)[-1] in geometry for node in root.iter()):
            raise ValueError('没有可显示的图形')
        part_type = 'part_pin' if kind == 'symbol' else 'part_pad'
        count = sum(node.get('c_partid') == part_type for node in root.iter())
        name_key = 'name' if kind == 'symbol' else 'package'
        name = ''
        for node in root.iter():
            params = node.get('c_para', '').split('`')
            attributes = dict(zip(params[::2], params[1::2]))
            if attributes.get(name_key):
                name = attributes[name_key]
                break
        # Keep the server's paths, text, units and CSS exactly as supplied.
        return VectorPreview(text.encode('utf-8'), name, count)
    except (ValueError, ET.ParseError) as exc:
        return VectorPreview(error=f'官方{label} SVG 无法预览：{exc}')


def build_library_preview(data: dict, part: str = '') -> LibraryPreview:
    entries = data.get('result') or []
    if not isinstance(entries, list):
        entries = []
    entries = [entry for entry in entries if isinstance(entry, dict)]
    # Match the storefront's preference for multi-unit symbols (docType 6).
    symbol_type = 6 if any(entry.get('docType') == 6 for entry in entries) else 2
    symbols = tuple(_read_svg(entry, 'symbol') for entry in entries if entry.get('docType') == symbol_type)
    if not symbols:
        symbols = (VectorPreview(error='官方库中没有符号 SVG 数据'),)
    footprints = [entry for entry in entries if entry.get('docType') == 4]
    footprint = _read_svg(footprints[0], 'footprint') if footprints else VectorPreview(error='官方库中没有封装 SVG 数据')
    title = next((document.name for document in symbols if document.name), part)
    return LibraryPreview(title, symbols, footprint, str(data.get('source_url') or ''))


class SvgPreviewPage(QWebEnginePage):
    rendered = Signal(int, str, str)

    def javaScriptConsoleMessage(self, level, message, line, source):
        if message.startswith('LCSC3D_SVG_STATE:'):
            try:
                state = json.loads(message[len('LCSC3D_SVG_STATE:'):])
                self.rendered.emit(int(state['token']), state['status'], state['message'])
            except (ValueError, TypeError, KeyError):
                pass


class VectorPreviewView(QWebEngineView):
    """A local browser canvas that preserves the official SVG's CSS."""
    state = Signal(str, str)

    def __init__(self, background, parent=None, profile=None):
        super().__init__(parent)
        self.background = background
        self.document = None
        self.revision = 0
        self.shell_ready = False
        self.shell_started = False
        self.load_state = ('empty', '')
        self.profile = profile if profile is not None else QWebEngineProfile(self)
        page = SvgPreviewPage(self.profile, self)
        self.setPage(page)
        page.setBackgroundColor(QColor(background))
        page.rendered.connect(self.on_rendered)
        page.renderProcessTerminated.connect(self.on_render_process_terminated)
        self.loadFinished.connect(self.on_shell_loaded)
        self.setContextMenuPolicy(Qt.NoContextMenu)

    def set_state(self, status, message):
        self.load_state = (status, message)
        self.state.emit(status, message)

    def on_shell_loaded(self, ok):
        self.shell_ready = ok
        if not ok:
            self.set_state('error', '预览画布加载失败，请重新加载')
        elif self.document is not None:
            self.load_document()

    def on_rendered(self, token, status, message):
        if token == self.revision:
            self.set_state(status, message)

    def on_render_process_terminated(self, *args):
        self.shell_ready = False
        self.set_state('error', '预览画布已停止，请重新加载')

    def show_document(self, document: VectorPreview) -> bool:
        if document.error or not document.svg:
            return False
        if document is self.document and self.load_state[0] != 'error':
            return True
        restart_shell = self.load_state[0] == 'error' and not self.shell_ready
        self.document = document
        self.revision += 1
        self.set_state('loading', '正在显示商城官方 SVG…')
        if not self.shell_started:
            self.shell_started = True
            self.setHtml((RESOURCE_ROOT / 'vector_viewer.html').read_text(encoding='utf-8'))
        elif self.shell_ready:
            self.load_document()
        elif restart_shell:
            self.reload()
        return True

    def load_document(self):
        encoded = base64.b64encode(self.document.svg).decode('ascii')
        # Pass data after the small shell loads, avoiding setHtml's 2 MB limit.
        args = ','.join(json.dumps(value) for value in (encoded, self.background, self.revision))
        self.page().runJavaScript(f'window.showSvgDocument({args})')

    def fit_content(self):
        if self.shell_ready and self.document is not None:
            self.page().runJavaScript('window.fitSvg()')
