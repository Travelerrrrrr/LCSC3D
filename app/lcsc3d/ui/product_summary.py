"""Paint readable product introductions without creating a widget per row."""
from PySide6.QtCore import Qt, QRectF, QPointF, QSize, QEvent
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QTextLayout, QTextOption
from PySide6.QtWidgets import QStyledItemDelegate, QStyle, QStyleOptionViewItem

from lcsc3d.core.i18n import text as tr, render
from lcsc3d.ui.app_theme import theme_manager

SUMMARY_ROLE = Qt.UserRole + 20


def summary_lines(product):
    title = product.get('title') or product.get('part') or product.get('code') or '—'
    if product.get('purchase_status') == 'unavailable':
        title = render(tr('已停售 · ')) + title
    metadata = ' · '.join(str(product[key]) for key in ('manufacturer', 'package', 'category') if product.get(key))
    description = product.get('description') or product.get('name') or ''
    if description == product.get('title'):
        description = ''
    parameters = ' · '.join(f'{key}: {value}' for key, value in (product.get('parameters') or [])[:4] if key and value)
    return str(title), metadata, str(description or render(tr('暂无商品简介'))), parameters


def set_product_summary(item, product):
    # Retain full data in a role and tooltip; only the painted preview is elided.
    item.setData(SUMMARY_ROLE, {key: product[key] for key in (
        'title', 'part', 'code', 'manufacturer', 'package', 'category', 'description', 'name',
        'parameters', 'purchase_status') if key in product})
    item.setToolTip('\n'.join(line for line in summary_lines(product) if line))


class ProductSummaryDelegate(QStyledItemDelegate):
    def __init__(self, table, column=2):
        super().__init__(table)
        self.table = table
        self.column = column
        table.setItemDelegateForColumn(column, self)
        table.installEventFilter(self)
        self.refresh_rows()

    def eventFilter(self, watched, event):
        if event.type() in (QEvent.FontChange, QEvent.ApplicationFontChange, QEvent.StyleChange):
            self.refresh_rows()
        return super().eventFilter(watched, event)

    def refresh_rows(self):
        header = self.table.horizontalHeaderItem(self.column)
        self.table.minimum_flexible_width = max(250, self.table.fontMetrics().height() * 16,
            self.table.fontMetrics().horizontalAdvance(header.text()) + 24 if header else 0)
        self.table.column_timer.start(0)
        self.table.verticalHeader().setDefaultSectionSize(self.row_height())
        self.table.viewport().update()

    def row_height(self):
        return max(104, self.table.fontMetrics().lineSpacing() * 5 + 28)

    def sizeHint(self, option, index):
        return QSize(self.table.minimum_flexible_width, self.row_height())

    @staticmethod
    def draw_lines(painter, text, rect, font, color, count=1):
        if not text or rect.width() <= 0:
            return
        painter.setFont(font)
        painter.setPen(color)
        text = ' '.join(text.split())
        metrics = QFontMetrics(font)
        layout = QTextLayout(text, font)
        wrap = QTextOption()
        wrap.setWrapMode(QTextOption.WrapAtWordBoundaryOrAnywhere)
        layout.setTextOption(wrap)
        layout.beginLayout()
        lines = []
        for index in range(count):
            line = layout.createLine()
            if not line.isValid():
                break
            line.setLineWidth(rect.width())
            lines.append(line)
        layout.endLayout()
        for index, line in enumerate(lines):
            if index == count - 1:
                remainder = metrics.elidedText(text[line.textStart():], Qt.ElideRight, int(rect.width()))
                painter.drawText(QRectF(rect.x(), rect.y() + index * metrics.lineSpacing(), rect.width(), metrics.height()),
                                 Qt.AlignLeft | Qt.AlignVCenter, remainder)
            else:
                line.draw(painter, QPointF(rect.x(), rect.y() + index * metrics.lineSpacing()))

    def paint(self, painter, option, index):
        product = index.data(SUMMARY_ROLE)
        if product is None:
            return super().paint(painter, option, index)
        prepared = QStyleOptionViewItem(option)
        self.initStyleOption(prepared, index)
        prepared.text = ''
        style = prepared.widget.style()
        style.drawControl(QStyle.CE_ItemViewItem, prepared, painter, prepared.widget)
        selected = bool(prepared.state & QStyle.State_Selected)
        enabled = bool(prepared.state & QStyle.State_Enabled)
        tokens = theme_manager().tokens
        ink = QColor(tokens['on_accent'] if selected else tokens['text'] if enabled else tokens['disabled'])
        muted = QColor(tokens['on_accent'] if selected else tokens['muted'] if enabled else tokens['disabled'])
        title, metadata, description, parameters = summary_lines(product)
        painter.save()
        painter.setRenderHint(QPainter.TextAntialiasing)
        painter.setClipRect(prepared.rect)
        rect = QRectF(prepared.rect).adjusted(10, 10, -10, -8)
        font = QFont(prepared.font)
        bold = QFont(font); bold.setBold(True)
        spacing = QFontMetrics(font).lineSpacing()
        self.draw_lines(painter, title, rect, bold, ink)
        rect.translate(0, spacing + 4)
        self.draw_lines(painter, metadata, rect, font, muted)
        rect.translate(0, spacing + 3)
        self.draw_lines(painter, description, rect, font, ink, 2)
        rect.translate(0, spacing * 2 + 3)
        self.draw_lines(painter, parameters, rect, font, muted)
        painter.restore()
