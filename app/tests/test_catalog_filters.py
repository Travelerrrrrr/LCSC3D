"""Official facet schemas, native transitions, cancellation and bounded photo work."""
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.parse import unquote

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PySide6.QtCore import Qt, QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication, QHeaderView
from lcsc3d.core import app_logging as logs
from lcsc3d.services.catalog_filters import (parse_categories, search_facets, parameter_facets, normalize_filters,
                                            search_payload, read_categories, read_parameters)
from lcsc3d.services.store import StoreClient, StoreError, search_page_products
from lcsc3d.selftests.catalog_fixture import categories, parameters, add_facets
from lcsc3d.selftests.favorites_selftest import OfflineStore, fixture_search
from lcsc3d.ui.favorites import FavoritesDialog


class FilterDataTests(unittest.TestCase):
    def test_category_headings_and_keyword_links_keep_only_real_electronics_ids(self):
        payload = categories(); rows = payload['result']['chips']['sonCatalogList']
        rows.append({'catalogId': None, 'catalogName': '工具', 'sonCatalogList': [
            {'catalogId': 11532, 'catalogName': '卷尺'}, {'catalogId': None, 'catalogName': '工业品', 'mroCatalogId': 123}]})
        rows[0]['keywordVOList'] = [{'keywordName': '电阻', 'keywordUrl': 'https://list.szlcsc.com/catalog/308.html'},
                                   {'keywordName': 'bad', 'keywordUrl': 'https://example.com/catalog/9.html'}]
        result = parse_categories(payload)[0]['children']
        self.assertEqual(result[1]['id'], '')
        self.assertEqual([c['id'] for c in result[1]['children']], ['11532'])
        self.assertEqual(result[0]['children'][-1]['id'], '308')
        with self.assertRaises(StoreError): parse_categories({'code': 200, 'result': []})

    def test_native_payload_encoding_multiselect_and_range_semantics(self):
        request = search_payload('STM32', 2, {'catalogIdFilter': '11329', 'brandIdFilter': '74$100',
            'standardFilter': 'LQFP-48(7x7)', 'spotFilter': 0, 'hasDataFile': True, 'sortNumber': 1,
            'startPrice': '0.0123', 'endPrice': '2.2', 'demandNumber': '100',
            'parameters': {'param_31013_n': ['64KB', '128KB'], 'param_31011_n#range': ['32MHz~72MHz']}})
        self.assertEqual(request['currentPage'], 2); self.assertEqual(request['pageSize'], 30)
        self.assertEqual(request['brandIdFilter'], '74$100'); self.assertEqual(request['spotFilter'], 0)
        self.assertEqual(unquote(request['queryParameterValue']), '64KB@param_31013_n$128KB@param_31013_n$32MHz~72MHz@param_31011_n#range')
        self.assertEqual(normalize_filters({'catalogIdFilter': '1$2', 'parameters': {'param_x': ['v']}})['parameters'], {})
        for bad in ({'startPrice': '1.00001'}, {'startPrice': '2', 'endPrice': '1'}, {'demandNumber': '0'},
                    {'catalogIdFilter': '1/malicious'}, {'sortNumber': 999}, {'parameters': {'bad': ['a']}}):
            with self.subTest(bad=bad), self.assertRaises(StoreError): normalize_filters(bad)

    def test_search_and_category_facets_preserve_codes_units_disabled_options(self):
        result = search_facets(add_facets(fixture_search([('C2040', 'RP2040')])))
        self.assertEqual(result[0]['options'][0]['value'], '11329')
        self.assertEqual(result[1]['options'][0]['value'], '100')
        for data in (parameters(), parameters(category=True)):
            group = next(g for g in parameter_facets(data) if g['key'] == 'param_31013_n')
            self.assertEqual(group['units']['KB'], 1024)
            self.assertTrue(group['numeric']); self.assertFalse(group['options'][-1]['enabled'])

    def test_every_native_page_keeps_filters_and_official_sort_order(self):
        calls = []
        def reply(url, **kwargs):
            request = kwargs['payload']; calls.append((url, request))
            page = request['currentPage']; start = (page - 1) * 30
            return add_facets(fixture_search([(f'C{1000+i}', str(i)) for i in range(start, start+30)], page, 90))
        client = StoreClient()
        with patch.object(client, '_json', side_effect=reply):
            result = client.search_results_page('', 2, filters={'catalogIdFilter': '11329', 'sortNumber': 2})
        self.assertEqual([p['part'] for p in result['items']], [f'C{i}' for i in range(1020, 1040)])
        self.assertEqual(len(calls), 2)
        for url, request in calls:
            self.assertEqual(url, 'https://list.szlcsc.com/category/product')
            self.assertEqual(request['catalogIdFilter'], '11329'); self.assertEqual(request['sortNumber'], 2)


class FilterUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if QApplication.instance() is None:
            QApplication.setAttribute(Qt.AA_ShareOpenGLContexts)
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(); self.addCleanup(self.folder.cleanup)
        env = patch.dict(os.environ, LOCALAPPDATA=self.folder.name); env.start(); self.addCleanup(env.stop)
        logs.configure_logging(version='2.4.1'); self.addCleanup(logs.close_logging)
        self.service = OfflineStore(); self.addCleanup(self.service.close)
        self.client = self.service.client()
        self.dialog = FavoritesDialog(client_factory=lambda: self.client, vault=False)
        self.dialog.setAttribute(Qt.WA_DontShowOnScreen)
        self.dialog.request_detail = lambda *_: None
        self.dialog.show(); self.addCleanup(self.close)

    def wait(self, predicate):
        end = time.monotonic() + 8
        while not predicate() and time.monotonic() < end:
            self.app.processEvents(); time.sleep(.005)
        self.assertTrue(predicate())

    def close(self):
        self.dialog.shutdown(); self.dialog.close(); self.wait(lambda: not self.dialog.has_jobs())
        self.dialog.deleteLater(); QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete); self.app.processEvents()

    def test_categories_load_on_demand_and_filter_apply_stays_inside_search(self):
        d = self.dialog
        self.assertFalse(self.service.requests)
        d.category_button.click(); self.wait(lambda: bool(d.categories))
        self.assertEqual(d.search_content.currentWidget(), d.category_panel)
        self.assertFalse(d.category_panel.isWindow())
        d.category_panel.tree.setCurrentItem(d.category_panel.tree.topLevelItem(0).child(0)); d.category_panel.apply.click()
        self.wait(lambda: not d.searching and bool(d.search_items))
        self.assertEqual(d.search_content.currentWidget(), d.search_results)
        d.search_table.item(0, 0).setCheckState(Qt.Checked)
        d.filter_button.click(); self.wait(lambda: bool(d.filter_cache))
        panel = d.filter_panel
        panel.groups.setCurrentRow(next(i for i, g in enumerate(panel.facets) if g['key'] == 'param_31013_n'))
        panel.options.item(0).setCheckState(Qt.Checked); panel.flags['spotFilter'].setChecked(True)
        panel.apply.click(); self.wait(lambda: not d.searching)
        self.assertEqual(d.search_content.currentWidget(), d.search_results)
        self.assertIn('C2040', d.search_selected)
        requests = [json.loads(row[3]) for row in self.service.requests if row[1] == '/category/product']
        self.assertIn('64KB@param_31013_n', unquote(requests[-1]['queryParameterValue']))
        self.assertEqual(requests[-1]['spotFilter'], 0)

    def test_range_selection_clear_and_query_validation(self):
        panel = self.dialog.filter_panel
        panel.begin({'catalogIdFilter': '11329'}, parameter_facets(parameters()))
        emitted = []; panel.applied.connect(emitted.append)
        panel.low.setText('64'); panel.high.setText('128'); panel.unit.setCurrentText('KB'); panel.range_apply.click()
        self.assertEqual(panel.draft['parameters']['param_31013_n#range'], ['64KB~128KB'])
        self.assertIn('64KB~128KB', panel.range_choice.text())
        panel.inputs['startPrice'].setText('3'); panel.inputs['endPrice'].setText('2'); panel.apply.click()
        self.assertFalse(emitted)
        panel.inputs['endPrice'].setText('4'); panel.apply.click(); self.assertEqual(len(emitted), 1)
        panel.clear_group(); self.assertNotIn('param_31013_n#range', panel.draft['parameters'])

    def test_filter_requests_are_debounced_cached_and_cancelled_on_back(self):
        d = self.dialog; d.search_keyword = 'fixture'; d.open_filters()
        for price in ('1', '2', '3'):
            d.queue_filters({'startPrice': price})
        self.wait(lambda: bool(d.filter_cache))
        before = len([r for r in self.service.requests if r[1] == '/query/product'])
        self.assertEqual(before, 1)
        d.queue_filters({'startPrice': '3'}); d.filter_timer.stop(); d.load_filter_options()
        self.assertEqual(len([r for r in self.service.requests if r[1] == '/query/product']), before)
        d.queue_filters({'startPrice': '4'}); d.close_catalog_panel()
        self.assertFalse(d.filter_timer.isActive())
        self.assertEqual(d.search_content.currentWidget(), d.search_results)

    def test_quantity_index_survives_removal_and_column_width_survives_resize(self):
        d = self.dialog; table = d.favorite_table
        for part in ('C1', 'C2', 'C3'):
            d.append_row(table, dict(part=part, title=part, min_quantity=5, quantity_step=5))
        table.removeRow(0); d.set_purchase_quantity('C3', 15)
        self.assertEqual(table.cellWidget(1, table.quantity_column).value(), 15)
        self.assertNotIn('C1', table.part_indexes)
        self.assertEqual(table.horizontalHeader().sectionResizeMode(2), QHeaderView.Interactive)
        table.setColumnWidth(2, 310); d.resize(1200, 760); self.app.processEvents()
        self.assertEqual(table.columnWidth(2), 310)

    def test_favorite_photos_only_request_visible_rows(self):
        d = self.dialog; d.tabs.setCurrentIndex(1)
        for n in range(100):
            d.append_row(d.favorite_table, dict(part=f'C{n+1}', title='Fixture', image=f'https://alimg.szlcsc.com/{n}.png'))
        self.wait(lambda: not d.favorite_table.item(0, d.favorite_table.thumbnail_column).icon().isNull())
        self.wait(lambda: not d.has_jobs())
        self.assertLessEqual(len([r for r in self.service.requests if r[1].endswith('.png')]), 20)
        d.favorite_table.verticalScrollBar().setValue(d.favorite_table.verticalScrollBar().maximum())
        self.wait(lambda: not d.favorite_table.item(99, d.favorite_table.thumbnail_column).icon().isNull())
        self.assertLessEqual(len(d.thumbnail_cache), 40)

    def test_filtered_sparse_favorites_load_every_visible_photo(self):
        d = self.dialog; d.tabs.setCurrentIndex(1)
        for n in range(100):
            d.append_row(d.favorite_table, dict(part=f'C{n+1}', title='Match' if n in (0, 50, 99) else 'Other',
                                              image=f'https://alimg.szlcsc.com/sparse-{n}.png'))
        d.filter_input.setText('Match')
        self.wait(lambda: all(not d.favorite_table.item(n, d.favorite_table.thumbnail_column).icon().isNull() for n in (0, 50, 99)))
        self.assertEqual(len([r for r in self.service.requests if 'sparse-' in r[1]]), 3)

    def test_log_fields_have_counts_and_timing_without_query_or_filter_values(self):
        d = self.dialog; d.search_keyword = 'PRIVATE_QUERY_MARKER'; d.queue_filters({'secondKeyword': 'PRIVATE_FILTER_MARKER'})
        d.show_catalog_panel(d.filter_panel); d.filter_timer.stop(); d.load_filter_options()
        self.wait(lambda: bool(d.filter_cache))
        log_path = logs.get_log_directory() / 'LCSC3D.log'
        logs.close_logging()
        text = log_path.read_text('utf-8')
        self.assertIn('catalog.facets_loaded', text); self.assertIn('elapsed_ms', text)
        self.assertNotIn('PRIVATE_QUERY_MARKER', text); self.assertNotIn('PRIVATE_FILTER_MARKER', text)

    def test_positional_cancellation_keeps_search_diagnostic_metadata(self):
        self.client.search_results_page('PRIVATE_QUERY_MARKER', 1, threading.Event())
        log_path = logs.get_log_directory() / 'LCSC3D.log'; logs.close_logging()
        records = [json.loads(line) for line in log_path.read_text('utf-8').splitlines()]
        started = next(row for row in records if row['event'] == 'catalog.search_page.started')
        self.assertEqual(started['page'], 1)
        self.assertEqual(started['query_length'], len('PRIVATE_QUERY_MARKER'))
