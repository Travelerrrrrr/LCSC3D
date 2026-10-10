"""Public product photos embedded in the main component preview."""
from i18n import text as ui_text, message as ui_message
from PySide6.QtCore import Signal, Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (QWidget, QHBoxLayout, QVBoxLayout)
from localized_widgets import (QPushButton, QLabel)

from favorites import Jobs
from store import StoreClient, StoreError
from international_store import catalog_client
from store_images import PhotoView


class ProductPreview(QWidget):
    state = Signal(str, str)

    def __init__(self, parent=None, *, client_factory=None):
        super().__init__(parent)
        self.factory = client_factory or catalog_client
        self.jobs = Jobs(self)
        self.part, self.urls, self.index = '', [], -1
        self.cache, self.cache_size = {}, 0
        self.load_state = ('empty', ui_text('选择器件后查看商品图片'))
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.view = PhotoView()
        layout.addWidget(self.view, 1)
        controls = QHBoxLayout()
        self.previous, self.next = QPushButton(ui_text('上一张')), QPushButton(ui_text('下一张'))
        self.position = QLabel('')
        self.position.setAlignment(Qt.AlignCenter)
        self.previous.clicked.connect(lambda: self.load_image(self.index-1))
        self.next.clicked.connect(lambda: self.load_image(self.index+1))
        controls.addWidget(self.previous)
        controls.addWidget(self.position, 1)
        controls.addWidget(self.next)
        layout.addLayout(controls)
        self.update_controls()

    def notify(self, state, text):
        self.load_state = state, text
        self.state.emit(state, text)

    def clear(self):
        self.jobs.cancel_all()
        self.part, self.urls, self.index = '', [], -1
        self.cache, self.cache_size = {}, 0
        self.view.clear_photo()
        self.update_controls()

    def select(self, part, refresh=False):
        if self.part == part and not refresh:
            self.state.emit(*self.load_state)
            return
        self.clear()
        self.part = part
        self.notify('loading', ui_text('正在获取商城商品图片…'))
        client = self.factory()
        self.jobs.start('product-photo-detail', lambda stop, progress: client.product(part, stop),
                        self.product_loaded, self.failed)

    def product_loaded(self, product):
        if product.get('part') != self.part:
            self.failed(StoreError(ui_text('商城返回的商品编号不一致，请重试')))
            return
        self.urls = product.get('images') or ([product['image']] if product.get('image') else [])
        if not self.urls:
            self.failed(StoreError(ui_text('该元件暂无商品图片')))
            return
        self.load_image(0)

    def load_image(self, index):
        if not 0 <= index < len(self.urls):
            return
        self.jobs.cancel('product-photo')
        self.index = index
        self.view.clear_photo()
        self.update_controls()
        self.notify('loading', ui_text('正在加载商品原图…'))
        if index in self.cache:
            self.loaded(self.cache[index])
        else:
            client, url = self.factory(), self.urls[index]
            self.jobs.start('product-photo', lambda stop, progress: client.image(url, stop), self.loaded, self.failed)

    def loaded(self, data):
        pixmap = QPixmap()
        if not data or not pixmap.loadFromData(data):
            self.failed(StoreError(ui_text('商品图片无法解码，请重新加载')))
            return
        if self.index not in self.cache and len(data) <= 16 * 1024 * 1024:
            if self.cache_size + len(data) > 16 * 1024 * 1024:
                self.cache, self.cache_size = {}, 0
            self.cache[self.index] = data
            self.cache_size += len(data)
        self.view.set_photo(pixmap)
        self.notify('ready', ui_message('商品实物图 · {0} × {1} · 滚轮缩放，拖动平移', pixmap.width(), pixmap.height()))

    def failed(self, error):
        self.notify('error', str(error))

    def update_controls(self):
        self.previous.setEnabled(self.index > 0)
        self.next.setEnabled(0 <= self.index < len(self.urls)-1)
        self.position.setText(ui_message('{0} / {1}', self.index+1, len(self.urls)) if self.index >= 0 else ui_text('暂无图片'))
