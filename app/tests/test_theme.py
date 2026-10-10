"""Appearance persistence, live translation, and international catalog routing."""
from dataclasses import replace
import json
import ast
import re
from pathlib import Path
import sys
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app_settings import Preferences, ACCENT_COLORS, set_preferences, get_preferences, DEFAULT_FONT_FAMILY
from app_theme import colors, contrast, theme_manager, effective_font_family
from i18n import text, message, render, set_language, language
from localized_widgets import QLabel, QLineEdit, QTableWidget, QTableWidgetItem, QMessageBox, QPushButton
from international_store import (InternationalStoreClient, international_product, catalog_client,
                                 storefront_url, HOME, API)
from store import StoreClient, StoreError
from PySide6.QtCore import Qt
from PySide6.QtGui import QFontDatabase, QPalette
from PySide6.QtWidgets import QApplication

FIXTURE = json.loads((Path(__file__).parent / 'fixtures/international_catalog.json').read_text('utf-8'))


class AppearanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if QApplication.instance() is None:
            QApplication.setAttribute(Qt.AA_ShareOpenGLContexts)
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.original = get_preferences()
        self.original_language = language()
        self.addCleanup(set_preferences, self.original)
        self.addCleanup(set_language, self.original_language)
        self.addCleanup(lambda: theme_manager().apply(self.original))

    def test_new_preferences_validate_and_old_configs_keep_compatible_defaults(self):
        for value in ({}, {'language': [], 'theme_mode': {}, 'accent_color': '#fff'},
                      {'language': 'bad', 'theme_mode': 'bad', 'accent_color': 'red; color:white'},
                      {'accent_text_color': '#fff', 'font_family': []},
                      {'accent_text_color': None, 'font_family': '\nArial'},
                      {'accent_text_color': {}, 'font_family': ' '},
                      {'accent_text_color': [], 'font_family': 'f' * 129}):
            self.assertEqual(Preferences.from_mapping(value), Preferences())
        expected = Preferences(language='en_US', theme_mode='dark', accent_color='#123abc', store_proxy='direct',
                               accent_text_color='#fff1d6', font_family='Segoe UI')
        values = expected.to_mapping()
        values['accent_color'] = '#123ABC'
        values['accent_text_color'] = '#FFF1D6'
        self.assertEqual(Preferences.from_mapping(values), expected)

    def test_explicit_ui_messages_have_english_catalog_entries(self):
        from translations_en import ENGLISH
        missing = set()
        for path in Path(__file__).resolve().parents[1].glob('*.py'):
            for node in ast.walk(ast.parse(path.read_text('utf-8'))):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in ('ui_text', 'ui_message'):
                    if node.args and isinstance(node.args[0], ast.Constant):
                        source = node.args[0].value
                        if isinstance(source, str) and re.search('[\u4e00-\u9fff]', source) and source not in ENGLISH:
                            missing.add(source)
        self.assertEqual(missing, set())

    def test_live_translation_preserves_arguments_editable_contents_and_item_data(self):
        set_language('zh_CN')
        status = QLabel(message('已识别 {0} 个器件', 7))
        foreign_data = QLabel(message('商品原图 · {0} · {1}', 'C2040', '封装'))
        edit = QLineEdit('F:/保存/设置.step')
        table = QTableWidget(1, 1)
        item = QTableWidgetItem(text('等待下载'))
        item.setData(Qt.UserRole, '等待下载')
        item.setCheckState(Qt.Checked)
        table.setItem(0, 0, item)
        set_language('en_US')
        self.assertEqual(status.text(), 'Detected 7 parts')
        self.assertEqual(foreign_data.text(), 'Original images · C2040 · 封装')
        self.assertEqual(edit.text(), 'F:/保存/设置.step')
        self.assertEqual(item.text(), 'Waiting')
        self.assertEqual(item.data(Qt.UserRole), '等待下载')
        self.assertEqual(item.checkState(), Qt.Checked)
        set_language('zh_CN')
        self.assertEqual(status.text(), '已识别 7 个器件')
        self.assertEqual(item.text(), '等待下载')
        for widget in (status, foreign_data, edit, table):
            widget.deleteLater()

    def test_translated_signal_messages_keep_dynamic_values(self):
        set_language('en_US')
        self.assertEqual(render(str(message('商品实物图 · {0} × {1} · 滚轮缩放，拖动平移', 800, 600))),
                         'Product photo · 800 × 600 · Scroll to zoom, drag to pan')
        self.assertEqual(render('F:/设置/模型.step'), 'F:/设置/模型.step')

    def test_import_notice_constructor_translates_title_and_message(self):
        set_language('en_US')
        notice = QMessageBox(QMessageBox.Information, text('加入下载列表成功'),
                             message('元件导入完成：新增 {0} 个，跳过 {1} 个已有元件；下载列表共 {2} 个。', 1, 0, 1), QMessageBox.Ok)
        self.assertEqual(notice.windowTitle(), 'Added to download list')
        self.assertEqual(notice.text(), 'Imported 1 new parts; skipped 0 existing parts. Total: 1.')
        set_language('zh_CN')
        self.assertEqual(notice.windowTitle(), '加入下载列表成功')
        self.assertIn('新增 1 个', notice.text())
        notice.deleteLater()

    def test_theme_follows_system_only_in_system_mode(self):
        manager = theme_manager()
        manager.apply(Preferences(theme_mode='system'), Qt.ColorScheme.Dark)
        self.assertTrue(manager.dark)
        manager.system_changed(Qt.ColorScheme.Light)
        self.assertFalse(manager.dark)
        manager.apply(Preferences(theme_mode='dark'))
        manager.system_changed(Qt.ColorScheme.Light)
        self.assertTrue(manager.dark)
        manager.apply(Preferences(theme_mode='light'))
        manager.system_changed(Qt.ColorScheme.Dark)
        self.assertFalse(manager.dark)

    def test_custom_accent_colors_keep_text_readable_in_both_themes(self):
        for dark in (True, False):
            for accent in [v for _, v in ACCENT_COLORS] + ['#ffffff', '#000000', '#777777', '#ffff00']:
                with self.subTest(dark=dark, accent=accent):
                    c = colors(dark, accent)
                    self.assertGreaterEqual(contrast(c['accent'], c['on_accent']), 4.5)
                    self.assertGreaterEqual(contrast(c['accent_ink'], c['surface']), 4.5)
                    self.assertGreaterEqual(contrast(c['text'], c['surface']), 4.5)

    def test_manual_text_color_reaches_buttons_selection_and_hover_without_being_overridden(self):
        manager = theme_manager()
        button, selected = QPushButton('Download'), QPushButton('3D')
        button.setObjectName('primary')
        selected.setObjectName('previewMode')
        selected.setCheckable(True)
        selected.setChecked(True)
        for widget in (button, selected):
            widget.resize(140, 42)
        for mode, accent in (('light', '#168878'), ('dark', '#ffffff'), ('light', '#000000')):
            manager.apply(Preferences(theme_mode=mode, accent_color=accent, accent_text_color='#fff1d6'))
            for widget in (button, selected):
                widget.ensurePolished()
                # A :checked QSS color is resolved during painting; QWidget's
                # ordinary palette still describes the unchecked state.
                image = widget.grab().toImage()
                matching = sum(max(abs(channel - wanted) for channel, wanted in
                                   zip(image.pixelColor(x, y).getRgb()[:3], (255, 241, 214))) < 18
                               for y in range(image.height()) for x in range(image.width()))
                self.assertGreater(matching, 5, (mode, accent, widget.objectName()))
            self.assertEqual(manager.tokens['on_accent_hover'], '#fff1d6')
            self.assertEqual(self.app.palette().color(QPalette.HighlightedText).name(), '#fff1d6')
        manager.apply(Preferences())
        self.assertEqual(manager.tokens['on_accent'], '#000000')
        for widget in (button, selected):
            widget.deleteLater()

    def test_font_change_updates_existing_widgets_even_when_colors_are_unchanged(self):
        manager = theme_manager()
        first = effective_font_family(DEFAULT_FONT_FAMILY)
        second = next(f for f in QFontDatabase.families() if f != first and not f.startswith('@'))
        button = QPushButton('Download')
        label = QLabel('LCSC3D Aa 123')
        manager.apply(Preferences(font_family=first))
        manager.apply(Preferences(font_family=second))
        for widget in (button, label):
            widget.ensurePolished()
            self.assertEqual(widget.font().family(), second)
        self.assertEqual(manager.tokens['font_family'], second)
        manager.apply(Preferences())
        self.assertEqual(label.font().family(), first)
        self.assertEqual(label.font().pixelSize(), 13)
        for widget in (button, label):
            widget.deleteLater()

    def test_font_missing_on_another_computer_falls_back_to_an_installed_font(self):
        value = Preferences(font_family='LCSC3D missing font 7b972d9')
        theme_manager().apply(value)
        expected = effective_font_family(DEFAULT_FONT_FAMILY)
        self.assertEqual(theme_manager().tokens['font_family'], expected)
        self.assertEqual(self.app.font().family(), expected)

    def test_font_change_reflows_open_product_descriptions_and_parameter_rows(self):
        from favorites import DetailLabel, ParameterTable
        label = DetailLabel('LCSC3D component parameter with long text. ' * 24)
        label.resize(210, 30)
        table = ParameterTable()
        table.setRowCount(1)
        table.setItem(0, 0, QTableWidgetItem('Long parameter'))
        table.setItem(0, 1, QTableWidgetItem('Long parameter details abcdef0123456789 ' * 10))
        table.resize(320, 240)
        label.show()
        table.show()
        try:
            for family in ('Segoe UI', 'Consolas'):
                theme_manager().apply(Preferences(font_family=family))
                for _ in range(10):
                    self.app.processEvents()
                self.assertGreaterEqual(label.height(), label.heightForWidth(label.width()))
                before = table.rowHeight(0)
                table.resizeRowsToContents()
                self.assertEqual(before, table.rowHeight(0))
        finally:
            for widget in (label, table):
                widget.close()
                widget.deleteLater()

    def test_language_routes_links_and_clients_without_reusing_mainland_urls(self):
        set_preferences(Preferences(language='en_US'))
        self.assertEqual(storefront_url('C2040', 'https://item.szlcsc.com/123.html'), HOME + '/product-detail/C2040.html')
        self.assertIsInstance(catalog_client(), InternationalStoreClient)
        set_preferences(Preferences(language='zh_CN'))
        self.assertEqual(storefront_url('C2040', 'https://item.szlcsc.com/123.html'), 'https://item.szlcsc.com/123.html')
        self.assertIsInstance(catalog_client(), StoreClient)


class InternationalCatalogTests(unittest.TestCase):
    def client(self, responses):
        session = MagicMock()
        session.request.side_effect = [(json.dumps(value).encode(), API) if isinstance(value, dict) else (value, HOME)
                                       for value in responses]
        return InternationalStoreClient(session), session

    def page(self, rows, page=1, total=52):
        return {'code': 200, 'result': {'currPage': page, 'pageRow': 50, 'actualTotalRow': total, 'dataList': rows}}

    def discovery(self):
        return {'code': 200, 'result': {'scene': 'FULL_MATCH', 'catalogVOS': [
            {'catalogId': 941, 'catalogNameEn': 'Microcontrollers', 'productNum': 52},
            {'catalogId': 559, 'catalogNameEn': 'RF transceivers', 'productNum': 2}]}}

    def test_international_product_uses_english_parameters_usd_and_original_images(self):
        product = international_product(FIXTURE['product'])
        self.assertEqual(product['part'], 'C2040')
        self.assertEqual(product['title'], 'RP2040')
        self.assertEqual(product['currency'], 'USD')
        self.assertEqual(product['price_tiers'][0]['price'], '0.9975')
        self.assertIn(('Number of I/O', '30'), product['parameters'])
        self.assertTrue(all(url.startswith('https://assets.lcsc.com/') for url in product['images']))
        self.assertNotIn('szlcsc.com', json.dumps(product))

    def test_exact_part_search_reads_the_international_product_page(self):
        html = '<script id="__NEXT_DATA__">' + json.dumps({'props': {'pageProps': {'webData': FIXTURE['product']}}}) + '</script>'
        client, session = self.client([FIXTURE['redirect'], html.encode()])
        result = client.search_results_page('C2040')
        self.assertEqual((result['total'], result['items'][0]['part']), (1, 'C2040'))
        self.assertEqual(session.request.call_args_list[1].args[0], HOME + '/product-detail/C2040.html')

    def test_category_and_pagination_requests_stay_on_international_endpoints(self):
        client, session = self.client([self.discovery(), self.page(FIXTURE['search_rows']),
                                      self.page(FIXTURE['search_rows'], page=2), self.page(FIXTURE['search_rows'], total=2)])
        first = client.search_results_page('STM32')
        second = client.search_results_page('STM32', page=2, catalog_id=941)
        switched = client.search_results_page('STM32', catalog_id=559)
        self.assertEqual((first['catalog_id'], second['page'], switched['catalog_id']), (941, 2, 559))
        self.assertEqual(len(first['categories']), 2)
        calls = session.request.call_args_list
        self.assertEqual(len(calls), 4)  # Discovery is reused across pages/categories.
        self.assertEqual(json.loads(calls[-1].kwargs['data'])['catalogIdList'], [559])
        self.assertTrue(all(c.args[0].startswith(API + '/') for c in calls))
        self.assertTrue(all(c.kwargs['headers']['Referer'] == HOME + '/' for c in calls))

    def test_bad_pagination_is_reported_instead_of_showing_a_wrong_page(self):
        client, _ = self.client([self.discovery(), self.page(FIXTURE['search_rows'], page=1)])
        with self.assertRaises(StoreError):
            client.search_results_page('STM32', page=2)

    def test_foreign_prices_do_not_fall_back_to_mainland_cny_amounts(self):
        raw = dict(FIXTURE['product'], productPriceList=[{'ladder': 1, 'productPrice': '7.20'}])
        self.assertEqual(international_product(raw)['price_tiers'], [])

    def test_public_image_client_rejects_unrelated_and_mainland_hosts(self):
        session = MagicMock()
        client = InternationalStoreClient(session)
        for url in ('https://assets.lcsc.com.example.org/x.jpg', 'https://alimg.szlcsc.com/x.jpg', 'file:///secret'):
            self.assertEqual(client.image(url), b'')
        session.request.assert_not_called()


if __name__ == '__main__':
    unittest.main()
