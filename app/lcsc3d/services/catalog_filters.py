"""Official catalog trees and server-side product facets for the native store."""
import json
import re
from decimal import Decimal, InvalidOperation
from urllib.parse import quote, urlsplit

from lcsc3d.services.store import StoreError, text, product_id, decimal_number, quantity, check_cancelled
from lcsc3d.core.app_logging import traced, log_event

CATEGORIES_URL = 'https://staticdata.szlcsc.com/common/data/resource-catalog-list-new.json'
CATEGORY_PRODUCTS_URL = 'https://list.szlcsc.com/category/product'
PARAMETERS_URL = 'https://so.szlcsc.com/query/param/agg?sign=query/product'
GROUPS = (('catalogIdFilter', '类目', 'catalogGroup', 'value'),
          ('brandIdFilter', '品牌', 'gradePlateGroup', 'value'),
          ('standardFilter', '封装', 'encapsulationGroup', 'label'),
          ('arrangeFilter', '包装', 'arrangeGroup', 'label'),
          ('labelFilter', '标签', 'labelGroup', 'label'),
          ('authenticationFilter', '认证信息', 'authenticationGroup', 'label'))
CATEGORY_FIELDS = {'queryProductGradePlateId': 'brandIdFilter', 'queryProductStandard': 'standardFilter',
    'queryPlaceProduction': 'brandPlaceFilter', 'queryProductLabel': 'labelFilter',
    'queryProductAuthentication': 'authenticationFilter', 'queryProductArrange': 'arrangeFilter',
    'querySmtLabel': 'smtLabelFilter', 'queryProductTypeCode': 'queryProductTypeCode'}
DEFAULTS = {**{field: '' for field, *_ in GROUPS}, 'sortNumber': 0, 'satisfyStockType': '',
    'startPrice': '', 'endPrice': '', 'demandNumber': '', 'spotFilter': 1, 'discountFilter': 1,
    'hasDataFile': False, 'brandPlaceFilter': '', 'brandOriginFilter': '', 'secondKeyword': '',
    'smtLabelFilter': '', 'queryProductTypeCode': '', 'lastParamName': ''}
SORTS = [('综合排序', 0), ('价格从低到高', 1), ('价格从高到低', 2), ('销量优先', 6),
         ('库存从高到低', 8), ('库存从低到高', 9), ('广东仓库存从高到低', 10),
         ('广东仓库存从低到高', 11), ('江苏仓库存从高到低', 12), ('江苏仓库存从低到高', 13)]


def finite_number(value):
    try:
        number = Decimal(str(value))
        if not number.is_finite() or number.copy_abs() > Decimal('1e30') or number and number.adjusted() < -30:
            return None
        return number if number else Decimal(0)
    except InvalidOperation:
        return None


def range_input(value, units):
    """Restore the single-unit range emitted by the native filter editor."""
    if value.count('~') != 1:
        return None
    low, high = value.split('~')
    if not (low or high):
        return None
    for unit in sorted(units, key=len, reverse=True):
        if unit and all(not part or part.endswith(unit) and finite_number(part[:-len(unit)]) is not None
                        for part in (low, high)):
            return low[:-len(unit)] if low else '', high[:-len(unit)] if high else '', unit
    return None


def range_bounds(value, units):
    fields = range_input(value, units)
    if fields is None:
        return None
    low, high, unit = fields
    scale = finite_number(units[unit])
    if scale is None or scale <= 0:
        return None
    bounds = (finite_number(low) * scale if low else None, finite_number(high) * scale if high else None)
    return None if bounds[0] is not None and bounds[1] is not None and bounds[0] > bounds[1] else bounds


def option_in_range(option, bounds):
    """Match official base-unit values, including fully contained intervals."""
    low, high = bounds
    start = finite_number(option.get('number'))
    end = finite_number(option.get('end_number')) if option.get('interval') else start
    return (start is not None and end is not None and end >= start
            and (low is None or start >= low)
            and (high is None or end is not None and end <= high))


def normalize_filters(filters=None):
    filters = filters or {}
    result = dict(DEFAULTS)
    for key, default in DEFAULTS.items():
        value = filters.get(key, default)
        if type(value) is not type(default):
            raise StoreError('筛选条件格式无效，请重置筛选。')
        if isinstance(value, str) and len(value) > 16000:
            raise StoreError('筛选项过多，请减少选择。')
        result[key] = value
    if result['sortNumber'] not in dict((v, k) for k, v in SORTS) or any(result[k] not in (0, 1) for k in ('spotFilter', 'discountFilter')):
        raise StoreError('筛选选项无效，请重置筛选。')
    for key in ('catalogIdFilter', 'brandIdFilter', 'queryProductTypeCode'):
        if result[key] and any(not product_id(v) for v in result[key].split('$')):
            raise StoreError('分类或品牌标识无效。')
    for key in ('startPrice', 'endPrice'):
        value = result[key]
        if value and (not re.fullmatch(r'\d{1,8}(?:\.\d{1,4})?', value) or decimal_number(value) is None):
            raise StoreError('价格请输入非负数，最多保留四位小数。')
    if result['startPrice'] and result['endPrice'] and decimal_number(result['startPrice']) > decimal_number(result['endPrice']):
        raise StoreError('最低价格不能大于最高价格。')
    if result['demandNumber'] and not re.fullmatch(r'[1-9]\d{0,7}', result['demandNumber']):
        raise StoreError('需求数量请输入 1 至 99999999 的整数。')
    result['secondKeyword'] = result['secondKeyword'].strip()[:160]
    parameters = filters.get('parameters', {})
    if not isinstance(parameters, dict) or len(parameters) > 120:
        raise StoreError('器件参数格式无效。')
    result['parameters'] = {}
    for key, values in parameters.items():
        if not re.fullmatch(r'param_[\w]{1,60}(?:#range)?', key) or not isinstance(values, list) or len(values) > 500:
            raise StoreError('器件参数格式无效。')
        if any(not isinstance(v, str) or not v or len(v) > 500 or '$' in v for v in values):
            raise StoreError('器件参数值无效。')
        if values:
            result['parameters'][key] = list(dict.fromkeys(values))
    if len(result['catalogIdFilter'].split('$')) > 1:
        result['parameters'] = {}; result['lastParamName'] = ''
    return result


def search_payload(keyword, page, filters=None):
    result = normalize_filters(filters)
    params = result.pop('parameters')
    value = '$'.join(v + '@' + key for key, values in params.items() for v in values)
    if len(value) > 32000:
        raise StoreError('器件参数过多，请减少选择。')
    result.update(keyword=keyword.strip()[:160], currentPage=page, pageSize=30,
                  queryParameterValue=quote(value, safe="~!*'()"))
    if keyword and result['brandOriginFilter']:
        result['brandPlaceFilter'] = result['brandOriginFilter']
    return result


def filter_count(filters):
    values = normalize_filters(filters)
    return sum(values[key] != default for key, default in DEFAULTS.items() if key != 'lastParamName') + len(values['parameters'])


def parse_categories(payload):
    data = payload.get('result') if isinstance(payload, dict) and payload.get('code') == 200 else None
    if not isinstance(data, dict):
        raise StoreError('商城分类数据格式已变化，请重试。')
    count = 0
    def node(raw, depth=0):
        nonlocal count
        count += 1
        if count > 10000 or depth > 5 or not isinstance(raw, dict):
            raise StoreError('商城分类数据不完整，请重试。')
        code, name = product_id(raw.get('catalogId')), text(raw.get('catalogName'))
        if not name or raw.get('catalogId') is not None and not code:
            raise StoreError('商城分类数据不完整，请重试。')
        children = [child for value in raw.get('sonCatalogList') or [] if (child := node(value, depth + 1))]
        existing = {child['id'] for child in children}
        for link in raw.get('keywordVOList') or []:
            if not isinstance(link, dict):
                continue
            url = urlsplit(text(link.get('keywordUrl'), 1000))
            match = re.fullmatch(r'/catalog/(\d+)\.html', url.path)
            if url.hostname == 'list.szlcsc.com' and match and match[1] not in existing:
                children.append(dict(id=match[1], name=text(link.get('keywordName')), children=[]))
                existing.add(match[1])
        # MRO-only leaves have no electronics catalog ID. Retain headings that
        # contain electronics; do not invent IDs from the separate MRO catalog.
        return dict(id=code, name=name, children=children) if code or children else None
    return [row for value in data.values() if (row := node(value))]


@traced('catalog.categories', level='INFO')
def read_categories(client, cancelled=None):
    result = parse_categories(client._json(CATEGORIES_URL, cancelled=cancelled))
    log_event('INFO', 'catalog.categories_loaded', count=len(result))
    return result


def _options(rows, label='label', value='label'):
    result, seen = [], set()
    if not isinstance(rows, list):
        raise StoreError('商城筛选选项无法识别，请重试。')
    if len(rows) > 20000:
        raise StoreError('商城筛选选项过多，请缩小搜索范围。')
    for row in rows:
        if not isinstance(row, dict):
            continue
        name = text(row.get(label), 500)
        val = str(row.get(value)) if isinstance(row.get(value), (str, int)) else name
        if not name or val in seen:
            continue
        seen.add(val)
        result.append(dict(label=name, value=val, count=quantity(row.get('count')), enabled=row.get('status') is not False,
            number=row.get('standardUnitValue'), end_number=row.get('standardUnitEndValue'), interval=bool(row.get('isRange'))))
    return result


def parameter_facets(data):
    groups = data.get('paramGroupMap') if isinstance(data, dict) else None
    if not isinstance(groups, dict):
        raise StoreError('商城参数筛选数据无法识别，请重试。')
    facets = []
    for key, rows in groups.items():
        if isinstance(rows, dict):  # Official category page's combined facets.
            info = rows; label = text(info.get('paramName')); rows = info.get('paramDetailList') or []
        else:  # Keyword search's separate parameter aggregation.
            info = (data.get('paramInfoMap') or {}).get(key) or {}
            label = text((data.get('paramNameMap') or {}).get(key))
        if key not in CATEGORY_FIELDS and not re.fullmatch(r'param_\w{1,60}', key):
            continue
        if label:
            facets.append(dict(key=CATEGORY_FIELDS.get(key, key), label=label,
                options=_options(rows, 'name', 'code' if key in ('queryProductGradePlateId', 'queryProductTypeCode') else 'name'),
                units=info.get('unitConversionMap') or {}, interval=bool(info.get('isInterval')),
                numeric=bool(info.get('isNumberRange') or info.get('isNumberRang'))))
    return facets


def search_facets(payload):
    result = payload.get('result') or {}
    if isinstance(result.get('aggregateResultVO'), dict):
        return parameter_facets(result['aggregateResultVO'])
    page = result.get('searchResult') or {}
    facets = []
    for key, label, group, value in GROUPS:
        rows = page.get(group)
        if rows is None:
            try:
                rows = json.loads(result.get(group + 'Json') or '[]')
            except (ValueError, TypeError):
                raise StoreError('商城筛选数据无法识别，请重试。') from None
        facets.append(dict(key=key, label=label, options=_options(rows, value=value)))
    return facets


@traced('catalog.parameters', level='INFO')
def read_parameters(client, keyword, filters, facets, cancelled=None):
    selected = normalize_filters(filters)['catalogIdFilter'].split('$')
    if selected == ['']:
        categories = next((g['options'] for g in facets if g['key'] == 'catalogIdFilter'), [])
        selected = [categories[0]['value']] if len(categories) == 1 else []
    if len(selected) != 1 or not selected[0] or not keyword:
        return []
    payload = search_payload(keyword, 1, {**filters, 'catalogIdFilter': selected[0]})
    response = client._json(PARAMETERS_URL, payload=payload, cancelled=cancelled)
    if not isinstance(response, dict) or response.get('code') != 200:
        raise StoreError('读取器件参数筛选失败，请重试。')
    check_cancelled(cancelled)
    result = parameter_facets(response.get('result'))
    log_event('INFO', 'catalog.parameters_loaded', count=len(result))
    return result
