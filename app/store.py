"""Process-local storefront sessions and public catalog data for the native UI."""
from __future__ import annotations

from app_logging import traced, safe_part, network_target, record_error

import gzip
import base64
from html.parser import HTMLParser
import http.cookiejar
import http.client
import json
import re
import socket
import ssl
from decimal import Decimal, InvalidOperation
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import urlencode, urlsplit
import zlib

from backend import shared_ssl_context
from errors import Cancelled
from store_crypto import encrypt_login_value, decode_login_values
from store_diagnostics import record_request_error
from app_settings import proxy_settings, proxy_mode
from app_logging import log_event

PASSPORT = 'https://passport.jlc.com'
STOREFRONT = 'https://www.szlcsc.com'
FAVORITES_URL = 'https://member.szlcsc.com/member/favorite.html'
FAVORITES_API = 'https://member.szlcsc.com/member/favorite/v3'
SEARCH_URL = 'https://pro.lceda.cn/api/szlcsc/eda/product/list'
STORE_SEARCH_URL = 'https://so.szlcsc.com/query/product'
FAVORITE_STATE_URL = 'https://member.szlcsc.com/select/product/favorite/v2'
FAVORITE_TOGGLE_URL = 'https://member.szlcsc.com/async/favorite/add/dynamic'
FAVORITE_CANCEL_URL = 'https://member.szlcsc.com/async/favorite/cancel'
SEARCH_PAGE_SIZE = 50
USER_AGENT = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
              '(KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36')
MAX_PARTS, MAX_PAGES = 10000, 200
PART = re.compile(r'C[0-9]{1,18}\Z', re.I)


class StoreError(ValueError):
    def __init__(self, message, *, service_code=None):
        super().__init__(message)
        self.service_code = service_code if type(service_code) is int else None


class SessionExpired(StoreError):
    pass


class CaptchaRequired(StoreError):
    def __init__(self, scene):
        super().__init__('请完成图片验证码后继续登录。')
        self.scene = scene


def check_cancelled(cancelled):
    if cancelled is not None and cancelled.is_set():
        raise Cancelled()


def text(value, limit=240):
    return value[:limit].strip() if isinstance(value, str) else ''


def is_favorites_url(url):
    try:
        p = urlsplit(url)
        return (p.scheme == 'https' and p.hostname == 'member.szlcsc.com'
                and p.port in (None, 443) and not p.username and not p.password
                and p.path.rstrip('/') == '/member/favorite.html')
    except ValueError:
        return False


def public_url(url):
    """Discard non-web URLs before opening catalog links or loading images."""
    if not isinstance(url, str):
        return ''
    if url.startswith('//'):
        url = 'https:' + url
    try:
        p = urlsplit(url)
        return url if p.scheme == 'https' and p.hostname and not p.username and not p.password else ''
    except ValueError:
        return ''


def normalize_items(items):
    result, seen = [], set()
    if not isinstance(items, list):
        return result
    for item in items[:MAX_PARTS]:
        if not isinstance(item, dict):
            continue
        part = text(item.get('part')).upper()
        if not PART.fullmatch(part) or part in seen:
            continue
        seen.add(part)
        result.append({'part': part, 'title': text(item.get('title'))})
    return result


def catalog_products(payload, keyword=''):
    if not isinstance(payload, dict) or payload.get('success') is False or payload.get('code') != 0:
        raise StoreError('商品查询失败，请稍后重试。')
    rows = payload.get('result')
    if not isinstance(rows, list):
        raise StoreError('商品查询返回了无法识别的数据。')
    result, seen = [], set()
    meta = {'LCSC Part Name', 'Supplier Part', 'Supplier', 'Manufacturer', 'Manufacturer Part',
            'Supplier Footprint', 'Datasheet', 'Name', 'Add into BOM', 'Convert to PCB',
            'Symbol', 'Footprint', '3D Model', '3D Model Title', '3D Model Transform', 'Designator'}
    for raw in rows:
        if not isinstance(raw, dict):
            continue
        attrs = raw.get('attributes')
        attrs = attrs if isinstance(attrs, dict) else {}
        part = text(raw.get('product_code') or attrs.get('Supplier Part')).upper()
        if not PART.fullmatch(part) or part in seen:
            continue
        seen.add(part)
        datasheet = public_url(attrs.get('Datasheet'))
        match = re.search(r'/([0-9]+)\.html(?:\?|$)', datasheet)
        images = raw.get('images')
        images = list(dict.fromkeys(public_url(v) for v in images if public_url(v))) if isinstance(images, list) else []
        image = images[0] if images else ''
        tags = raw.get('tags') or {}
        child = tags.get('child_tag') or {} if isinstance(tags, dict) else {}
        parameters = [(text(k, 100), text(v, 1000)) for k, v in list(attrs.items())[:120]
                      if k not in meta and isinstance(v, str) and v.strip()]
        result.append({'part': part, 'title': text(attrs.get('Manufacturer Part') or attrs.get('LCSC Part Name')
                                                  or raw.get('display_title') or raw.get('title')),
                       'manufacturer': text(attrs.get('Manufacturer')),
                       'package': text(attrs.get('Supplier Footprint')),
                       'description': text(raw.get('description'), 2000),
                       'category': text(child.get('name_cn') or child.get('name')) if isinstance(child, dict) else '',
                       'image': image, 'images': images, 'thumbnails': images, 'datasheet': datasheet,
                       'product_id': match[1] if match else '', 'details_complete': True, 'detail_source': 'EDA 元件目录',
                       'store_url': f'https://item.szlcsc.com/{match[1]}.html' if match else
                                    'https://so.szlcsc.com/global.html?' + urlencode({'k': part}),
                       'model': text(attrs.get('3D Model Title')),
                       'resources': {key: bool(attrs.get(key)) for key in ('Symbol', 'Footprint', '3D Model')},
                       'parameters': parameters})
    exact = keyword.strip().upper()
    result.sort(key=lambda row: (row['part'] != exact, row['title'].upper() != exact))
    return result


def image_urls(value):
    if isinstance(value, str):
        value = value.split('<$>')
    if not isinstance(value, list):
        return []
    return list(dict.fromkeys(public_url(url) for url in value if public_url(url)))


def original_image(url):
    """Use the same original-image path as the official storefront gallery."""
    url = public_url(url)
    if urlsplit(url).hostname == 'alimg.szlcsc.com':
        return url.replace('/product/breviary/', '/product/source/')
    return url


def product_id(value):
    value = str(value) if isinstance(value, (str, int)) and not isinstance(value, bool) else ''
    return value if value.isdigit() and len(value) <= 18 else ''


def decimal_number(value):
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        return None
    raw = str(value).strip()
    if not raw or len(raw) > 64:
        return None
    try:
        number = Decimal(raw)
        return number if number.is_finite() and number >= 0 and -30 <= number.adjusted() <= 18 else None
    except InvalidOperation:
        return None


def quantity(value):
    number = decimal_number(value)
    return int(number) if number is not None and number == number.to_integral_value() else None


def commercial_data(raw):
    """Use mainland unit-price tiers, independently of package and SMT stock."""
    data = {}
    minimum = quantity(raw.get('minBuyNumber')) or 1
    if 'minBuyNumber' in raw:
        data['min_quantity'] = minimum
    unit = text(raw.get('productUnit'), 20)
    if unit:
        data['unit'] = unit
    for key in ('validStockNumber', 'stockNumber'):
        stock = quantity(raw.get(key))
        if stock is not None:
            data['stock'] = stock
            break
    if 'productPriceList' not in raw:
        return data
    rows = raw.get('productPriceList')
    rows = rows if isinstance(rows, list) and raw.get('isShowPrice') is not False else []
    tiers = []
    for row in rows[:100]:
        if not isinstance(row, dict):
            continue
        start = quantity(row.get('startPurchasedNumber')) or quantity(row.get('spNumber'))
        amount = decimal_number(row.get('productPrice') if row.get('productPrice') is not None else row.get('thePrice'))
        end_value = row.get('endPurchasedNumber', row.get('epNumber'))
        end = quantity(end_value)
        if not start or amount is None or end is not None and (end < start or end < minimum):
            continue
        tiers.append({'quantity': max(start, minimum), 'end': end, 'price': format(amount, 'f')})
    by_quantity = {}
    for tier in sorted(tiers, key=lambda tier: tier['quantity']):
        by_quantity.setdefault(tier['quantity'], tier)
    data['price_tiers'] = list(by_quantity.values())
    return data


def storefront_product(raw, parameters=()):
    if not isinstance(raw, dict):
        return None
    part = text(raw.get('productCode')).upper()
    if not PART.fullmatch(part):
        return None
    identity = product_id(raw.get('productId'))
    thumbnails = image_urls(raw.get('luceneBreviaryImageUrls')) or image_urls(raw.get('breviaryImageUrl'))
    images = list(dict.fromkeys(original_image(url) for url in thumbnails))
    title = text(raw.get('productModel') or raw.get('productName'))
    description = [text(raw.get('productName'), 16384), text(raw.get('remark'), 16384)]
    return {'part': part, 'title': title, 'product_id': identity,
            'manufacturer': text(raw.get('productGradePlateName') or raw.get('brandName')),
            'package': text(raw.get('encapsulationModel')), 'category': text(raw.get('productType')),
            'description': '\n'.join(dict.fromkeys(v for v in description if v and v != title)),
            'image': images[0] if images else '', 'images': images, 'thumbnails': thumbnails,
            'store_url': f'https://item.szlcsc.com/{identity}.html' if identity else '',
            'parameters': list(parameters), 'details_complete': False, 'detail_source': '立创商城',
            **commercial_data(raw)}


def search_page_products(payload, expected_page):
    if not isinstance(payload, dict) or payload.get('code') != 200:
        raise StoreError('商城商品搜索失败，请稍后重试。')
    result = payload.get('result')
    page = result.get('searchResult') if isinstance(result, dict) else None
    if not isinstance(page, dict) or not isinstance(page.get('productRecordList'), list):
        raise StoreError('商城返回了无法识别的搜索结果，请重试。')
    try:
        total, size, current, pages = (int(page[k]) for k in ('totalCount', 'pageSize', 'currePage', 'countPage'))
        if total < 0 or not 1 <= size <= 1000 or current != expected_page or pages < 0:
            raise ValueError()
        if pages < (total + size - 1) // size:
            raise ValueError()
    except (KeyError, ValueError, TypeError, OverflowError):
        raise StoreError('商城搜索分页信息异常，已停止读取。') from None
    rows = page['productRecordList']
    if not rows and total > (expected_page - 1) * size:
        raise StoreError('商城搜索分页为空，已停止读取，避免遗漏商品。')
    products, positions, seen = [], [], set()
    for row in rows:
        if not isinstance(row, dict):
            positions.append(None)
            continue
        params = row.get('paramLinkedMap')
        params = [(text(k, 100), text(v, 16384)) for k, v in params.items()
                  if isinstance(v, str) and v.strip()] if isinstance(params, dict) else []
        product = storefront_product(row.get('productVO'), params)
        # The displayed search inventory is the search row's current mainland total.
        stock = quantity(row.get('totalStockNumber'))
        if product and stock is not None:
            product['stock'] = stock
        positions.append(product)
        if product and product['part'] not in seen:
            seen.add(product['part'])
            products.append(product)
    return {'items': products, 'positions': positions, 'total': total, 'page': current, 'pages': pages, 'size': size}


class _PageData(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.inside, self.chunks = False, []

    def handle_starttag(self, tag, attrs):
        if tag == 'script' and dict(attrs).get('id') == '__NEXT_DATA__':
            self.inside = True

    def handle_data(self, data):
        if self.inside:
            self.chunks.append(data)

    def handle_endtag(self, tag):
        if tag == 'script':
            self.inside = False


def product_page(html, expected_part):
    parser = _PageData()
    try:
        parser.feed(html.decode('utf-8') if isinstance(html, bytes) else html)
        data = json.loads(''.join(parser.chunks))['props']['pageProps']['webData']
        params = [(text(p.get('parameterName'), 100), text(p.get('parameterDetailValue') or p.get('parameterValue'), 16384))
                  for p in data.get('paramList', []) if isinstance(p, dict) and p.get('parameterName')]
        product = storefront_product(data.get('productRecord'), params)
        if not product or product['part'] != expected_part:
            raise ValueError()
        pdf = data.get('pdfFileDetailVO') or {}
        path = pdf.get('fileUrl') if isinstance(pdf, dict) else ''
        product['datasheet'] = public_url(path) or ('https://datasheet.lcsc.com' + path
                                                  if isinstance(path, str) and path.startswith('/upload/public/pdf/') else '')
        product['details_complete'] = True
        return product
    except (ValueError, KeyError, TypeError, UnicodeError):
        raise StoreError('商城商品详细资料暂不可用，请稍后重试。') from None


def favorite_products(payload, expected_page):
    """Account favorites return result.page, independently of public search."""
    if not isinstance(payload, dict) or payload.get('code') != 200:
        raise StoreError('获取商城收藏失败，请稍后重试。')
    result = payload.get('result')
    page = result.get('page') if isinstance(result, dict) else None
    if not isinstance(page, dict) or not isinstance(page.get('dataList'), list):
        raise StoreError('商城返回了无法识别的收藏数据，请重试。')
    try:
        total, size = int(page['totalRow']), int(page.get('pageRow', page.get('pageSize', 100)))
        current = int(page.get('currPage', page.get('currentPage', expected_page)))
        if total < 0 or size < 1 or size > 1000 or current != expected_page:
            raise ValueError()
    except (KeyError, ValueError, TypeError, OverflowError):
        raise StoreError('商城收藏分页信息异常，已停止读取。') from None
    rows = page['dataList']
    if not rows and total > (expected_page - 1) * size:
        raise StoreError('商城收藏分页为空，已停止读取，避免遗漏元件。')
    products, seen = [], set()
    for raw in rows:
        if not isinstance(raw, dict):
            continue
        part = text(raw.get('productCode') or raw.get('selfProductCode')).upper()
        if not PART.fullmatch(part) or part in seen:
            continue
        seen.add(part)
        identity = product_id(raw.get('selfProductId') or raw.get('productId'))
        thumbnails = image_urls(raw.get('luceneBreviaryImageUrls')) or image_urls(raw.get('breviaryImageUrl'))
        images = [original_image(url) for url in thumbnails]
        products.append({'part': part,
                         'title': text(raw.get('productModel') or raw.get('productModelWithoutHighlight') or raw.get('productName')),
                         'manufacturer': text(raw.get('brandName') or raw.get('productGradePlateName')),
                         'package': text(raw.get('selfStandard') or raw.get('encapsulationModel') or raw.get('standard')),
                         'image': images[0] if images else '', 'images': images, 'thumbnails': thumbnails,
                         'product_id': identity, 'details_complete': False,
                         'store_url': f'https://item.szlcsc.com/{identity}.html' if identity else ''})
    return products, total, size


def request_operation(url):
    """Use fixed labels so an error never displays query strings or auth codes."""
    try:
        parsed = urlsplit(url)
    except ValueError:
        return '商城请求'
    if parsed.hostname == 'passport.jlc.com':
        return {'/api/cas/login/get-official-qrcode': '获取登录二维码',
                '/api/cas/login/get-official-scan-result': '查询扫码状态',
                '/api/cas/config/get-public-key': '获取登录加密配置',
                '/api/cas/secret/update': '初始化登录加密通道',
                '/api/cas/secure/check-login-risk': '账号登录前校验',
                '/api/cas/secure/check-sms-risk': '短信发送前校验',
                '/api/cas/login/with-sms/send-code': '发送短信验证码',
                '/api/cas/login/with-sms/check-code': '验证短信验证码',
                '/api/cas/login/with-password': '账号密码登录',
                '/api/cas/login/get-init-session': '初始化登录会话',
                '/api/cas/login/with-official-qrcode': '确认扫码登录',
                '/api/cas/captcha/get-static-verify-img': '获取图片验证码',
                '/api/cas/captcha/check-static-verify-img': '验证图片验证码',
                }.get(parsed.path, '登录服务请求')
    if parsed.hostname == 'mp.weixin.qq.com':
        return '加载二维码图片'
    return '商城请求'


class MemorySession:
    """No file-backed cookie jar, account logging, automatic POST retries or disk cache."""
    def __init__(self):
        self.cookies = http.cookiejar.CookieJar()
        self._proxies = proxy_settings('store')
        self.opener = self._build_opener(self._proxies)
        self.lock = threading.RLock()

    def _build_opener(self, proxies):
        return urllib.request.build_opener(
            urllib.request.ProxyHandler(proxies),
            urllib.request.HTTPCookieProcessor(self.cookies),
            urllib.request.HTTPSHandler(context=shared_ssl_context()))

    @traced('store.http', lambda self, url, **kw: {'host': network_target(url), 'operation': request_operation(url)})
    def request(self, url, *, method='GET', data=None, headers=None, cancelled=None, limit=8 * 1024 * 1024):
        check_cancelled(cancelled)
        operation = request_operation(url)

        def failed(error, message):
            check_cancelled(cancelled)
            record_request_error(error, operation)
            raise StoreError(f'{operation}失败：{message}') from None

        request_headers = {'User-Agent': USER_AGENT, 'Accept': 'application/json,text/html,*/*',
                           'Referer': FAVORITES_URL, 'Cache-Control': 'no-cache'}
        request_headers.update(headers or {})
        req = urllib.request.Request(url, data=data, headers=request_headers, method=method)
        with self.lock:
            check_cancelled(cancelled)
            try:
                proxies = proxy_settings('store')
                if proxies != self._proxies:
                    self.opener = self._build_opener(proxies)
                    self._proxies = proxies
                started = time.monotonic()
                log_event('DEBUG', 'store.request_started', operation=operation, method=method,
                          proxy_mode=proxy_mode('store'))
                with self.opener.open(req, timeout=12) as response:
                    log_event('DEBUG', 'store.http_response', status=getattr(response, 'status', None))
                    chunks, size = [], 0
                    while True:
                        check_cancelled(cancelled)
                        chunk = response.read(65536)
                        if not chunk:
                            break
                        size += len(chunk)
                        if size > limit:
                            raise StoreError('商城返回的数据过大，已停止读取。')
                        chunks.append(chunk)
                    body = b''.join(chunks)
                    if response.headers.get('Content-Encoding') == 'gzip' or body.startswith(b'\x1f\x8b'):
                        body = gzip.decompress(body)
                        if len(body) > limit:
                            raise StoreError('商城返回的数据过大，已停止读取。')
                    check_cancelled(cancelled)
                    log_event('DEBUG', 'store.request_completed', operation=operation, bytes=len(body),
                              elapsed_ms=round((time.monotonic() - started) * 1000))
                    return body, response.geturl()
            except urllib.error.HTTPError as exc:
                if exc.code == 401:
                    raise SessionExpired('商城登录已失效，请重新登录。') from None
                failed(exc, f'服务器返回 HTTP {exc.code}，请稍后重试。')
            except http.client.InvalidURL as exc:
                failed(exc, '请求地址或代理配置无效，请检查代理设置。')
            except http.client.HTTPException as exc:
                failed(exc, '网络响应异常或不完整，请检查网络或代理后重试。')
            except (gzip.BadGzipFile, EOFError, zlib.error) as exc:
                failed(exc, '压缩响应损坏或不完整，请检查网络或代理后重试。')
            except urllib.error.URLError as exc:
                reason = exc.reason
                if isinstance(reason, ssl.SSLCertVerificationError):
                    failed(exc, '安全证书校验失败，请检查系统时间及网络代理。')
                if isinstance(reason, ssl.SSLError):
                    failed(exc, '安全连接建立失败，请检查网络或代理。')
                if isinstance(reason, socket.gaierror):
                    failed(exc, '域名解析失败，请检查网络或 DNS 设置。')
                if isinstance(reason, TimeoutError):
                    failed(exc, '连接超时，请检查网络后重试。')
                failed(exc, '连接失败，请检查网络或代理后重试。')
            except TimeoutError as exc:
                failed(exc, '连接超时，请检查网络后重试。')
            except StoreError:
                raise
            except ValueError as exc:
                failed(exc, '请求地址或代理配置无效，请检查代理设置。')
            except OSError as exc:
                failed(exc, '连接中断，请检查网络或代理后重试。')

    def clear(self):
        with self.lock:
            self.cookies.clear()


class StoreClient:
    def __init__(self, session=None):
        self.session = session or MemorySession()
        self.account = None
        self.pages_read = 0
        self.favorite_lock = threading.Lock()
        self._login_public_key = ''
        self._auth_keys = {}
        self._auth_expires = 0

    def _json(self, url, *, payload=None, form=None, cancelled=None, headers=None):
        request_headers = {}
        body = None
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
            request_headers = {'Content-Type': 'application/json;charset=UTF-8',
                       'Origin': PASSPORT, 'Referer': PASSPORT + '/login?appId=LC_PUB'}
        if form is not None:
            body = urlencode(form).encode('utf-8')
            request_headers = {'Content-Type': 'application/x-www-form-urlencoded',
                               'Origin': 'https://member.szlcsc.com', 'Referer': FAVORITES_URL}
        request_headers.update(headers or {})
        if url.startswith(PASSPORT + '/api/cas/') and self._auth_keys:
            request_headers['secretkey'] = self._auth_keys['keyId']
        data, final_url = self.session.request(url, method='POST' if body is not None else 'GET', data=body,
                                       headers=request_headers, cancelled=cancelled)
        final = urlsplit(final_url)
        if final.hostname == 'passport.jlc.com' and final.path.startswith('/login'):
            raise SessionExpired('商城登录已失效，请重新登录。')
        try:
            result = json.loads(data)
        except (ValueError, UnicodeError):
            raise StoreError('商城返回了无法识别的数据，请重试。') from None
        if not isinstance(result, dict):
            raise StoreError('商城返回了无法识别的数据，请重试。')
        code = result.get('code')
        log_event('DEBUG', 'store.response_parsed', operation=request_operation(url),
                  code=code if isinstance(code, int) and not isinstance(code, bool) else None,
                  data_type=type(result.get('data')).__name__)
        if self._auth_keys and url.startswith(PASSPORT + '/api/cas/'):
            try:
                result = decode_login_values(result, self._auth_keys['privateHexKey'])
            except Exception as exc:
                record_error(exc, 'login.response_decrypt_failed')
                raise StoreError('登录响应校验失败，请刷新登录后重试。') from None
        if result.get('needLogin') or result.get('msg') in ('needLogin', '请先登录') or result.get('code') in (401, 10219):
            raise SessionExpired('商城登录已失效，请重新登录。', service_code=result.get('code'))
        return result

    def _cas(self, path, payload, cancelled=None):
        result = self._json(PASSPORT + '/api/cas/' + path, payload=payload, cancelled=cancelled)
        if result.get('code') != 200:
            code = result.get('code')
            code = code if isinstance(code, int) and not isinstance(code, bool) else '未知'
            raise StoreError(f'登录服务请求失败（服务代码 {code}），请重新登录。', service_code=result.get('code'))
        data = result.get('data')
        if data is None:
            return {}
        if not isinstance(data, dict):
            raise StoreError('登录服务返回的数据格式异常，请重新登录。')
        return data

    @traced('login.qr_create', level='INFO')
    def start_qr(self, cancelled=None):
        result = self._cas('login/get-official-qrcode', {'appId': 'LC_PUB'}, cancelled)
        token, url = result.get('token'), public_url(result.get('url'))
        if not isinstance(token, str) or not token or not url or urlsplit(url).hostname != 'mp.weixin.qq.com':
            raise StoreError('未能获取官方登录二维码，请重试。')
        try:
            expires = max(1, min(int(result.get('expireSeconds') or 300), 600))
        except (ValueError, TypeError, OverflowError):
            raise StoreError('登录二维码有效期数据异常，请刷新重试。') from None
        image, _ = self.session.request(url, cancelled=cancelled, limit=2 * 1024 * 1024)
        return {'token': token, 'image': image, 'expires': expires}

    @traced('login.qr_poll')
    def scan_status(self, token, cancelled=None):
        result = self._cas('login/get-official-scan-result', {'token': token}, cancelled)
        status = result.get('status')
        log_event('DEBUG', 'login.qr_state', state=status if status in ('CREATED', 'SCANNED', 'EXPIRED', 'SUCCESS', 'LOGIN_SUCCESS', 'REGISTER_SUCCESS') else 'unknown')
        if status == 'SUCCESS':
            return 'LOGIN_SUCCESS'
        if status not in ('CREATED', 'SCANNED', 'EXPIRED', 'LOGIN_SUCCESS', 'REGISTER_SUCCESS'):
            raise StoreError('无法识别扫码状态，请刷新二维码。')
        return status

    @traced('login.qr_confirm', level='INFO')
    def finish_login(self, token, cancelled=None, *, remember=False):
        # The official form creates its flow session immediately before submitting.
        self._cas('login/get-init-session', {'appId': 'LC_PUB', 'clientType': 'WEB'}, cancelled)
        result = self._json(PASSPORT + '/api/cas/login/with-official-qrcode',
                            payload={'token': token, 'isAutoLogin': bool(remember)}, cancelled=cancelled)
        return self._finish_auth(result, cancelled)

    @traced('login.authorization_exchange')
    def _finish_auth(self, result, cancelled=None):
        if result.get('code') not in (200, 2017):
            flow = result.get('code')
            raise StoreError(f'此账号需要补充绑定或身份验证，当前登录未完成（{flow}）。', service_code=flow)
        info = result.get('data') or {}
        if not isinstance(info, dict):
            raise StoreError('登录授权数据格式异常，请重新登录。')
        code = info.get('authCode') or info.get('code')
        if not code:
            checked = self._cas('sso/check-login', {'appId': 'LC_PUB'}, cancelled)
            code = checked.get('code') if checked.get('isLogin') else None
        if not isinstance(code, str) or not code:
            raise StoreError('登录确认后未取得商城授权，请重新登录。')
        self.session.request(STOREFRONT + '/cas/login?' + urlencode({'code': code}), cancelled=cancelled)
        account = self.account_info(cancelled)
        check_cancelled(cancelled)
        self.account = account
        log_event('INFO', 'login.authenticated')
        return account

    @traced('login.encryption_initialize')
    def _prepare_credentials(self, cancelled=None):
        if self._auth_keys and self._login_public_key and time.monotonic() < self._auth_expires:
            return
        self._auth_keys = {}
        result = self._json(PASSPORT + '/api/cas/config/get-public-key', cancelled=cancelled)
        public_key = result.get('data')
        if result.get('code') != 200 or not isinstance(public_key, str) or not re.fullmatch(r'04[0-9a-fA-F]{128}', public_key):
            raise StoreError('获取账号登录加密配置失败，请稍后重试。')
        # The official key exchange sends an empty POST, not a JSON object.
        body, _ = self.session.request(PASSPORT + '/api/cas/secret/update', method='POST',
            headers={'Content-Type': 'application/json;charset=UTF-8', 'Origin': PASSPORT,
                     'Referer': PASSPORT + '/login?appId=LC_PUB'}, cancelled=cancelled)
        try:
            result = json.loads(body)
            keys = result['data']
            if (result.get('code') != 200 or not isinstance(keys, dict) or not keys.get('keyId')
                    or not re.fullmatch(r'[0-9a-fA-F]{64}', keys.get('privateHexKey', ''))
                    or not re.fullmatch(r'04[0-9a-fA-F]{128}', keys.get('publicHexKey', ''))):
                raise ValueError()
        except (ValueError, KeyError, TypeError):
            raise StoreError('初始化登录加密通道失败，请稍后重试。') from None
        self._login_public_key = public_key
        self._auth_keys = {key: keys[key] for key in ('keyId', 'privateHexKey', 'publicHexKey')}
        self._auth_expires = time.monotonic() + 1200

    def _encrypted_field(self, value):
        try:
            return encrypt_login_value(value, self._login_public_key)
        except Exception:
            raise StoreError('登录信息加密失败，请刷新登录后重试。') from None

    def _credential_response(self, result, scene, *, success_codes=(200, 2017)):
        code = result.get('code')
        if code == 102280:
            raise CaptchaRequired(scene)
        if code in (29001, 29003):
            self._auth_keys = {}
            self._login_public_key = ''
            raise StoreError('登录加密会话已更新，请再次提交。', service_code=code)
        if code == 10210:
            raise StoreError('此手机号尚未注册商城账号，请使用已有账号登录。', service_code=code)
        if code not in success_codes:
            message = text(result.get('message'), 200)
            raise StoreError(message or '登录验证失败，请检查输入后重试。', service_code=code)
        return result

    @traced('login.password', level='INFO')
    def login_password(self, username, password, cancelled=None, *, remember=False, captcha_ticket=''):
        username = username.strip()
        if not username or len(username) > 160 or not password or len(password) > 200:
            raise StoreError('请输入有效账号和登录密码。')
        self._prepare_credentials(cancelled)
        self._cas('login/get-init-session', {'appId': 'LC_PUB', 'clientType': 'WEB'}, cancelled)
        payload = {'username': self._encrypted_field(username), 'password': self._encrypted_field(password),
                   'isAutoLogin': bool(remember), 'appId': 'LC_PUB'}
        if captcha_ticket:
            payload['captchaTicket'] = captcha_ticket
        else:
            risk = self._json(PASSPORT + '/api/cas/secure/check-login-risk',
                              payload={'username': payload['username']}, cancelled=cancelled)
            self._credential_response(risk, 'pass_word_login', success_codes=(200,))
        result = self._json(PASSPORT + '/api/cas/login/with-password', payload=payload, cancelled=cancelled)
        if result.get('code') in (2011, 2012, 2013, 2014, 2015, 2016, 2018, 20191):
            return self._finish_auth(result, cancelled)
        return self._finish_auth(self._credential_response(result, 'pass_word_login'), cancelled)

    @staticmethod
    def _phone_number(phone):
        phone = re.sub(r'[ -]', '', phone.strip())
        if phone.startswith('+86'):
            phone = phone[3:]
        if not re.fullmatch(r'1[0-9]{10}', phone):
            raise StoreError('请输入有效的 11 位手机号码。')
        return phone

    @traced('login.sms_send', level='INFO')
    def send_sms(self, phone, cancelled=None, *, captcha_ticket=''):
        phone = self._phone_number(phone)
        self._prepare_credentials(cancelled)
        encrypted = self._encrypted_field(phone)
        if not captcha_ticket:
            risk = self._json(PASSPORT + '/api/cas/secure/check-sms-risk',
                payload={'recipient': encrypted, 'sceneType': 'login'}, cancelled=cancelled)
            self._credential_response(risk, 'login', success_codes=(200,))
        payload = {'phoneNumber': encrypted, 'appId': 'LC_PUB'}
        if captcha_ticket:
            payload['captchaTicket'] = captcha_ticket
        result = self._json(PASSPORT + '/api/cas/login/with-sms/send-code', payload=payload, cancelled=cancelled)
        self._credential_response(result, 'login', success_codes=(200,))
        return {'sent': True}

    @traced('login.sms_verify', level='INFO')
    def login_sms(self, phone, code, cancelled=None, *, remember=False, captcha_ticket=''):
        phone = self._phone_number(phone)
        if not re.fullmatch(r'[0-9]{4,8}', code.strip()):
            raise StoreError('请输入有效的短信验证码。')
        self._prepare_credentials(cancelled)
        self._cas('login/get-init-session', {'appId': 'LC_PUB', 'clientType': 'WEB'}, cancelled)
        payload = {'phoneNumber': self._encrypted_field(phone), 'validateCode': code.strip(),
                   'isAutoLogin': bool(remember), 'appId': 'LC_PUB'}
        if captcha_ticket:
            payload['captchaTicket'] = captcha_ticket
        result = self._json(PASSPORT + '/api/cas/login/with-sms/check-code', payload=payload, cancelled=cancelled)
        if result.get('code') in (2011, 2012, 2013, 2014, 2015, 2016, 2018, 20191):
            return self._finish_auth(result, cancelled)
        return self._finish_auth(self._credential_response(result, 'login'), cancelled)

    @traced('login.challenge_image')
    def captcha_image(self, scene, cancelled=None):
        if scene not in ('pass_word_login', 'login'):
            raise StoreError('验证码场景无效，请重新登录。')
        result = self._json(PASSPORT + '/api/cas/captcha/get-static-verify-img?' + urlencode({'sceneType': scene}), cancelled=cancelled)
        data = result.get('data') or {}
        try:
            raw = data['verifyImg']
            if result.get('code') != 200 or not data.get('ticketCode') or not isinstance(raw, str) or len(raw) > 3 * 1024 * 1024:
                raise ValueError()
            image = base64.b64decode(raw.split(',', 1)[-1], validate=True)
            return {'image': image, 'ticket_code': data['ticketCode'], 'scene': scene}
        except (ValueError, KeyError, TypeError):
            raise StoreError('获取图片验证码失败，请刷新重试。') from None

    @traced('login.challenge_verify')
    def check_captcha(self, scene, ticket_code, verify_code, cancelled=None):
        if not ticket_code or not verify_code.strip() or scene not in ('pass_word_login', 'login'):
            raise StoreError('请输入图片验证码。')
        result = self._json(PASSPORT + '/api/cas/captcha/check-static-verify-img',
            payload={'ticketCode': ticket_code, 'verifyCode': verify_code.strip(), 'sceneType': scene}, cancelled=cancelled)
        data = result.get('data') or {}
        if result.get('code') != 200 or not isinstance(data, dict) or not data.get('checkSuccess') or not data.get('captchaTicket'):
            raise StoreError('图片验证码错误或已过期，请刷新后重试。')
        return data['captchaTicket']

    @traced('session.verify')
    def account_info(self, cancelled=None):
        result = self._json(STOREFRONT + '/cas/user/info', cancelled=cancelled)
        data = result.get('result')
        if (result.get('code') != 200 or not isinstance(data, dict) or not data.get('customerCode')
                or data.get('customerLogin', 1) not in (1, '1')):
            raise SessionExpired('未建立商城登录状态，请重新登录。')
        return {'code': text(data.get('customerCode')),
                'name': text(data.get('customerName') or data.get('nickName') or data.get('customerCode'))}

    @traced('session.restore', level='INFO')
    def restore_account(self, cancelled=None):
        try:
            return self.account_info(cancelled)
        except SessionExpired:
            checked = self._cas('sso/check-login', {'appId': 'LC_PUB'}, cancelled)
            code = checked.get('code') if checked.get('isLogin') else None
            if not code:
                auto = self._json(PASSPORT + '/api/cas/login/auto-login-with-cookie',
                                  payload={'appId': 'LC_PUB'}, cancelled=cancelled)
                data = auto.get('data') or {}
                code = data.get('code') if auto.get('code') == 200 and isinstance(data, dict) else None
            if not isinstance(code, str) or not code:
                raise SessionExpired('已保存的商城登录已失效，请重新登录。')
            self.session.request(STOREFRONT + '/cas/login?' + urlencode({'code': code}), cancelled=cancelled)
            return self.account_info(cancelled)

    def search_page(self, keyword, page=1, cancelled=None):
        keyword = keyword.strip()[:160]
        if not keyword or not isinstance(page, int) or page < 1:
            raise StoreError('请输入关键词和有效页码。')
        # The storefront accepts its native page size (30); size 100 returns null.
        payload = {'keyword': keyword, 'currentPage': page, 'pageSize': 30,
                   'catalogIdFilter': '', 'brandIdFilter': '', 'standardFilter': '', 'arrangeFilter': '',
                   'labelFilter': '', 'authenticationFilter': '', 'sortNumber': 0, 'satisfyStockType': '',
                   'startPrice': '', 'endPrice': '', 'demandNumber': '', 'spotFilter': 1, 'discountFilter': 1,
                   'hasDataFile': False, 'brandPlaceFilter': '', 'secondKeyword': '',
                   'queryParameterValue': '', 'lastParamName': ''}
        result = self._json(STORE_SEARCH_URL, payload=payload, cancelled=cancelled,
                            headers={'Origin': 'https://so.szlcsc.com', 'Referer': 'https://so.szlcsc.com/global.html'})
        return search_page_products(result, page)

    @traced('catalog.search_page', lambda self, keyword, page=1, **kw: {'page': page, 'query_length': len(keyword)}, level='INFO')
    def search_results_page(self, keyword, page=1, cancelled=None):
        """Read just the native pages covering one 50-result software page."""
        keyword = keyword.strip()[:160]
        if not keyword or not isinstance(page, int) or page < 1:
            raise StoreError('请输入关键词和有效页码。')
        start = (page - 1) * SEARCH_PAGE_SIZE
        native_size = 30
        native_page = start // native_size + 1
        first = self.search_page(keyword, native_page, cancelled)
        total = first['total']
        pages = (total + SEARCH_PAGE_SIZE - 1) // SEARCH_PAGE_SIZE
        if page > max(1, pages):
            raise StoreError('该搜索页已不存在，请重新搜索。')
        count = min(SEARCH_PAGE_SIZE, max(0, total - start))
        slots, fingerprints = [], set()
        current = first
        offset = start % native_size
        while True:
            check_cancelled(cancelled)
            if current['size'] != native_size or current['total'] != total:
                raise StoreError('搜索分页或结果总数已变化，请重新搜索。')
            fingerprint = tuple(p['part'] for p in current['items'])
            if fingerprint and fingerprint in fingerprints:
                raise StoreError('商城搜索翻页未变化，请重试当前页。')
            fingerprints.add(fingerprint)
            slots.extend(current['positions'][offset:offset + count - len(slots)])
            if len(slots) >= count:
                break
            if native_page >= current['pages']:
                raise StoreError('商城搜索页面不完整，请重试当前页。')
            native_page += 1
            offset = 0
            current = self.search_page(keyword, native_page, cancelled)
        products, seen = [], set()
        for item in slots:
            if item and item['part'] not in seen:
                seen.add(item['part'])
                products.append(item)
        exact = keyword.upper()
        products.sort(key=lambda p: (p['part'] != exact, p['title'].upper() != exact))
        check_cancelled(cancelled)
        log_event('INFO', 'catalog.search_result', page=page, total=total, count=len(products), pages=pages)
        return {'items': products, 'total': total, 'page': page, 'pages': pages, 'size': SEARCH_PAGE_SIZE}

    def search(self, keyword, cancelled=None):
        keyword = keyword.strip()[:160]
        if not keyword:
            return []
        return self.search_results_page(keyword, cancelled=cancelled)['items']

    def _catalog_search(self, keyword, cancelled=None):
        keyword = keyword.strip()[:160]
        if not keyword:
            return []
        payload = self._json(SEARCH_URL + '?' + urlencode({'wd': keyword}), cancelled=cancelled)
        return catalog_products(payload, keyword)

    @traced('catalog.detail', lambda self, product, *a, **kw: {'part': safe_part(product.get('part') if isinstance(product, dict) else product)})
    def product(self, product, cancelled=None):
        base = dict(product) if isinstance(product, dict) else {'part': str(product).strip().upper()}
        part = base.get('part', '')
        if not PART.fullmatch(part):
            raise StoreError('商品编号无效。')
        detail_error = None
        official = None
        try:
            identity = product_id(base.get('product_id'))
            if not identity:
                match = re.fullmatch(r'https://item\.szlcsc\.com/([0-9]+)\.html', base.get('store_url', ''))
                identity = match[1] if match else ''
            if not identity:
                found = next((p for p in self.search_page(part, cancelled=cancelled)['items'] if p['part'] == part), None)
                identity = found['product_id'] if found else ''
            if identity:
                body, _ = self.session.request(f'https://item.szlcsc.com/{identity}.html', cancelled=cancelled)
                official = product_page(body, part)
        except StoreError as exc:
            detail_error = exc
        # EDA resources supplement storefront details; an absent EDA row must not
        # hide an otherwise valid storefront product or its original images.
        catalog = None
        try:
            catalog = next((p for p in self._catalog_search(part, cancelled) if p['part'] == part), None)
        except StoreError:
            pass
        check_cancelled(cancelled)
        if official:
            commerce = {'stock', 'price_tiers', 'min_quantity', 'unit'}
            base.update({key: value for key, value in official.items()
                         if (key not in commerce or key not in base) and (value or key not in base)})
            if catalog:
                for key in ('model', 'resources'):
                    base[key] = catalog[key]
            return base
        if catalog:
            base.update({key: value for key, value in catalog.items() if value or key not in base})
            if detail_error:
                base['detail_warning'] = str(detail_error) + ' 当前显示元件目录资料。'
            return base
        if detail_error:
            raise detail_error
        return None

    @traced('favorites.check')
    def favorite_state(self, identity, cancelled=None):
        identity = product_id(identity)
        if not identity:
            raise StoreError('此商品缺少商城编号，无法加入账号收藏。')
        result = self._json(FAVORITE_STATE_URL + '?' + urlencode({'productIds': identity}), cancelled=cancelled)
        rows = result.get('result')
        if result.get('code') != 200 or 'result' not in result or rows is not None and not isinstance(rows, list):
            raise StoreError('无法确认账号收藏状态，请稍后重试。')
        if rows is None:
            return False
        return any(product_id(row.get('productId') if isinstance(row, dict) else row) == identity
                   for row in rows)

    @traced('favorites.add', lambda self, product, *a, **kw: {'part': safe_part(product.get('part'))}, level='INFO')
    def add_favorite(self, product, cancelled=None):
        identity = product_id(product.get('product_id')) if isinstance(product, dict) else ''
        if not identity:
            raise StoreError('此商品缺少商城编号，无法加入账号收藏。')
        with self.favorite_lock:
            check_cancelled(cancelled)
            self.account_info(cancelled)
            # The official endpoint toggles. Check first so repeated adds never
            # cancel an existing favorite; never retry a toggle after an error.
            if self.favorite_state(identity, cancelled):
                return {'product': product, 'added': False}
            result = self._json(FAVORITE_TOGGLE_URL + '?' + urlencode({'productId': identity}), cancelled=cancelled)
            if result.get('code') != 200:
                raise StoreError('加入账号收藏失败，请稍后重试。')
            if not self.favorite_state(identity, cancelled):
                raise StoreError('未能确认收藏成功，请刷新账号收藏后重试。')
            return {'product': product, 'added': True}

    @traced('favorites.remove', lambda self, product, *a, **kw: {'part': safe_part(product.get('part'))}, level='INFO')
    def remove_favorite(self, product, cancelled=None):
        identity = product_id(product.get('product_id')) if isinstance(product, dict) else ''
        if not identity:
            raise StoreError('此商品缺少商城编号，无法取消账号收藏。')
        with self.favorite_lock:
            check_cancelled(cancelled)
            self.account_info(cancelled)
            if not self.favorite_state(identity, cancelled):
                return {'product': product, 'removed': False}
            result = self._json(FAVORITE_CANCEL_URL, form={'productIds': identity}, cancelled=cancelled)
            if result.get('code') != 200:
                raise StoreError('取消账号收藏失败，请稍后重试。')
            if self.favorite_state(identity, cancelled):
                raise StoreError('未能确认取消收藏，请刷新账号收藏后重试。')
            return {'product': product, 'removed': True}

    @traced('image.fetch', lambda self, url, *a, **kw: {'host': network_target(url)})
    def image(self, url, cancelled=None):
        url = public_url(url)
        host = urlsplit(url).hostname if url else ''
        if not host or not (host == 'szlcsc.com' or host.endswith('.szlcsc.com')
                            or host == 'lceda.cn' or host.endswith('.lceda.cn')):
            return b''
        return self.session.request(url, cancelled=cancelled, limit=16 * 1024 * 1024)[0]

    @traced('favorites.read', level='INFO')
    def favorites(self, cancelled=None, on_page=None):
        self.account_info(cancelled)
        result, seen_parts, seen_pages = [], set(), set()
        self.pages_read = 0
        for page in range(1, MAX_PAGES + 1):
            check_cancelled(cancelled)
            payload = self._json(FAVORITES_API + '?' + urlencode({'currentPage': page, 'pageSize': 100,
                                 'catalogId': '', 'brandId': '', 'keyword': '', 'folderUuid': '', 'favoriteTime': ''}),
                                 cancelled=cancelled)
            products, total, size = favorite_products(payload, page)
            fingerprint = tuple(item['part'] for item in products)
            if fingerprint and fingerprint in seen_pages:
                raise StoreError('商城收藏翻页未变化，已停止读取，避免重复或漏读。')
            seen_pages.add(fingerprint)
            for item in products:
                if item['part'] not in seen_parts:
                    seen_parts.add(item['part'])
                    result.append(item)
            if len(result) > MAX_PARTS:
                raise StoreError(f'收藏超过 {MAX_PARTS:,} 个，已停止读取。')
            self.pages_read = page
            log_event('DEBUG', 'favorites.page_read', page=page, count=len(products), collected=len(result), total=total)
            check_cancelled(cancelled)
            if on_page:
                on_page(page, products)
            if page * size >= total:
                return result
        raise StoreError(f'收藏超过 {MAX_PAGES} 页，已停止读取。')

    @traced('session.logout', level='INFO')
    def clear(self):
        self.account = None
        self._auth_keys = {}
        self._login_public_key = ''
        self.session.clear()
