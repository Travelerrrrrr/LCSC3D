"""Native storefront photo gallery with original pixels, thumbnails and zoom."""
from __future__ import annotations

from PySide6.QtCore import Qt, QTimer, QSize, Signal
from PySide6.QtGui import QIcon, QKeySequence, QPixmap, QShortcut
from PySide6.QtWidgets import (
    QDialog, QGraphicsScene, QGraphicsView, QHBoxLayout, QLabel, QListWidget,
    QListWidgetItem, QPushButton, QVBoxLayout,
)

from errors import Cancelled
from store import StoreError, check_cancelled


class ProductImage(QLabel):
    clicked = Signal()

    def __init__(self):
        super().__init__()
        self.setTextFormat(Qt.PlainText)
        self.setAlignment(Qt.AlignCenter)
        self.setFocusPolicy(Qt.StrongFocus)
        self.original = QPixmap()

    def set_image(self, pixmap):
        self.original = pixmap
        self._resize_image()

    def clear(self):
        self.original = QPixmap()
        super().clear()

    def _resize_image(self):
        if not self.original.isNull():
            self.setPixmap(self.original.scaled(self.contentsRect().size() - QSize(12, 12),
                                               Qt.KeepAspectRatio, Qt.SmoothTransformation))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._resize_image()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.clicked.emit()
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key_Return, Qt.Key_Enter, Qt.Key_Space):
            self.clicked.emit()
            event.accept()
        else:
            super().keyPressEvent(event)


class PhotoView(QGraphicsView):
    zoom_changed = Signal(float)

    def __init__(self):
        super().__init__()
        self.canvas = QGraphicsScene(self)
        self.setScene(self.canvas)
        self.setDragMode(QGraphicsView.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.AnchorViewCenter)
        self.setStyleSheet('background:#f4f7fa;border:1px solid #dce4eb;border-radius:8px;')
        self.photo = None
        self.fitted = True

    @property
    def zoom(self):
        return self.transform().m11()

    def set_photo(self, pixmap):
        self.canvas.clear()
        self.photo = self.canvas.addPixmap(pixmap)
        self.canvas.setSceneRect(self.photo.boundingRect())
        self.fit_photo()

    def clear_photo(self):
        self.canvas.clear()
        self.photo = None
        self.resetTransform()

    def fit_photo(self):
        self.fitted = True
        if self.photo:
            self.resetTransform()
            self.fitInView(self.photo, Qt.KeepAspectRatio)
            self.zoom_changed.emit(self.zoom)

    def set_zoom(self, zoom):
        if self.photo:
            zoom = max(0.01, min(32.0, zoom))
            self.fitted = False
            self.scale(zoom / self.zoom, zoom / self.zoom)
            self.zoom_changed.emit(self.zoom)

    def wheelEvent(self, event):
        if self.photo:
            self.set_zoom(self.zoom * (1.25 if event.angleDelta().y() > 0 else 0.8))
            event.accept()
        else:
            super().wheelEvent(event)

    def mouseDoubleClickEvent(self, event):
        self.fit_photo()
        event.accept()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self.fitted:
            self.fit_photo()


class ImageGallery(QDialog):
    activity_finished = Signal()
    closed = Signal()

    def __init__(self, parent, product, client, jobs_factory):
        super().__init__(parent)
        self.part = product['part']
        self.urls = product.get('images') or ([product['image']] if product.get('image') else [])
        self.thumb_urls = product.get('thumbnails') or self.urls
        self.client = client
        self.jobs = jobs_factory(self)
        self.jobs.idle.connect(self.activity_finished)
        self.cache = {}
        self.setWindowTitle(f"商品原图 · {self.part} · {product.get('title', '')}")
        self.resize(960, 790)
        self.setMinimumSize(600, 460)
        layout = QVBoxLayout(self)
        controls = QHBoxLayout()
        self.previous = QPushButton('上一张')
        self.previous.clicked.connect(lambda: self.select_offset(-1))
        controls.addWidget(self.previous)
        self.position = QLabel('')
        controls.addWidget(self.position)
        self.next = QPushButton('下一张')
        self.next.clicked.connect(lambda: self.select_offset(1))
        controls.addWidget(self.next)
        controls.addStretch()
        self.minus = QPushButton('缩小')
        self.plus = QPushButton('放大')
        self.actual = QPushButton('原始大小')
        self.fit = QPushButton('适应窗口')
        for button in (self.minus, self.plus, self.actual, self.fit):
            controls.addWidget(button)
        self.scale_label = QLabel('')
        controls.addWidget(self.scale_label)
        layout.addLayout(controls)
        self.view = PhotoView()
        self.view.zoom_changed.connect(lambda value: self.scale_label.setText(f'{value * 100:.0f}%'))
        self.minus.clicked.connect(lambda: self.view.set_zoom(self.view.zoom / 1.25))
        self.plus.clicked.connect(lambda: self.view.set_zoom(self.view.zoom * 1.25))
        self.actual.clicked.connect(lambda: self.view.set_zoom(1))
        self.fit.clicked.connect(self.view.fit_photo)
        layout.addWidget(self.view, 1)
        self.thumbnails = QListWidget()
        self.thumbnails.setFlow(QListWidget.LeftToRight)
        self.thumbnails.setWrapping(False)
        self.thumbnails.setIconSize(QSize(76, 76))
        self.thumbnails.setFixedHeight(112)
        for index, _ in enumerate(self.urls):
            item = QListWidgetItem(f'{index + 1}')
            item.setTextAlignment(Qt.AlignCenter)
            item.setSizeHint(QSize(96, 92))
            self.thumbnails.addItem(item)
        self.thumbnails.currentRowChanged.connect(self.load_image)
        layout.addWidget(self.thumbnails)
        self.status = QLabel('点击缩略图切换 · 滚轮缩放 · 拖动平移 · 双击适应窗口')
        self.status.setTextFormat(Qt.PlainText)
        layout.addWidget(self.status)
        self.shortcuts = []
        for key, callback in ((Qt.Key_Left, lambda: self.select_offset(-1)),
                              (Qt.Key_Right, lambda: self.select_offset(1))):
            shortcut = QShortcut(QKeySequence(key), self)
            shortcut.activated.connect(callback)
            self.shortcuts.append(shortcut)
        QTimer.singleShot(0, self.begin)

    def begin(self):
        if not self.isVisible() or not self.urls:
            return
        self.thumbnails.setCurrentRow(0)
        self.jobs.start('thumbnails', self.fetch_thumbnails, lambda _: None, lambda _: None, self.thumbnail_loaded)

    def fetch_thumbnails(self, stop, progress):
        for index, url in enumerate(self.thumb_urls[:len(self.urls)]):
            check_cancelled(stop)
            try:
                data = self.client.image(url, stop)
                progress(index, data)
            except Cancelled:
                raise
            except StoreError:
                continue

    def thumbnail_loaded(self, index, data):
        pixmap = QPixmap()
        if data and pixmap.loadFromData(data):
            self.thumbnails.item(index).setIcon(QIcon(pixmap))

    def select_offset(self, offset):
        row = self.thumbnails.currentRow() + offset
        if 0 <= row < len(self.urls):
            self.thumbnails.setCurrentRow(row)

    def load_image(self, index):
        if not 0 <= index < len(self.urls):
            return
        self.jobs.cancel('original')
        self.view.clear_photo()
        self.position.setText(f'{index + 1} / {len(self.urls)}')
        self.previous.setEnabled(index > 0)
        self.next.setEnabled(index + 1 < len(self.urls))
        self.scale_label.clear()
        self.status.setText('正在加载商品原图…')
        if index in self.cache:
            self.show_photo(index, self.cache[index])
        else:
            self.jobs.start('original', lambda stop, progress: self.client.image(self.urls[index], stop),
                            lambda data: self.image_loaded(index, data), self.image_failed)

    def image_loaded(self, index, data):
        pixmap = QPixmap()
        if not data or not pixmap.loadFromData(data):
            self.image_failed(StoreError('商品原图加载失败，请重新选择图片重试。'))
            return
        self.cache[index] = pixmap
        self.show_photo(index, pixmap)

    def show_photo(self, index, pixmap):
        if index == self.thumbnails.currentRow():
            self.view.set_photo(pixmap)
            self.status.setText(f'原图 {pixmap.width()} × {pixmap.height()} · 滚轮缩放 · 拖动平移 · 双击适应窗口')

    def image_failed(self, error):
        self.status.setText(str(error))

    def closeEvent(self, event):
        self.jobs.cancel_all()
        event.accept()
        self.closed.emit()

    def reject(self):
        self.close()
