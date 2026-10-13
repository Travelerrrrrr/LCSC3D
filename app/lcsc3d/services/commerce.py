"""Small native shopping actions; the official site owns checkout and orders."""
from __future__ import annotations

import copy
import json

from lcsc3d.core.app_logging import log_event, traced, safe_part
from lcsc3d.services.store import PART, StoreError, check_cancelled, quantity

CART_URL = 'https://cart.szlcsc.com/cart/display.html'
ORDERS_URL = 'https://member.szlcsc.com/member/order/list.html'
CART_QUICK_API = 'https://cart-api.szlcsc.com/cart/quick'
MAX_CART_QUANTITY = 999999999


def session_cookies(client):
    with client.session.lock:
        return [copy.copy(cookie) for cookie in client.session.cookies if not cookie.is_expired()]


def apply_browser_cookies(client, cookies):
    with client.session.lock:
        for cookie in cookies:
            client.session.cookies.set_cookie(copy.copy(cookie))


def current_cart(client):
    """Respect the cart selected on the official page, without logging its ID."""
    for cookie in session_cookies(client):
        if cookie.name != 'currentCartVo' or cookie.domain.lstrip('.') != 'szlcsc.com':
            continue
        try:
            from urllib.parse import unquote
            data = json.loads(unquote(cookie.value))
            value = data.get('cartCode')
            if isinstance(value, str) and len(value) <= 100:
                return value
        except (ValueError, AttributeError):
            pass
    return ''


@traced('cart.add', lambda client, product, amount, **kw: {'part': safe_part(product.get('part')), 'quantity': amount}, level='INFO')
def add_to_cart(client, product, amount, cancelled=None):
    part = str(product.get('part', '')).upper()
    if not PART.fullmatch(part) or type(amount) is not int or not 1 <= amount <= MAX_CART_QUANTITY:
        raise StoreError('请输入有效的器件编号和加购数量。')
    minimum = quantity(product.get('min_quantity')) or 1
    if amount < minimum:
        raise StoreError('加购数量不能低于商品起订量。')
    check_cancelled(cancelled)
    # This is the site's C-number quick-add endpoint: quantity is in product
    # units, avoiding package conversion and unsupported overseas cart keys.
    # A write is issued exactly once. Never retry an ambiguous network failure.
    result = client._json(CART_QUICK_API,
                          form={'productCode': part, 'productNumber': amount,
                                'cartCode': current_cart(client)},
                          headers={'Origin': 'https://cart.szlcsc.com', 'Referer': CART_URL},
                          cancelled=cancelled)
    if result.get('code') != 200:
        raise StoreError('商城未接受加购，请在购物车核对库存、起订量或商品限制。',
                         service_code=result.get('code'))
    log_event('INFO', 'cart.add_confirmed', part=safe_part(part), quantity=amount)
    return {'product': product, 'quantity': amount}
