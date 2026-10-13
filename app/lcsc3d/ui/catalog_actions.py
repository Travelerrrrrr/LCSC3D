"""Cancellable, on-demand catalog navigation and facet requests."""
import json
from collections import OrderedDict
from PySide6.QtCore import QTimer
from lcsc3d.core.i18n import text as tr, message as msg
from lcsc3d.core.app_logging import log_event
from lcsc3d.services.catalog_filters import read_categories, read_parameters, filter_count, normalize_filters
from lcsc3d.ui.catalog_filters import CategoryPanel, FilterPanel


class CatalogActions:
    def setup_catalog(self):
        self.catalog_filters = {}; self.search_facets = []; self.categories = []
        self.filter_cache = OrderedDict(); self.filter_pending = None
        self.category_panel = CategoryPanel(); self.filter_panel = FilterPanel()
        self.search_content.addWidget(self.category_panel); self.search_content.addWidget(self.filter_panel)
        self.category_button.clicked.connect(self.open_categories); self.filter_button.clicked.connect(self.open_filters)
        self.category_panel.back_requested.connect(self.close_catalog_panel)
        self.filter_panel.back_requested.connect(self.close_catalog_panel)
        self.category_panel.refresh_requested.connect(self.load_categories)
        self.category_panel.selected.connect(self.browse_category)
        self.filter_panel.changed.connect(self.queue_filters); self.filter_panel.applied.connect(self.apply_catalog_filters)
        self.filter_panel.refresh_requested.connect(self.reload_filters)
        self.filter_timer = QTimer(self); self.filter_timer.setSingleShot(True); self.filter_timer.setInterval(350)
        self.filter_timer.timeout.connect(self.load_filter_options)
        self.category_button.setVisible(not self.international); self.filter_button.setVisible(not self.international)

    def show_catalog_panel(self, panel):
        self.search_content.setCurrentWidget(panel); self.search_hint.setVisible(panel is self.search_results)
        self.update_count()

    def close_catalog_panel(self):
        self.filter_timer.stop(); self.jobs.cancel('filter-options'); self.jobs.cancel('catalog-categories')
        self.show_catalog_panel(self.search_results)

    def open_categories(self):
        self.filter_timer.stop(); self.jobs.cancel('filter-options'); self.show_catalog_panel(self.category_panel)
        if not self.categories:
            self.load_categories()

    def load_categories(self):
        self.category_panel.hint.setText(tr('正在读取商城分类…')); self.category_panel.refresh.setEnabled(False)
        client = self.catalog
        self.jobs.start('catalog-categories', lambda stop, progress: read_categories(client, stop), self.categories_loaded, self.categories_failed)

    def categories_loaded(self, rows):
        self.categories = rows; self.category_panel.show_categories(rows)

    def categories_failed(self, error):
        self.category_panel.hint.setText(str(error)); self.category_panel.refresh.setEnabled(True)

    def browse_category(self, category):
        self.search_input.clear(); self.search_keyword = ''; self.search_selected.clear()
        self.search_facets = []
        self.category_button.setToolTip(category['name']); self.apply_catalog_filters({'catalogIdFilter': category['id']})

    def open_filters(self):
        self.jobs.cancel('catalog-categories'); self.filter_panel.begin(self.catalog_filters, self.search_facets)
        self.show_catalog_panel(self.filter_panel)
        if not (self.search_keyword or self.catalog_filters.get('catalogIdFilter')):
            self.filter_panel.hint.setText(tr('先搜索商品或选择分类，再筛选当前范围。')); self.filter_panel.apply.setEnabled(False)
        else:
            self.queue_filters(self.catalog_filters)

    def reload_filters(self):
        self.filter_cache.clear(); self.queue_filters(self.filter_panel.draft)

    def queue_filters(self, value):
        self.jobs.cancel('filter-options'); self.filter_pending = normalize_filters(value)
        if self.search_keyword or self.filter_pending.get('catalogIdFilter'):
            self.filter_panel.hint.setText(tr('正在更新筛选选项…')); self.filter_timer.start()

    def load_filter_options(self):
        value, keyword = self.filter_pending, self.search_keyword
        if value is None or self.search_content.currentWidget() is not self.filter_panel:
            return
        cache_key = json.dumps([keyword, value], sort_keys=True, ensure_ascii=False)
        if cache_key in self.filter_cache:
            self.filters_loaded(cache_key, self.filter_cache[cache_key]); return
        client = self.catalog
        current = None
        if self.search_page and not self.searching and value == normalize_filters(self.catalog_filters):
            current = dict(facets=self.search_facets, total=self.search_total)
        def read(stop, progress):
            page = current or client.search_page(keyword, 1, stop, filters=value)
            facets = page.get('facets', [])
            return dict(facets=facets + read_parameters(client, keyword, value, facets, stop), total=page['total'])
        self.jobs.start('filter-options', read, lambda result: self.filters_loaded(cache_key, result), self.filters_failed)

    def filters_loaded(self, cache_key, result):
        self.filter_cache[cache_key] = result; self.filter_cache.move_to_end(cache_key)
        while len(self.filter_cache) > 6:
            self.filter_cache.popitem(last=False)
        self.filter_panel.show_facets(result['facets'])
        self.filter_panel.hint.setText(msg('匹配 {0:,} 个商品 · 同一项内多选为「或」，不同项之间为「且」。', result['total']))
        log_event('INFO', 'catalog.facets_loaded', count=len(result['facets']), total=result['total'])

    def filters_failed(self, error):
        self.filter_panel.hint.setText(str(error) + tr(' 已保留所选条件，可更新选项重试。'))

    def apply_catalog_filters(self, filters):
        value = normalize_filters(filters)
        if not (self.search_keyword or value['catalogIdFilter']):
            self.filter_panel.hint.setText(tr('先搜索商品或选择分类，再筛选当前范围。')); return
        self.close_catalog_panel(); self.jobs.cancel('search'); self.jobs.cancel('thumbnails')
        self.catalog_filters = value; self.searching = False; self.search_page = self.search_pages = 0
        self.refresh_filter_buttons()
        log_event('INFO', 'catalog.filters_applied', count=filter_count(value), query_length=len(self.search_keyword),
                  sort=value['sortNumber'], parameter_count=len(value['parameters']))
        self.load_search_page(1)

    def refresh_filter_buttons(self):
        count = filter_count(self.catalog_filters)
        self.filter_button.setText(msg('筛选 ({0})', count) if count else tr('筛选'))
        self.category_button.setText(tr('分类（已选）') if self.catalog_filters.get('catalogIdFilter') else tr('分类'))
