"""Vector previews from the same official component data used by the exporters."""
from __future__ import annotations

from dataclasses import dataclass
from xml.etree import ElementTree as ET

from easyeda2kicad.easyeda.easyeda_svg_renderer import render_footprint_svg, render_symbol_svg
from PySide6.QtCore import QByteArray, Qt, QTimer
from PySide6.QtGui import QColor, QPainter
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtSvgWidgets import QGraphicsSvgItem
from PySide6.QtWidgets import QFrame, QGraphicsItem, QGraphicsScene, QGraphicsView


SYMBOL_BACKGROUND = '#ffffff'
FOOTPRINT_BACKGROUND = '#172a3c'
SYMBOL_SHAPES = {'P', 'PL', 'PG', 'E', 'C', 'R', 'A', 'PT', 'T'}
FOOTPRINT_SHAPES = {'PAD', 'TRACK', 'CIRCLE', 'ARC', 'RECT', 'SOLIDREGION', 'HOLE', 'VIA', 'TEXT', 'SILK_LABEL'}


@dataclass(frozen=True)
class VectorPreview:
    svg: bytes = b''
    name: str = ''
    count: int = 0
    error: str = ''
    unsupported: tuple[str, ...] = ()


@dataclass(frozen=True)
class LibraryPreview:
    title: str
    symbols: tuple[VectorPreview, ...]
    footprint: VectorPreview


def _render(data: dict, kind: str) -> VectorPreview:
    label = '符号' if kind == 'symbol' else '封装'
    package = data.get('packageDetail')
    block = data.get('dataStr') if kind == 'symbol' else package.get('dataStr') if isinstance(package, dict) else None
    if not isinstance(block, dict) or not isinstance(block.get('shape'), list) or not block['shape']:
        return VectorPreview(error=f'官方库中没有{label}数据')
    try:
        shapes = [shape for shape in block['shape'] if isinstance(shape, str)]
        types = {shape.split('~', 1)[0] for shape in shapes}
        supported = SYMBOL_SHAPES if kind == 'symbol' else FOOTPRINT_SHAPES
        unknown = tuple(sorted(types - supported - {'SVGNODE'}))
        count = sum(shape.startswith('P~' if kind == 'symbol' else 'PAD~') for shape in shapes)
        params = (block.get('head') or {}).get('c_para') or {}
        name = str(data.get('title') or params.get('name') or '') if kind == 'symbol' else str(params.get('package') or '')
        renderer = render_symbol_svg if kind == 'symbol' else render_footprint_svg
        svg = renderer(data, bg_color=SYMBOL_BACKGROUND if kind == 'symbol' else FOOTPRINT_BACKGROUND)
        root = ET.fromstring(svg)
        # A background rectangle and title alone are not a usable preview.
        if len(root) <= 2:
            return VectorPreview(name=name, error=f'{label}没有可显示的图形', unsupported=unknown)
        if kind == 'footprint':
            _fit_pad_labels(root, shapes)
            ET.register_namespace('', 'http://www.w3.org/2000/svg')
            svg = ET.tostring(root, encoding='unicode')
        return VectorPreview(svg.encode('utf-8'), name, count, unsupported=unknown)
    except (ValueError, TypeError, KeyError, IndexError, AttributeError, ET.ParseError) as exc:
        return VectorPreview(error=f'{label}数据无法预览：{exc}')


def _fit_pad_labels(root, shapes):
    # Upstream uses a fixed font size of 2 canvas units, which overlaps fine-
    # pitch pads. Qt SVG also needs an explicit baseline for vertical centering.
    pads = {}
    for shape in shapes:
        fields = shape.split('~')
        if fields[0] == 'PAD' and len(fields) > 8:
            pads[(fields[8], float(fields[2]), float(fields[3]))] = (abs(float(fields[4])), abs(float(fields[5])))
    for text in root.iter('{http://www.w3.org/2000/svg}text'):
        key = (text.text, float(text.get('x', 0)), float(text.get('y', 0)))
        if key in pads:
            width, height = pads[key]
            size = min(2.0, min(width, height) * 0.7 / max(1.0, len(text.text) * 0.65))
            text.set('font-size', str(size))
            text.set('font-family', 'Arial')
            text.set('y', str(key[2] + size * 0.35))


def build_library_preview(data: dict) -> LibraryPreview:
    title = str(data.get('title') or '').strip()
    subparts = data.get('subparts') or []
    units = subparts if isinstance(subparts, list) and subparts else [data]
    symbols = tuple(_render({**unit, 'title': str(unit.get('title') or title)}, 'symbol')
                    if isinstance(unit, dict) else VectorPreview(error='符号单元数据无法预览')
                    for unit in units)
    return LibraryPreview(title, symbols, _render(data, 'footprint'))


class VectorPreviewView(QGraphicsView):
    """Qt vector canvas with bounded zoom, drag to pan, and automatic fitting."""
    def __init__(self, background, parent=None):
        super().__init__(parent)
        self.setScene(QGraphicsScene(self))
        self.setFrameShape(QFrame.NoFrame)
        self.setBackgroundBrush(QColor(background))
        self.setRenderHints(QPainter.Antialiasing | QPainter.TextAntialiasing | QPainter.SmoothPixmapTransform)
        self.setDragMode(QGraphicsView.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.AnchorViewCenter)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.document = None
        self.renderer = None
        self.item = None
        self.auto_fit = True
        self.fit_scale = 1.0
        self.fit_timer = QTimer(self)
        self.fit_timer.setSingleShot(True)
        self.fit_timer.timeout.connect(self.fit_if_automatic)

    def show_document(self, document: VectorPreview) -> bool:
        if document is self.document:
            return self.item is not None
        self.scene().clear()
        self.item = None
        if self.renderer:
            self.renderer.deleteLater()
        self.renderer = QSvgRenderer(QByteArray(document.svg), self)
        self.document = document
        if not self.renderer.isValid():
            return False
        self.item = QGraphicsSvgItem()
        self.item.setSharedRenderer(self.renderer)
        self.item.setCacheMode(QGraphicsItem.NoCache)
        self.scene().addItem(self.item)
        bounds = self.item.boundingRect()
        padding = max(bounds.width(), bounds.height()) * 0.06
        self.scene().setSceneRect(bounds.adjusted(-padding, -padding, padding, padding))
        self.auto_fit = True
        self.fit_content()
        return True

    def fit_content(self):
        if not self.item:
            return
        self.resetTransform()
        self.fitInView(self.sceneRect(), Qt.KeepAspectRatio)
        self.fit_scale = self.transform().m11()
        self.auto_fit = True

    def wheelEvent(self, event):
        delta = event.angleDelta().y()
        if not self.item or not delta:
            event.ignore()
            return
        self.auto_fit = False
        ratio = self.transform().m11() / self.fit_scale
        target = min(20.0, max(0.2, ratio * 1.2 ** (delta / 120)))
        self.scale(target / ratio, target / ratio)
        event.accept()

    def mousePressEvent(self, event):
        if self.item and event.button() == Qt.LeftButton:
            self.auto_fit = False
        super().mousePressEvent(event)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self.auto_fit:
            self.fit_timer.start(0)

    def fit_if_automatic(self):
        if self.auto_fit:
            self.fit_content()
