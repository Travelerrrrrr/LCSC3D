"""Display the official SVGs used by the domestic LCSC storefront."""
from __future__ import annotations

import base64
import json
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from lcsc3d.core.assets import RESOURCE_ROOT
from xml.etree import ElementTree as ET
from lcsc3d.eda.resources import svg_entries
from lcsc3d.eda.symbol_style import symbol_style as resolve_symbol_style, normalize_symbol_style
from lcsc3d.core.app_logging import (traced, log_event, record_error, safe_part, current_context,
                         log_context, log_script_error)

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile
from PySide6.QtWebEngineWidgets import QWebEngineView


SYMBOL_BACKGROUND = '#ffffff'
FOOTPRINT_BACKGROUND = '#000000'
SVG_NAMESPACE = 'http://www.w3.org/2000/svg'


@dataclass(frozen=True)
class VectorPreview:
    svg: bytes = b''
    name: str = ''
    count: int = 0
    error: str = ''
    symbol_style: str = 'jlc'


def styled_symbol(document: VectorPreview, style: str) -> VectorPreview:
    style = style if style == 'kicad' else normalize_symbol_style(style)
    return document if document.symbol_style == style else replace(document, symbol_style=style)


@dataclass(frozen=True)
class LibraryPreview:
    title: str
    symbols: tuple[VectorPreview, ...]
    footprint: VectorPreview
    source_url: str = ''


@traced('preview.svg_parse', lambda entry, kind: {'viewer': kind})
def _read_svg(entry: dict, kind: str) -> VectorPreview:
    label = '符号' if kind == 'symbol' else '封装'
    text = entry.get('svg')
    if not isinstance(text, str) or not text.strip():
        log_event('WARNING', 'preview.svg_missing', viewer=kind)
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
        record_error(exc, 'preview.svg_invalid', viewer=kind)
        return VectorPreview(error=f'官方{label} SVG 无法预览：{exc}')


@traced('preview.library_parse', lambda data, part='': {'part': safe_part(part)})
def build_library_preview(data: dict, part: str = '') -> LibraryPreview:
    symbols = tuple(_read_svg(entry, 'symbol') for entry in svg_entries(data, 'SYMBOL'))
    if not symbols:
        symbols = (VectorPreview(error='官方库中没有符号 SVG 数据'),)
    footprints = svg_entries(data, 'FOOTPRINT')
    footprint = _read_svg(footprints[0], 'footprint') if footprints else VectorPreview(error='官方库中没有封装 SVG 数据')
    title = next((document.name for document in symbols if document.name), part)
    return LibraryPreview(title, symbols, footprint, str(data.get('source_url') or ''))


class SvgPreviewPage(QWebEnginePage):
    rendered = Signal(int, str, str)

    def javaScriptConsoleMessage(self, level, message, line, source):
        if message.startswith('LCSC3D_SVG_STATE:'):
            try:
                state = json.loads(message[len('LCSC3D_SVG_STATE:'):])
                if state.get('status') == 'error':
                    with log_context(getattr(self, 'diagnostic_context', {})):
                        log_event('ERROR', 'preview.render_failed', viewer='svg',
                                  reason='svg_render', revision=state.get('token') if type(state.get('token')) is int else None)
                self.rendered.emit(int(state['token']), state['status'], state['message'])
            except (ValueError, TypeError, KeyError) as exc:
                record_error(exc, 'preview.state_parse_failed', viewer='svg')
        elif getattr(level, 'value', 0) >= 1:
            with log_context(getattr(self, 'diagnostic_context', {})):
                log_script_error(level, message, line, 'svg')


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
        self.diagnostic_context = current_context()
        self.profile = profile if profile is not None else QWebEngineProfile(self)
        page = SvgPreviewPage(self.profile, self)
        self.setPage(page)
        page.setBackgroundColor(QColor(background))
        page.rendered.connect(self.on_rendered)
        page.renderProcessTerminated.connect(self.on_render_process_terminated)
        self.loadFinished.connect(self.on_shell_loaded)
        self.setContextMenuPolicy(Qt.NoContextMenu)

    def set_state(self, status, message):
        with log_context(self.diagnostic_context):
            log_event('ERROR' if status == 'error' else 'DEBUG', 'preview.svg_state',
                      revision=self.revision, state=status if status in ('loading', 'ready', 'error', 'empty') else 'unknown')
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
        with log_context(self.diagnostic_context):
            log_event('ERROR', 'preview.renderer_terminated', viewer='svg', revision=self.revision,
                      termination_status=getattr(args[0], 'value', None) if args else None,
                      exit_code=args[1] if len(args) > 1 else None)
        self.shell_ready = False
        self.set_state('error', '预览画布已停止，请重新加载')

    def show_document(self, document: VectorPreview) -> bool:
        self.diagnostic_context = current_context()
        self.page().diagnostic_context = self.diagnostic_context
        if document.error or not document.svg:
            return False
        if document == self.document and self.load_state[0] != 'error':
            return True
        restart_shell = self.load_state[0] == 'error' and not self.shell_ready
        self.document = document
        self.revision += 1
        self.set_state('loading', '正在显示器件预览…')
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
        style = resolve_symbol_style(self.document.symbol_style)
        args = ','.join(json.dumps(value) for value in (encoded, self.background, self.revision,
                                                      style.preview() if style else None))
        self.page().runJavaScript(f'window.showSvgDocument({args})')

    def fit_content(self):
        log_event('DEBUG', 'preview.svg_fit', revision=self.revision)
        if self.shell_ready and self.document is not None:
            self.page().runJavaScript('window.fitSvg()')
