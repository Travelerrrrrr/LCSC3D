"""Download official models and export native AD symbol/footprint libraries."""
from __future__ import annotations

import gzip
import json
import os
import re
import ssl
import tempfile
import threading
import time
import urllib.request
from urllib.parse import urlsplit
from collections import OrderedDict
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Callable

from errors import Cancelled, DownloadError
from model3d import document, model_reference
from altium import export_schlib, export_pcblib
import certifi
import urllib3
from urllib3.util import Retry, Timeout

SVG_ENDPOINTS = (
    'https://lceda.cn/api/products/{part}/svgs',
    'https://easyeda.com/api/products/{part}/svgs',
)
COMPONENT_ENDPOINTS = ('https://lceda.cn/api/products/{part}/components',
                       'https://easyeda.com/api/products/{part}/components')
ENDPOINT_3D_MODEL = 'https://modules.easyeda.com/3dmodel/{uuid}'
ENDPOINT_3D_MODEL_STEP = 'https://modules.easyeda.com/qAxj6KHrDKw4blvCG8QJPs7Y/{uuid}'


class PayloadCache:
    """A bounded, process-local cache; model UUIDs identify immutable payloads."""
    def __init__(self, max_bytes=16 * 1024 * 1024):
        self.max_bytes = max_bytes
        self.size = 0
        self.items = OrderedDict()
        self.lock = threading.Lock()

    def get(self, key):
        with self.lock:
            entry = self.items.pop(key, None)
            if entry is None:
                return None
            expires, value = entry
            if expires < time.monotonic():
                self.size -= len(value)
                return None
            self.items[key] = entry
            return value

    def put(self, key, value, ttl):
        if len(value) > self.max_bytes:
            return
        with self.lock:
            old = self.items.pop(key, None)
            if old:
                self.size -= len(old[1])
            self.items[key] = (time.monotonic() + ttl, value)
            self.size += len(value)
            while self.size > self.max_bytes:
                _, (_, removed) = self.items.popitem(last=False)
                self.size -= len(removed)


PAYLOAD_CACHE = PayloadCache()


@lru_cache(maxsize=1)
def shared_ssl_context():
    context = ssl.create_default_context()
    context.load_verify_locations(cafile=certifi.where())
    return context


@lru_cache(maxsize=4)
def connection_pool(proxy=None):
    kwargs = dict(num_pools=8, maxsize=6, block=True, ssl_context=shared_ssl_context())
    return urllib3.ProxyManager(proxy, **kwargs) if proxy else urllib3.PoolManager(**kwargs)


def parse_part_numbers(text: str) -> tuple[list[str], list[str], int]:
    """Accept multiline/comma/space-separated IDs; preserve order, deduplicate."""
    ids, invalid, seen = [], [], set()
    duplicates = 0
    for token in re.split(r'[\s,;，；、|]+', text.strip()):
        token = token.strip('\"\'[]()')
        if not token:
            continue
        if not re.fullmatch(r'[cC][0-9]+', token):
            if token not in invalid:
                invalid.append(token)
            continue
        part = token.upper()
        if part in seen:
            duplicates += 1
        else:
            seen.add(part)
            ids.append(part)
    return ids, invalid, duplicates


def safe_filename(name: str) -> str:
    clean = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', name).strip(' .')[:115]
    if not clean:
        return 'model'
    if re.fullmatch(r'(CON|PRN|AUX|NUL|COM[0-9]|LPT[0-9])', clean, re.I):
        return '_' + clean
    return clean


def atomic_write(path: Path, data: bytes) -> None:
    """Replace complete files only. Partial downloads never become final files."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.lcsc-', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


class NetworkApi:
    """Official component/model APIs with bounded requests and cancellation."""
    def __init__(self, cancelled: threading.Event | None = None, use_cache=True):
        self.cancelled = cancelled or threading.Event()
        self.headers = {'User-Agent': 'LCSC3D/2.0.0', 'Accept-Encoding': 'gzip',
                        'Accept': '*/*'}
        self.use_cache = use_cache
        self.proxies = urllib.request.getproxies()

    def check_cancelled(self):
        if self.cancelled.is_set():
            raise Cancelled()

    def fetch(self, url: str, optional: bool = False) -> bytes | None:
        for attempt in range(2):
            self.check_cancelled()
            parsed = urlsplit(url)
            proxy = None if urllib.request.proxy_bypass(parsed.hostname or '') else self.proxies.get(parsed.scheme)
            response = None
            try:
                response = connection_pool(proxy).request('GET', url, headers=self.headers, preload_content=False,
                    timeout=Timeout(connect=6, read=18), pool_timeout=18,
                    retries=Retry(total=2, connect=0, read=0, status=0, redirect=2))
                if optional and response.status in (404, 410):
                    return None
                if response.status >= 400:
                    if response.status < 500 or attempt:
                        raise DownloadError(f'服务器返回 HTTP {response.status}')
                else:
                    chunks = []
                    while True:
                        self.check_cancelled()
                        block = response.read(64 * 1024, decode_content=True)
                        if not block:
                            break
                        chunks.append(block)
                    raw = b''.join(chunks)
                    response.release_conn()
                    response = None
                    return gzip.decompress(raw) if raw.startswith(b'\x1f\x8b') else raw
            except (urllib3.exceptions.HTTPError, TimeoutError, OSError) as exc:
                self.check_cancelled()
                if attempt:
                    raise DownloadError(f'网络请求失败：{getattr(exc, "reason", exc)}') from exc
            finally:
                if response is not None:
                    response.close()
                    response.release_conn()
            if self.cancelled.wait(0.7):
                raise Cancelled()
        raise DownloadError('网络请求失败')

    def get_info_from_easyeda_api(self, lcsc_id: str) -> dict:
        if not re.fullmatch(r'C[0-9]+', lcsc_id):
            raise DownloadError('请使用有效的立创 C 编号')
        key = 'component:' + lcsc_id
        raw = PAYLOAD_CACHE.get(key) if self.use_cache else None
        if raw is not None:
            self.check_cancelled()
            return json.loads(raw)
        for template in COMPONENT_ENDPOINTS:
            self.check_cancelled()
            try:
                raw = self.fetch(template.format(part=lcsc_id))
                try:
                    data = json.loads(raw or b'{}')
                except (ValueError, UnicodeDecodeError) as exc:
                    raise DownloadError('接口返回了无法识别的数据，请稍后重试') from exc
                if not isinstance(data, dict) or not data.get('success') or not isinstance(data.get('result'), dict) or not data['result']:
                    raise DownloadError('未找到器件，请核对立创 C 编号')
                data['source_url'] = template.format(part=lcsc_id)
                if self.use_cache:
                    PAYLOAD_CACHE.put(key, json.dumps(data, ensure_ascii=False).encode('utf-8'), 600)
                return data
            except DownloadError as exc:
                last_error = exc
        raise last_error

    def get_cad_data_of_component(self, part: str) -> dict:
        response = self.get_info_from_easyeda_api(part)
        data = dict(response['result'])
        data['_source_url'] = response.get('source_url', '')
        for key in ('dataStr', 'packageDetail'):
            if data.get(key):
                data[key] = document(data[key])
        if data.get('packageDetail'):
            package = dict(data['packageDetail'])
            package['dataStr'] = document(package.get('dataStr'))
            data['packageDetail'] = package
        return data

    def get_svg_data_of_component(self, part: str) -> dict:
        """Fetch storefront SVGs; use the official mirror if the first host fails."""
        if not re.fullmatch(r'C[0-9]+', part):
            raise DownloadError('请使用有效的立创 C 编号')
        for template in SVG_ENDPOINTS:
            self.check_cancelled()
            url = template.format(part=part)
            try:
                raw = self.fetch(url)
                try:
                    data = json.loads(raw or b'{}')
                except (ValueError, UnicodeDecodeError) as exc:
                    raise DownloadError('官方 SVG 接口返回了无法识别的数据') from exc
                if not isinstance(data, dict) or not data.get('success') or not isinstance(data.get('result'), list):
                    raise DownloadError('未找到官方 SVG 预览，请核对立创 C 编号')
                self.check_cancelled()
                return {**data, 'source_url': url}
            except DownloadError as exc:
                last_error = exc
        raise DownloadError(f'官方 SVG 预览获取失败：{last_error}') from last_error

    def get_raw_3d_model_obj(self, uuid: str) -> str | None:
        return self._model_payload(uuid, ENDPOINT_3D_MODEL, 'obj')

    def get_step_3d_model(self, uuid: str) -> bytes | None:
        return self._model_payload(uuid, ENDPOINT_3D_MODEL_STEP, 'step')

    def _model_payload(self, uuid, template, kind):
        if not isinstance(uuid, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', uuid):
            raise DownloadError('3D 模型编号不受支持')
        key = kind + ':' + uuid
        self.check_cancelled()
        raw = PAYLOAD_CACHE.get(key) if self.use_cache else None
        if raw is None:
            last_error = None
            for endpoint in (template.replace('modules.easyeda.com', 'modules.lceda.cn'), template):
                try:
                    raw = self.fetch(endpoint.format(uuid=uuid), optional=True)
                    if raw is None:
                        continue
                    if kind == 'step' and b'ISO-10303-21' not in raw[:2048]:
                        raise DownloadError('STEP 接口未返回有效的 STEP 文件')
                    if kind == 'obj':
                        text = raw.decode('utf-8-sig')
                        if '<html' in text[:300].lower() or '<!doctype' in text[:300].lower():
                            raise DownloadError('OBJ 接口返回了网页，请稍后重试')
                    if self.use_cache:
                        PAYLOAD_CACHE.put(key, raw, 3600)
                    break
                except (DownloadError, UnicodeDecodeError) as exc:
                    last_error = exc
                    raw = None
            if raw is None and last_error is not None:
                raise DownloadError(str(last_error)) from last_error
        self.check_cancelled()
        return raw.decode('utf-8-sig') if kind == 'obj' and raw is not None else raw


@dataclass
class Options:
    destination: Path
    formats: tuple[str, ...] = ('STEP',)


@dataclass
class Result:
    part: str
    status: str
    title: str = ''
    model: str = ''
    message: str = ''
    files: list[str] = field(default_factory=list)
    folder: str = ''
    store_url: str = ''


def get_component_metadata(part: str, api: NetworkApi) -> dict[str, str]:
    """Read the part title and linked model name without downloading model files."""
    api.check_cancelled()
    data = api.get_cad_data_of_component(part)
    api.check_cancelled()
    title = str(data.get('title') or '').strip()
    model_name = ''
    try:
        model = model_reference(data)
        if model:
            model_name = model.name
    except (DownloadError, KeyError, TypeError, ValueError, IndexError, AttributeError):
        # A missing/malformed 3D association must not hide a valid part title.
        pass
    api.check_cancelled()
    return {'title': title, 'model': model_name}


def download_part(part: str, options: Options, api: NetworkApi,
                  progress: Callable[[str, str], None] | None = None) -> Result:
    if not options.formats or any(fmt not in ('STEP', 'OBJ', 'SCHLIB', 'PCBLIB') for fmt in options.formats):
        raise DownloadError('请选择 STEP、OBJ、AD 符号库或 AD 封装库')
    notify = progress or (lambda phase, title: None)
    notify('查询器件', '')
    data = api.get_cad_data_of_component(part)
    title = str(data.get('title') or '').strip()
    product_id = (data.get('szlcsc') or data.get('lcsc') or {}).get('id')
    store_url = f'https://item.szlcsc.com/{product_id}.html' if product_id else f'https://so.szlcsc.com/global.html?k={part}'
    result = Result(part, '失败', title=title, store_url=store_url)
    api.check_cancelled()
    component_name = safe_filename(title or '未命名器件')
    folder = options.destination / f'{component_name}_{part}'
    result.folder = str(folder)
    completed, errors = [], []
    model, model_error = None, ''
    try:
        model = model_reference(data)
    except (DownloadError, KeyError, TypeError, ValueError, IndexError) as exc:
        model_error = '器件模型信息缺失或格式不受支持：' + str(exc)
    if not model and not any(fmt in ('SCHLIB', 'PCBLIB') for fmt in options.formats):
        result.status = '失败' if model_error else '无模型'
        result.message = model_error or '官方库中没有关联的 3D 模型'
        return result
    result.model = model.name if model else ''

    basename = f'{part}_{safe_filename(model.name)}' if model else part
    paths = {fmt: folder / (f'{component_name}.SchLib' if fmt == 'SCHLIB' else f'{component_name}.PcbLib' if fmt == 'PCBLIB'
                            else f'{basename}.{fmt.lower()}') for fmt in options.formats}
    with ThreadPoolExecutor(max_workers=2, thread_name_prefix='model-format') as pool:
        step = pool.submit(api.get_step_3d_model, model.uuid) if model and 'STEP' in options.formats else None
        obj = pool.submit(api.get_raw_3d_model_obj, model.uuid) if model and 'OBJ' in options.formats else None
        for fmt in options.formats:
            api.check_cancelled()
            path = paths[fmt]
            notify({'SCHLIB': '导出 AD 符号库', 'PCBLIB': '导出 AD 封装库'}.get(fmt, f'下载 {fmt}'), title)
            try:
                if fmt == 'SCHLIB':
                    payload = export_schlib(data, part, api.check_cancelled)
                elif fmt == 'PCBLIB':
                    payload = export_pcblib(data, part, api.check_cancelled)
                elif not model:
                    errors.append(f'{fmt}：' + (model_error or '官方库中没有关联的 3D 模型'))
                    continue
                elif fmt == 'STEP':
                    payload = step.result()
                else:
                    raw_obj = obj.result()
                    payload = raw_obj.encode('utf-8') if raw_obj else None
                api.check_cancelled()
                if not payload:
                    errors.append(f'{fmt} 不可用')
                else:
                    atomic_write(path, payload)
                    completed.append(fmt)
                    result.files.append(str(path))
            except Cancelled:
                raise
            except Exception as exc:
                errors.append(f'{fmt}：{exc}')
    if completed:
        result.status = '部分完成' if errors else '成功'
        result.message = '；'.join(['已保存 ' + ' / '.join(completed)] + errors)
    else:
        result.message = '；'.join(errors) or '没有可保存的文件'
    return result


def download_batch(parts, options, cancelled=None, progress=None, on_result=None, api=None, workers=3):
    """Download with bounded concurrency; callbacks may finish out of row order."""
    if not parts:
        return []
    cancelled = cancelled or threading.Event()
    api = api or NetworkApi(cancelled, use_cache=False)
    results = [None] * len(parts)

    def run(index, part):
        try:
            if cancelled.is_set():
                raise Cancelled()
            return download_part(part, options, api,
                (lambda status, title: progress(index, status, title)) if progress else None)
        except Cancelled:
            return Result(part, '已取消', message='任务已取消；已保存的完整文件保留')
        except Exception as exc:
            return Result(part, '失败', message=str(exc))

    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix='model-part') as pool:
        remaining = iter(enumerate(parts))
        pending = {}

        def fill_workers():
            while len(pending) < workers:
                item = next(remaining, None)
                if item is None:
                    break
                index, part = item
                pending[pool.submit(run, index, part)] = index

        fill_workers()
        while pending:
            finished, _ = wait(pending, return_when=FIRST_COMPLETED)
            for future in finished:
                index = pending.pop(future)
                result = results[index] = future.result()
                if on_result:
                    on_result(index, result)
            fill_workers()
    return results
