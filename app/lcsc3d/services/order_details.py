"""Read only the purchased goods and amounts from the official order response."""
from urllib.parse import urlencode
import re

from lcsc3d.core.app_logging import log_event, traced
from lcsc3d.services.commerce import safe_display, money
from lcsc3d.services.store import PART, StoreError, decimal_number, MAX_PARTS, image_urls, product_introduction

DETAIL_API = 'https://order-api.szlcsc.com/member/order/details'
GROUPS = tuple((prefix + suffix, label, kind)
    for prefix, label in (('sz', '深圳仓'), ('js', '江苏仓'), ('zh', '珠海仓'))
    for suffix, kind in (('NormalProductList', '商品'), ('AllBrokageDetailsList', '订货'), ('OverseasProductList', '海外代购'))
) + (('hkNormalProductList', '香港仓', '商品'), ('hkBookProductList', '香港仓', '订货'))


def identity(value):
    if not isinstance(value, str) or not re.fullmatch(r'[a-zA-Z0-9_-]{1,100}', value):
        raise StoreError('订单标识无效，请重新查询订单。')
    return value


def parse_order_details(payload, expected):
    result = payload.get('result') if isinstance(payload, dict) else None
    order = result.get('orderVO') if isinstance(result, dict) else None
    if not isinstance(payload, dict) or payload.get('code') != 200:
        raise StoreError('读取订单商品失败，请稍后重试。',
                         service_code=payload.get('code') if isinstance(payload, dict) else None)
    if not isinstance(order, dict) or order.get('uuid') != identity(expected):
        raise StoreError('订单明细与所选订单不一致，请重新查询。')
    if not any(key in result for key, _, _ in GROUPS):
        raise StoreError('订单商品数据格式已变化，可点击「查看订单详情」在商城核对。')
    rows = []
    for key, warehouse, kind in GROUPS:
        entries = result.get(key)
        if entries is None:
            entries = []
        if not isinstance(entries, list):
            raise StoreError('订单商品分组无法识别，请刷新明细。')
        for raw in entries:
            if not isinstance(raw, dict):
                raise StoreError('部分订单商品无法识别，请刷新明细。')
            code = safe_display(raw.get('productCode'), 48)
            if kind == '商品':
                code = safe_display(raw.get('selfProductCode'), 48) or code
            part = next((code.upper() for code in (code, safe_display(raw.get('lcProductCode'), 48))
                         if PART.fullmatch(code)), '')
            discount = decimal_number(raw.get('productDiscountPrice'))
            quote = raw.get('previousPrice') and raw.get('status') == 'set_cost_price'
            price = money(discount if discount and not quote else raw.get('productPrice'))
            display_code = code + (' (' + part + ')' if part and part != code else '')
            photos = image_urls(raw.get('breviaryImageUrl')) or image_urls(raw.get('luceneBreviaryImageUrls'))
            rows.append(dict(code=display_code, part=part, title=safe_display(raw.get('productModel') or raw.get('productName')),
                description=product_introduction(raw, safe_display(raw.get('productModel') or raw.get('productName'))),
                manufacturer=safe_display(raw.get('productGradePlateName') or raw.get('brandName')),
                category=safe_display(raw.get('productType')),
                image=photos[0] if photos else '',
                name=safe_display(raw.get('productName')), package=safe_display(raw.get('encapStandard')),
                quantity=money(raw.get('purchaseNumber')), unit=safe_display(raw.get('stockUnitName') or raw.get('stockUnit'), 24),
                price=price, total=money(raw.get('orderDetailTotalMoney')), warehouse=warehouse,
                kind='物品' if raw.get('isMro') else kind, downloadable=bool(part) and not bool(raw.get('isMro'))))
            if len(rows) > MAX_PARTS:
                raise StoreError('订单商品过多，请在商城查看完整订单。')
    currency = safe_display(order.get('currencyType'), 8)
    symbol = {'USD': '$', 'HKD': 'HK$', 'RMB': '¥', 'CNY': '¥', '': '¥'}.get(currency, currency + ' ')
    return dict(items=rows, code=safe_display(order.get('orderCode')), date=safe_display(order.get('orderTime')),
                currency=symbol, total=money(order.get('orderTotalMoney')),
                goods_total=money(order.get('productTotalMoney')), freight=money(order.get('freightMoney')))


@traced('orders.detail_read')
def read_order_details(client, order_id, cancelled=None):
    query = urlencode({'uuid': identity(order_id)})
    payload = client._json(DETAIL_API + '?' + query, cancelled=cancelled,
                          headers={'Origin': 'https://member.szlcsc.com',
                                   'Referer': 'https://member.szlcsc.com/member/order/details.html?' + query})
    result = parse_order_details(payload, order_id)
    log_event('INFO', 'orders.detail_loaded', count=len(result['items']),
              downloadable_count=sum(row['downloadable'] for row in result['items']))
    return result
