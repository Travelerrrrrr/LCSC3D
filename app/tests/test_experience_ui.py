"""V2.4.3 navigation and product-row regressions, using synthetic data only."""
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PySide6.QtCore import Qt, QEvent, QRect
from PySide6.QtGui import QFont, QImage, QPainter, QFontMetrics
from PySide6.QtTest import QTest, QSignalSpy
from PySide6.QtWidgets import QApplication, QStyleOptionViewItem, QStyle
from lcsc3d.core import app_logging as logs
from lcsc3d.core.i18n import set_language
from lcsc3d.core.app_settings import Preferences
from lcsc3d.services.store import favorite_products, storefront_product
from lcsc3d.services.commerce import cart_items
from lcsc3d.services.order_details import parse_order_details
from lcsc3d.selftests.favorites_selftest import fixture_order_detail
from lcsc3d.ui.app_theme import theme_manager
from lcsc3d.ui.catalog_filters import CategoryPanel
from lcsc3d.ui.commerce import CartPanel
from lcsc3d.ui.order_details import OrderDetailPanel
from lcsc3d.ui.pagination import PaginationBar, visible_pages
from lcsc3d.ui.product_summary import SUMMARY_ROLE, summary_lines, set_product_summary


class PageSequenceTests(unittest.TestCase):
    def test_reference_first_page_and_all_page_boundaries(self):
        self.assertEqual(visible_pages(1, 50), [1, 2, 3, 4, 5, 6, None, 50])
        self.assertEqual(visible_pages(0, 0), [])
        for total in (*range(1, 70), 1000000):
            for current in range(1, min(total, 70) + 1):
                for slots in range(5, 9):
                    values = visible_pages(current, total, slots)
                    numbers = [value for value in values if value is not None]
                    self.assertLessEqual(len(values), slots)
                    self.assertIn(current, numbers)
                    self.assertEqual((numbers[0], numbers[-1]), (1, total))
                    self.assertEqual(numbers, sorted(set(numbers)))
                    for index, value in enumerate(values):
                        if value is None:
                            self.assertGreater(values[index + 1] - values[index - 1], 2)


class ExperienceUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if QApplication.instance() is None:
            QApplication.setAttribute(Qt.AA_ShareOpenGLContexts)
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        env = patch.dict(os.environ, LOCALAPPDATA=folder.name)
        env.start(); self.addCleanup(env.stop)
        logs.configure_logging(version='2.4.3'); self.addCleanup(logs.close_logging)
        theme_manager().apply(Preferences(theme_mode='light'))
        set_language('zh_CN'); self.addCleanup(set_language, 'zh_CN')
        self.widgets = []
        self.addCleanup(self.close)

    def show(self, widget):
        self.widgets.append(widget)
        widget.setAttribute(Qt.WA_DontShowOnScreen)
        widget.show()
        QTest.qWait(30)
        return widget

    def close(self):
        for widget in self.widgets:
            widget.close(); widget.deleteLater()
        self.app.sendPostedEvents(None, QEvent.DeferredDelete)

    def test_number_click_current_busy_empty_and_last_page(self):
        pager = self.show(PaginationBar())
        pager.resize(600, 60); pager.set_state(1, 50)
        QTest.qWait(30)
        events = QSignalSpy(pager.page_requested)
        pager.number_buttons[1].click()
        QTest.qWait(30)
        self.assertEqual(events.count(), 0)
        self.assertTrue(pager.number_buttons[1].isChecked())
        self.assertGreaterEqual(pager.number_buttons[1].width(), 34)
        pager.number_buttons[50].click()
        self.assertEqual(events.at(0), [50])
        pager.set_state(50, 50, True)
        pager.previous.click(); pager.number_buttons[49].click()
        self.assertEqual(events.count(), 1)
        pager.set_state(50, 50)
        theme_manager().apply(Preferences(theme_mode='dark')); QTest.qWait(30)
        self.assertGreaterEqual(pager.number_buttons[50].width(), 34)
        self.assertTrue(pager.next.isHidden())
        pager.previous.click()
        self.assertEqual(events.at(1), [49])
        pager.set_state(0, 0)
        self.assertFalse(pager.number_buttons)
        self.assertFalse(pager.previous.isEnabled() or pager.next.isEnabled())
        events_text = (logs.get_log_directory() / 'LCSC3D.log').read_text('utf-8')
        self.assertIn('pagination.requested', events_text)
        self.assertIn('"page": 50', events_text)

    def test_large_font_narrow_pagination_remains_inside_its_viewport(self):
        pager = self.show(PaginationBar())
        font = QFont(pager.font()); font.setPixelSize(24)
        pager.setFont(font); pager.resize(260, 70); pager.set_state(25, 50)
        QTest.qWait(60)
        self.assertIn(25, pager.number_buttons)
        for widget in (pager.previous, pager.next, *pager.number_buttons.values()):
            point = widget.mapTo(pager, widget.rect().bottomRight())
            self.assertTrue(pager.rect().contains(point), (widget.text(), point, pager.size()))

    def test_categories_begin_collapsed_and_search_still_finds_hidden_children(self):
        panel = self.show(CategoryPanel())
        panel.show_categories([dict(name='元器件', id='', children=[
            dict(name='电容', id='1', children=[dict(name='贴片电容', id='2', children=[])]),
            dict(name='电阻', id='3', children=[])])])
        parent = panel.tree.topLevelItem(0)
        self.assertFalse(parent.isExpanded())
        parent.setExpanded(True)
        self.assertFalse(parent.child(0).isHidden())
        panel.search.setText('贴片电容'); QTest.qWait(180)
        self.assertEqual(panel.tree.topLevelItemCount(), 1)
        panel.tree.setCurrentItem(panel.tree.topLevelItem(0))
        result = QSignalSpy(panel.selected); panel.apply.click()
        self.assertEqual(result.at(0)[0]['id'], '2')
        panel.search.clear(); QTest.qWait(180)
        self.assertFalse(panel.tree.topLevelItem(0).isExpanded())

    def test_introductions_reach_cart_order_and_favorite_data_without_extra_requests(self):
        raw = dict(productCode='C1', productModel='MODEL', productName='32 位微控制器',
                   remark='低功耗，64KB Flash', productGradePlateName='品牌', encapsulationModel='QFN-32',
                   productType='微控制器', productNumber=1, convesionRatio=1, shopCarMapKey='fixture',
                   encapStandard='QFN-32')
        favorite = favorite_products(dict(code=200, result=dict(page=dict(currPage=1, pageRow=20,
            totalRow=1, dataList=[raw]))), 1)[0][0]
        cart = cart_items(dict(code=200, result=dict(shoppingCartVO=dict(rmbCnShoppingCart=dict(
            currentlyProductList=[raw])))))[0]
        payload = fixture_order_detail(dict(uuid='fixture-order', currencyType='RMB'))
        payload['result']['szNormalProductList'] = [raw]
        order = parse_order_details(payload, 'fixture-order')['items'][0]
        for product in (storefront_product(raw), favorite, cart, order):
            self.assertEqual(product['description'], '32 位微控制器\n低功耗，64KB Flash')
            self.assertIn('QFN-32', summary_lines(product)[1])
        panel = self.show(CartPanel()); panel.show_rows([cart])
        detail = self.show(OrderDetailPanel())
        detail.show_result(dict(items=[order], currency='¥', total='', goods_total='', freight=''))
        for table in (panel.table, detail.table):
            item = table.item(0, 2)
            self.assertIn('64KB Flash', item.toolTip())
            self.assertIn('32 位微控制器', item.data(SUMMARY_ROLE)['description'])
            before = table.rowHeight(0)
            font = QFont(table.font()); font.setPixelSize(24); table.setFont(font)
            QTest.qWait(30)
            self.assertGreater(table.rowHeight(0), before)
            self.assertGreaterEqual(table.rowHeight(0), table.fontMetrics().lineSpacing() * 5 + 28)
            if table is panel.table:
                control = table.cellWidget(0, 5)
                self.assertLess(control.height(), table.rowHeight(0) // 2)
            self.assertFalse(table.grab().isNull())

    def test_missing_description_and_stopped_status_translate_without_changing_product_text(self):
        set_language('en_US')
        title, _, description, _ = summary_lines(dict(title='设置', purchase_status='unavailable'))
        self.assertTrue(title.endswith('设置'))
        self.assertIn('No description', description)
        self.assertNotIn('已停售', title)

    def test_cached_product_painting_tracks_content_width_font_language_and_selection(self):
        from lcsc3d.ui.commerce import native_table, table_cell
        from lcsc3d.ui.product_summary import ProductSummaryDelegate
        table = self.show(native_table(['选择', '编号', '商品'], 2))
        table.setRowCount(1)
        item = table_cell(table, 0, 2, 'Part')
        delegate = ProductSummaryDelegate(table)
        option = QStyleOptionViewItem()
        option.widget = table
        def paint():
            target = QImage(520, 230, QImage.Format_ARGB32_Premultiplied)
            target.fill(Qt.transparent)
            painter = QPainter(target)
            try:
                delegate.paint(painter, option, table.model().index(0, 2))
            finally:
                painter.end()
            return target
        previous = None
        for index, (width, size, mode, selected, language) in enumerate((
                (300, 13, 'light', False, 'zh_CN'), (500, 13, 'light', False, 'zh_CN'),
                (300, 24, 'light', False, 'zh_CN'), (300, 24, 'dark', True, 'en_US'),
                (300, 24, 'dark', False, 'en_US'))):
            theme_manager().apply(Preferences(theme_mode=mode, font_size=size))
            set_language(language)
            option.font = QFont(table.font()); option.font.setPixelSize(size)
            option.rect = QRect(0, 0, width, 230)
            option.state = QStyle.State_Enabled | (QStyle.State_Selected if selected else QStyle.State_None)
            set_product_summary(item, dict(title=f'器件 {index}', description='多行商品简介含 Unicode 和 English，' * 12))
            cached = paint()
            self.assertEqual(cached, paint())
            delegate.layouts.clear(); delegate.layout_characters = 0
            self.assertEqual(cached, paint())
            if previous is not None:
                self.assertNotEqual(cached, previous)
            previous = cached
        for index in range(350):
            delegate.text_layout('description ' + str(index) + 'x' * 1000, 250, option.font, 2, cached)
        self.assertLessEqual(len(delegate.layouts), 256)
        self.assertLessEqual(delegate.layout_characters, 131072)

    def test_supplementary_characters_do_not_erase_the_second_description_line(self):
        from lcsc3d.ui.commerce import native_table
        from lcsc3d.ui.product_summary import ProductSummaryDelegate
        table = self.show(native_table(['选择', '编号', '商品'], 2))
        delegate = ProductSummaryDelegate(table)
        font = QFont(table.font())
        font.setPixelSize(13)
        target = QImage(200, 100, QImage.Format_ARGB32_Premultiplied)
        prefix = '😀' * 6 + ' '
        result = delegate.text_layout(prefix + 'tail', QFontMetrics(font).horizontalAdvance(prefix), font, 2, target)
        self.assertEqual(len(result[1]), 2)
        self.assertEqual(result[2], 'tail')


if __name__ == '__main__':
    unittest.main()
