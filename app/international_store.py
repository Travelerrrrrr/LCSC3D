"""Public LCSC international catalog. No mainland account cookies or price fallback."""
import json
from urllib.parse import urlsplit, urlencode

from app_settings import get_preferences
from store import (MemorySession, StoreClient, StoreError, PART, SEARCH_PAGE_SIZE,
                   _PageData, text, public_url, quantity, decimal_number, check_cancelled)

HOME = 'https://www.lcsc.com'
API = 'https://wmsc.lcsc.com'
HEADERS = {'Origin': HOME, 'Referer': HOME + '/', 'Accept-Language': 'en-US,en;q=0.9'}


def storefront_url(part='', domestic_url=''):
    if get_preferences().language == 'en_US':
        return HOME + '/product-detail/' + part + '.html' if PART.fullmatch(part) else HOME
    return domestic_url or 'https://so.szlcsc.com/global.html?' + urlencode({'k': part})


def catalog_client():
    return InternationalStoreClient() if get_preferences().language == 'en_US' else StoreClient()


def international_product(raw):
    if not isinstance(raw, dict) or not PART.fullmatch(str(raw.get('productCode', ''))):
        raise StoreError('The international catalog returned an invalid product.')
    part = raw['productCode']
    images = raw.get('productImages') or [raw.get('productImageUrlBig') or raw.get('productImageUrl')]
    images = list(dict.fromkeys(public_url(value) for value in images if isinstance(value, str) and public_url(value)))
    params = [(text(p.get('paramNameEn'), 160), text(p.get('paramValueEn'), 16384))
              for p in (raw.get('paramVOList') or []) if isinstance(p, dict) and p.get('paramNameEn')]
    tiers = []
    if raw.get('isShowForeignPrice') is not False:
        for row in (raw.get('productPriceList') or [])[:100]:
            if not isinstance(row, dict):
                continue
            start, price = quantity(row.get('ladder')), decimal_number(row.get('usdPrice'))
            if start and price is not None:
                tiers.append({'quantity': start, 'end': None, 'price': format(price, 'f')})
    return {'part': part, 'product_id': str(raw.get('productId') or ''),
            'title': text(raw.get('productModel')), 'manufacturer': text(raw.get('brandNameEn')),
            'package': text(raw.get('encapStandard')), 'category': text(raw.get('wmCatalogNameEn') or raw.get('catalogName')),
            'description': text(raw.get('productIntroEn') or raw.get('productNameEn'), 16384),
            'parameters': params, 'images': images, 'image': images[0] if images else '',
            'datasheet': public_url(raw.get('pdfUrl')), 'store_url': HOME + '/product-detail/' + part + '.html',
            'stock': quantity(raw.get('stockNumber')), 'min_quantity': quantity(raw.get('minBuyNumber')) or 1,
            'unit': text(raw.get('productUnit')) or 'Piece', 'price_tiers': sorted(tiers, key=lambda p: p['quantity']),
            'currency': 'USD', 'currency_symbol': 'US$', 'detail_source': 'LCSC International', 'resources': {}}


def categories_from(rows):
    categories = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        children = row.get('childCatelogs') or row.get('childCatalogs')
        if children:
            categories.extend(categories_from(children))
        elif isinstance(row.get('catalogId'), int):
            categories.append({'id': row['catalogId'], 'name': text(row.get('catalogNameEn')),
                               'count': quantity(row.get('productNum')) or 0})
    return categories


class InternationalStoreClient:
    international = True

    def __init__(self, session=None):
        self.session = session or MemorySession()
        self.account = None
        self.pages_read = 0
        self.discovery = {}

    def _json(self, path, payload, cancelled):
        body, _ = self.session.request(API + path, method='POST', data=json.dumps(payload).encode('utf-8'),
            headers={**HEADERS, 'Content-Type': 'application/json'}, cancelled=cancelled)
        try:
            value = json.loads(body)
            if value.get('code') != 200 or not isinstance(value.get('result'), dict):
                raise ValueError()
            return value['result']
        except (ValueError, AttributeError):
            raise StoreError('LCSC International could not return this search. Please retry or refine the keyword.') from None

    def product(self, product, cancelled=None):
        part = product.get('part', '') if isinstance(product, dict) else str(product).strip().upper()
        if not PART.fullmatch(part):
            raise StoreError('Invalid LCSC part number.')
        body, _ = self.session.request(HOME + '/product-detail/' + part + '.html', headers=HEADERS, cancelled=cancelled)
        parser = _PageData()
        try:
            parser.feed(body.decode('utf-8'))
            raw = json.loads(''.join(parser.chunks))['props']['pageProps']['webData']
            result = international_product(raw)
            if result['part'] != part:
                raise ValueError()
            return result
        except (ValueError, KeyError, TypeError):
            raise StoreError('LCSC International has no readable details for this part. Open its product page or retry.') from None

    def search_results_page(self, keyword, page=1, cancelled=None, *, catalog_id=None):
        keyword = keyword.strip()[:160]
        if not keyword or type(page) is not int or page < 1:
            raise StoreError('Enter a keyword and a valid page number.')
        if keyword not in self.discovery:
            result = self._json('/ftps/wm/search/v3/global', {'keyword': keyword}, cancelled)
            check_cancelled(cancelled)
            self.discovery = {keyword: result}
        result = self.discovery[keyword]
        if result.get('isToDetail'):
            part = (result.get('tipProductDetailUrlVO') or {}).get('productCode', '')
            if page != 1:
                raise StoreError('This search has only one page.')
            return dict(items=[self.product(part, cancelled)], page=1, pages=1, total=1, size=SEARCH_PAGE_SIZE, categories=[])
        categories = sorted(categories_from(result.get('catalogVOS')), key=lambda c: -c['count'])
        if categories:
            selected = next((c for c in categories if c['id'] == catalog_id), categories[0])
            payload = {'keyword': '', 'globalKeyword': keyword, 'catalogIdList': [selected['id']],
                       'brandIdList': [], 'encapValueList': [], 'isStock': False, 'isOtherSuppliers': False,
                       'isAsianBrand': False, 'isDeals': False, 'isRohsCert': False, 'paramNameValueMap': {},
                       'currentPage': page, 'pageSize': SEARCH_PAGE_SIZE}
            if result.get('scene') == 'FULL_MATCH':
                payload['scene'] = 'FULL_MATCH'
            values = self._json('/ftps/wm/product/query/list', payload, cancelled)
            total = quantity(values.get('actualTotalRow', values.get('totalRow')))
            if values.get('currPage') != page or values.get('pageRow') != SEARCH_PAGE_SIZE or total is None:
                raise StoreError('The international catalog pagination changed. Please search again.')
            rows = values.get('dataList')
        elif result.get('productSearchResultVO') is not None:
            values = self._json('/ftps/wm/search/v3/global', {'keyword': keyword, 'currentPage': page, 'pageSize': SEARCH_PAGE_SIZE}, cancelled)
            rows = (values.get('productSearchResultVO') or {}).get('productList')
            total = quantity(values.get('totalCount'))
        elif result.get('scene') == 'NO_RESULT' or result.get('totalCount') == 0:
            rows, total = [], 0
        else:
            raise StoreError('Please enter a part number or a more specific product keyword.')
        if not isinstance(rows, list) or total is None or len(rows) > SEARCH_PAGE_SIZE:
            raise StoreError('The international catalog returned an incomplete page. Please retry.')
        pages = (total + SEARCH_PAGE_SIZE - 1) // SEARCH_PAGE_SIZE
        if page > max(1, pages):
            raise StoreError('This search page no longer exists. Please search again.')
        products = [international_product(row) for row in rows]
        if len({p['part'] for p in products}) != len(products):
            raise StoreError('The international catalog returned duplicate products. Please retry.')
        check_cancelled(cancelled)
        return dict(items=products, total=total, page=page, pages=pages, size=SEARCH_PAGE_SIZE,
                    categories=categories, catalog_id=selected['id'] if categories else None)

    def image(self, url, cancelled=None):
        url = public_url(url)
        host = urlsplit(url).hostname or ''
        if not (host == 'lcsc.com' or host.endswith('.lcsc.com')):
            return b''
        return self.session.request(url, headers=HEADERS, cancelled=cancelled, limit=16 * 1024 * 1024)[0]

    def clear(self):
        self.session.clear()
