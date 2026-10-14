"""Native cart/order data and bounded writes; checkout continues in the browser."""
from __future__ import annotations

import json
import re
from decimal import Decimal
from urllib.parse import unquote, urlencode

from lcsc3d.core.app_logging import log_event, traced, safe_part, record_error
from lcsc3d.core.errors import Cancelled
from lcsc3d.services.store import (PART, StoreError, SessionExpired, check_cancelled, quantity, decimal_number,
                                  text, public_url, MAX_PARTS, product_introduction)

CART_URL = 'https://cart.szlcsc.com/cart/display.html'
ORDERS_URL = 'https://member.szlcsc.com/member/order/list.html'
CART_API = 'https://cart-api.szlcsc.com'
ORDERS_API = 'https://order-api.szlcsc.com/member/order/list'
CART_QUICK_API = CART_API + '/cart/quick'
MAX_CART_QUANTITY = 999999999
CART_ERRORS = {'out_of_stock': '商品不可售', 'cannot_buy': '商品不可购买', 'quantity_below_threshold': '数量未达门槛'}
OVERSEAS_LISTS = ('arrowProductList', 'digikeyProductList', 'mouserProductList', 'vericalProductList',
    'avnetUsProductList', 'avnetEurProductList', 'avnetAsiaProductList', 'futureProductList',
    'elementProductList', 'onlinecomponentsList', 'rscomponentsList', 'corestaffList', 'ttiList',
    'tmeList', 'rutronikList', 'powellProductList', 'sagerProductList', 'chip1stopProductList',
    'questcompProductList', 'element14UkProductList', 'heilindProductList', 'tiOvsProductList',
    'burklinProductList', 'ttiEurProductList', 'avnetEuEbvProductList', 'avnetEuSilicaProductList', 'flipProductList')

REJECTION_REASONS = {
    'unavailable': '商品已停售或下架，无法加购。',
    'stock_limit': '商城提示库存不足，请核对商品库存。',
    'quantity_rule': '购买数量不符合起订量或递增量要求。',
    'purchase_limit': '购买数量超出商城限购要求。',
    'price_unavailable': '商品暂无可用报价，请在商城商品页确认。',
    'store_rejected': '商城拒绝加购，请在商城商品页核对购买条件。',
    'invalid_part': '器件编号无效。',
    'rules_unavailable': '购买规则暂未核实，本项未发送，请稍后重试。',
}


class CartItemRejected(StoreError):
    """A local validation or explicit business refusal; no retry is needed."""
    def __init__(self, reason, *, service_code=None, stage='preflight'):
        self.commerce_reason = reason if reason in REJECTION_REASONS else 'store_rejected'
        self.commerce_stage = stage
        super().__init__(REJECTION_REASONS[self.commerce_reason], service_code=service_code)


def rejection_reason(message):
    # Only emit predefined categories/text. Never copy an arbitrary response to
    # logs or persisted notifications, where it might include account data.
    message = text(message, 1000)
    for reason, phrases in (
        ('unavailable', ('停售', '下架', '停止销售')),
        ('stock_limit', ('库存不足', '超过库存', '超出库存', '无库存')),
        ('quantity_rule', ('起订量', '递增量', '整数倍', '最小购买量')),
        ('purchase_limit', ('限购', '最大购买量')),
        ('price_unavailable', ('暂无报价', '暂无价格', '无价格', '未报价')),
    ):
        if any(phrase in message for phrase in phrases):
            return reason
    return 'store_rejected'


def purchase_rules_known(product):
    return bool(product.get('purchase_rules_known', True) and quantity(product.get('min_quantity'))
                and quantity(product.get('quantity_step')))


def current_cart(client):
    with client.session.lock:
        for cookie in client.session.cookies:
            if cookie.name != 'currentCartVo' or cookie.is_expired() or cookie.domain.lstrip('.') != 'szlcsc.com':
                continue
            try:
                value = json.loads(unquote(cookie.value)).get('cartCode')
                if isinstance(value, str) and re.fullmatch(r'[\w-]{1,100}', value):
                    return value
            except (ValueError, AttributeError):
                pass
    return ''


def cart_request(client, path, *, form=None, payload=None, cancelled=None):
    result = client._json(CART_API + path, form=form, payload=payload, cancelled=cancelled,
                          headers={'Origin': 'https://cart.szlcsc.com', 'Referer': CART_URL})
    if result.get('code') != 200:
        code = result.get('code')
        reason = rejection_reason(result.get('msg'))
        rejected = path == '/cart/quick' and type(code) is int and code == 402
        log_event('WARNING', 'cart.response_rejected', service_code=code if type(code) is int else None,
                  reason=reason, disposition='rejected' if rejected else 'unconfirmed',
                  reason_source='response_message' if reason != 'store_rejected' else 'unknown',
                  response_message_present=bool(text(result.get('msg'), 1000)))
        if rejected:
            raise CartItemRejected(reason, service_code=code, stage='response')
        raise StoreError('商城未接受操作，请刷新购物车核对库存、数量或商品限制。', service_code=result.get('code'))
    details = result.get('result')
    if isinstance(details, dict) and (details.get('hkAddFail') or details.get('shifting')):
        log_event('WARNING', 'cart.partial_response', hk_add_failed=bool(details.get('hkAddFail')),
                  shifting=bool(details.get('shifting')))
        raise StoreError('商城未完成操作，请刷新购物车核对商品限制。')
    return result


def money(value):
    number = decimal_number(value)
    return format(number, 'f') if number is not None else ''


def cart_items(payload):
    """Use the same explicit cart groups as the official cart application."""
    result = payload.get('result') if isinstance(payload, dict) else None
    cart = result.get('shoppingCartVO') if isinstance(result, dict) else None
    if not isinstance(payload, dict) or payload.get('code') != 200 or not isinstance(cart, dict):
        raise StoreError('购物车数据格式已变化，请稍后刷新。')
    groups = cart.get('cartItemGroups')
    groups = groups if result.get('isCartItemGroup') and isinstance(groups, list) and groups else [cart]
    output, seen = [], set()
    for group in groups:
        if not isinstance(group, dict):
            continue
        for band, currency, ratio_field, region in (
                ('rmbCnShoppingCart', '¥', 'convesionRatio', 'cn'),
                ('rmbHkShoppingCart', '¥', 'hkConvesionRatio', 'hk-rmb'),
                ('dollerHkShoppingCart', '$', 'hkConvesionRatio', 'hk-usd'),
                ('shoppingCartOverseaVO', '¥', 'convesionRatio', 'overseas')):
            section = group.get(band) or {}
            if not isinstance(section, dict):
                continue
            kinds = OVERSEAS_LISTS + ('cnOverseasProductList',) if region == 'overseas' else ('currentlyProductList', 'isNeedProductList', 'isFreeProductList')
            for kind in kinds:
                rows = section.get(kind) or []
                if not isinstance(rows, list):
                    raise StoreError('购物车商品列表无法识别，请刷新后重试。')
                for raw in rows:
                    if not isinstance(raw, dict):
                        continue
                    key = text(raw.get('shopCarMapKey'), 240)
                    if not key:
                        raise StoreError('购物车商品标识缺失，请刷新后重试。')
                    if key in seen:
                        continue
                    seen.add(key)
                    ratio = decimal_number(raw.get(ratio_field)) or Decimal(1)
                    packs = decimal_number(raw.get('productNumber'))
                    amount = quantity(packs * ratio) if packs is not None else None
                    if amount is None or not 1 <= amount <= MAX_CART_QUANTITY:
                        raise StoreError('购物车数量无法识别，已停止显示以避免误购。')
                    price = money(raw.get('productConsultPrice'))
                    if not price and decimal_number(raw.get('lineMoney')) is not None:
                        price = format(decimal_number(raw.get('lineMoney')) / amount, 'f')
                    output.append(dict(key=key, part=text(raw.get('productCode'), 48),
                        title=text(raw.get('productModel') or raw.get('productName')),
                        description=product_introduction(raw, text(raw.get('productModel') or raw.get('productName'))),
                        manufacturer=text(raw.get('productGradePlateName') or raw.get('brandName')),
                        category=text(raw.get('productType')),
                        product_id=str(raw.get('productId') or ''), package=text(raw.get('encapsulationModel') or raw.get('encapStandard')),
                        quantity=amount, ratio=str(ratio), price=price, total=money(raw.get('lineMoney')),
                        currency=currency, region=region, checked=bool(raw.get('checked') or raw.get('isChecked')),
                        unit=text(raw.get('productUnit'), 20) or '个',
                        minimum=(quantity(raw.get('minBuyNumber')) or 1) * (quantity(ratio) or 1),
                        quantity_step=(quantity(ratio) or 1) if region in ('cn', 'overseas') else 1,
                        available=raw.get('errStatus') not in CART_ERRORS,
                        status=CART_ERRORS.get(raw.get('errStatus')) or ('待订货' if kind == 'isNeedProductList' else '现货'),
                        image=public_url(raw.get('breviaryImageUrl'))))
                    if len(output) > MAX_PARTS:
                        raise StoreError('购物车商品过多，请先在商城整理后刷新。')
    if not output and (quantity(cart.get('cartTotalSize')) or 0) > 0:
        raise StoreError('购物车商品分组无法识别，请刷新后重试。')
    return output


@traced('cart.read')
def read_cart(client, cancelled=None):
    payload = cart_request(client, '/cart/display?' + urlencode({'isInit': 'true', 'cartCode': current_cart(client)}),
                           cancelled=cancelled)
    rows = cart_items(payload)
    log_event('INFO', 'cart.loaded', count=len(rows))
    return rows


def validate_quantity(amount, minimum=1, step=1):
    if type(amount) is not int or not max(1, minimum) <= amount <= MAX_CART_QUANTITY:
        raise StoreError('请输入不低于起订量的有效数量。')
    if amount % max(1, step):
        raise StoreError('购买数量必须是递增量的整数倍。')


@traced('cart.add', lambda client, product, amount, **kw: {'part': safe_part(product.get('part')), 'quantity': amount}, level='INFO')
def add_to_cart(client, product, amount, cancelled=None):
    part = str(product.get('part', '')).upper()
    if not PART.fullmatch(part):
        raise CartItemRejected('invalid_part')
    if product.get('purchase_status') == 'unavailable':
        raise CartItemRejected('unavailable')
    try:
        validate_quantity(amount, quantity(product.get('min_quantity')) or 1, quantity(product.get('quantity_step')) or 1)
    except StoreError:
        raise CartItemRejected('quantity_rule') from None
    check_cancelled(cancelled)
    cart_request(client, '/cart/quick', form={'productCode': part, 'productNumber': amount,
                 'cartCode': current_cart(client)}, cancelled=cancelled)
    log_event('INFO', 'cart.add_confirmed', part=safe_part(part), quantity=amount)
    return {'product': product, 'quantity': amount}


@traced('cart.batch', lambda client, rows, **kw: {'requested_count': len(rows)}, level='INFO')
def add_cart_batch(client, rows, cancelled=None):
    """Continue explicit item rejections; stop uncertain writes without replay."""
    result = {'items': [], 'products': [], 'session_expired': False, 'stop_reason': ''}

    def item(index, status, reason='', error=None):
        product, amount = rows[index]
        code = getattr(error, 'service_code', None)
        row = dict(part=safe_part(product.get('part')) or '—', quantity=amount, status=status,
                   reason=reason, service_code=code if type(code) is int else None,
                   request_attempted=status != 'not_sent' and getattr(error, 'commerce_stage', '') != 'preflight')
        result['items'].append(row)
        fields = dict(index=index + 1, part=safe_part(product.get('part')), quantity=amount,
                      disposition=status, reason=reason, service_code=row['service_code'],
                      request_attempted=row['request_attempted'])
        if error:
            record_error(error, 'cart.batch_item', level='WARNING', **fields)
        else:
            log_event('INFO', 'cart.batch_item', **fields)

    def remaining(start, reason):
        result['stop_reason'] = reason
        for index in range(start, len(rows)):
            item(index, 'not_sent', reason)

    def complete():
        counts = {status: sum(row['status'] == status for row in result['items'])
                  for status in ('added', 'rejected', 'unconfirmed', 'not_sent')}
        log_event('WARNING' if counts['rejected'] or counts['unconfirmed'] or counts['not_sent'] else 'INFO',
                  'cart.batch_completed', count=counts['added'], failed_count=counts['rejected'],
                  unconfirmed_count=counts['unconfirmed'], pending_count=counts['not_sent'],
                  stop_reason=result['stop_reason'])
        return result

    try:
        check_cancelled(cancelled)
        client.account_info(cancelled)
    except Cancelled:
        remaining(0, 'cancelled'); complete()
        raise
    except Exception as error:
        result['session_expired'] = isinstance(error, SessionExpired)
        reason = 'session_expired' if result['session_expired'] else 'account_check_failed'
        record_error(error, 'cart.batch_preflight_failed', reason=reason)
        remaining(0, reason)
        return complete()

    for index, (product, amount) in enumerate(rows):
        request_attempted = False
        if cancelled is not None and cancelled.is_set():
            remaining(index, 'cancelled'); complete()
            raise Cancelled()
        try:
            if product.get('purchase_status') != 'unavailable' and not purchase_rules_known(product):
                try:
                    product = client.product(product, cancelled) or product
                except Cancelled:
                    raise
                except SessionExpired as error:
                    result['session_expired'] = True
                    item(index, 'not_sent', 'session_expired', error)
                    remaining(index + 1, 'session_expired')
                    break
                except Exception as error:
                    item(index, 'not_sent', 'rules_unavailable', error)
                    continue
                result['products'].append({key: product[key] for key in
                    ('part', 'min_quantity', 'quantity_step', 'purchase_rules_known', 'purchase_status') if key in product})
                log_event('INFO', 'cart.purchase_rules', part=safe_part(product.get('part')),
                          minimum=quantity(product.get('min_quantity')), increment=quantity(product.get('quantity_step')),
                          available=product.get('purchase_status') != 'unavailable', known=purchase_rules_known(product))
                if product.get('purchase_status') != 'unavailable' and not purchase_rules_known(product):
                    item(index, 'not_sent', 'rules_unavailable')
                    continue
            request_attempted = True
            add_to_cart(client, product, amount, cancelled=cancelled)
        except CartItemRejected as error:
            item(index, 'rejected', error.commerce_reason, error)
            continue
        except Cancelled:
            if request_attempted:
                item(index, 'unconfirmed', 'cancelled_during_request')
            remaining(index + 1 if request_attempted else index, 'cancelled'); complete()
            raise
        except SessionExpired as error:
            result['session_expired'] = True
            item(index, 'rejected', 'session_expired', error)
            remaining(index + 1, 'session_expired')
            break
        except Exception as error:
            item(index, 'unconfirmed', 'request_unconfirmed', error)
            remaining(index + 1, 'previous_unconfirmed')
            break
        else:
            item(index, 'added')
    return complete()


def cart_key(row):
    key = row.get('key')
    if not isinstance(key, str) or not key or len(key) > 240 or any(c in key for c in ',@\r\n'):
        raise StoreError('购物车商品标识无效，请刷新。')
    return key


@traced('cart.quantity', lambda client, row, amount, **kw: {'part': safe_part(row.get('part')), 'quantity': amount}, level='INFO')
def update_quantity(client, row, amount, cancelled=None):
    validate_quantity(amount, row.get('minimum', 1), row.get('quantity_step', 1))
    ratio = decimal_number(row.get('ratio'))
    if not ratio:
        raise StoreError('商品包装换算数据无效，请刷新购物车。')
    return cart_request(client, '/cart/update', form={'cartKey': cart_key(row),
        'productNumber': format(Decimal(amount) / ratio, 'f'), 'cartCode': current_cart(client)}, cancelled=cancelled)


@traced('cart.remove', lambda client, rows, **kw: {'count': len(rows)}, level='INFO')
def remove_items(client, rows, cancelled=None):
    return cart_request(client, '/cart/delete', payload={'cartKeyList': [cart_key(row) for row in rows],
        'isSelectAll': False, 'cartCode': current_cart(client)}, cancelled=cancelled)


def safe_display(value, limit=240):
    return '' if isinstance(value, str) and value.startswith('SMEncrypt#') else text(value, limit)


def order_page(payload, expected_page):
    result = payload.get('result') if isinstance(payload, dict) else None
    page = result.get('page') if isinstance(result, dict) else None
    rows = result.get('customerOrderList') if isinstance(result, dict) else None
    if not isinstance(payload, dict) or payload.get('code') != 200 or not isinstance(page, dict) or not isinstance(rows, list):
        raise StoreError('订单列表数据格式已变化，请稍后刷新。')
    current, size, total = (quantity(page.get(k)) for k in ('currPage', 'pageRow', 'totalRow'))
    if current != expected_page or not size or size > 100 or total is None:
        raise StoreError('订单分页数据异常，请重新查询。')
    if not rows and total > (current - 1) * size:
        raise StoreError('订单分页为空，请重新查询，避免遗漏订单。')
    statuses, payments = result.get('orderStatusMap') or {}, result.get('onlineDefrayStatusMap') or {}
    if not isinstance(statuses, dict) or not isinstance(payments, dict):
        raise StoreError('订单状态数据无法识别。')
    output = []
    for row in rows[:size]:
        if not isinstance(row, dict):
            continue
        identity = safe_display(row.get('uuid'), 100)
        if not re.fullmatch(r'[a-zA-Z0-9_-]{1,100}', identity):
            continue
        details = row.get('orderDetailVOList') or []
        products = [safe_display(v.get('productCode'), 48) + ' ' + safe_display(v.get('productModel'))
                    for v in details if isinstance(v, dict)] if isinstance(details, list) else []
        output.append(dict(identity=identity, code=safe_display(row.get('orderCode')), date=safe_display(row.get('orderTime')),
            summary='；'.join(products) or '元器件订单', total=money(row.get('orderTotalMoney')),
            currency='$' if row.get('currencyType') == 'USD' else '¥',
            status=text(statuses.get(row.get('orderStatus'))) or safe_display(row.get('orderStatus')),
            payment=text(payments.get(row.get('onlineDefrayStatus'))) or safe_display(row.get('onlineDefrayStatus')),
            url='https://member.szlcsc.com/member/order/details.html?' + urlencode({'uuid': identity})))
    if len(output) != len(rows):
        raise StoreError('部分订单数据无法识别，请刷新后重试。')
    return dict(items=output, page=current, total=total, pages=(total + size - 1) // size)


@traced('orders.read', lambda client, page=1, **kw: {'page': page})
def read_orders(client, page=1, *, keyword='', status='', cancelled=None):
    if type(page) is not int or page < 1:
        raise StoreError('订单页码无效。')
    query = {'currentPage': page, 'pageSize': 20, 'searchKeywords': text(keyword, 160)}
    if status == 'wait':
        query['onlineDefrayStatus'] = 'wait'
    elif status in ('wait_audit', 'notSend', 'already_send', 'canceled'):
        query['orderStatus'] = status
    result = client._json(ORDERS_API + '?' + urlencode(query), cancelled=cancelled,
                          headers={'Origin': 'https://member.szlcsc.com', 'Referer': ORDERS_URL})
    result = order_page(result, page)
    log_event('INFO', 'orders.loaded', count=len(result['items']), page=page)
    return result
