"""Batch 3D model downloads from EasyEDA component data."""
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

from easyeda2kicad.easyeda.easyeda_api import (
    API_ENDPOINT, ENDPOINT_3D_MODEL, ENDPOINT_3D_MODEL_STEP, EasyedaApi,
)
from easyeda2kicad.easyeda.easyeda_importer import Easyeda3dModelImporter
from easyeda2kicad.kicad.export_kicad_3d_model import Exporter3dModelKicad

from errors import Cancelled, DownloadError
import certifi
import urllib3
from urllib3.util import Retry, Timeout

UPSTREAM_URL = 'https://github.com/uPesy/easyeda2kicad.py'
SOURCE_LABEL = 'JLCEDA/EasyEDA 官方库'
SVG_ENDPOINTS = (
    'https://lceda.cn/api/products/{part}/svgs',
    'https://easyeda.com/api/products/{part}/svgs',
)
COMPONENT_ENDPOINTS = ('https://lceda.cn/api/products/{part}/components', API_ENDPOINT.replace('{lcsc_id}', '{part}'))


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


def atomic_write(path: Path, data: bytes, overwrite: bool = False) -> bool:
    """Replace complete files only. Partial downloads never become final files."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and not overwrite and (not path.is_file() or path.stat().st_size):
        return False
    fd, name = tempfile.mkstemp(prefix='.lcsc-', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
        if not overwrite and path.exists() and (not path.is_file() or path.stat().st_size):
            return False
        os.replace(name, path)
        return True
    finally:
        if os.path.exists(name):
            os.unlink(name)


class NetworkApi(EasyedaApi):
    """Upstream-compatible API with bounded requests and cancellation checks."""
    def __init__(self, cancelled: threading.Event | None = None, use_cache=True):
        super().__init__()
        self.cancelled = cancelled or threading.Event()
        self.headers['Accept-Encoding'] = 'gzip'
        self.use_cache = use_cache
        self.proxies = urllib.request.getproxies()

    def _create_ssl_context(self):
        return shared_ssl_context()

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
                if self.use_cache:
                    PAYLOAD_CACHE.put(key, raw, 600)
                return data
            except DownloadError as exc:
                last_error = exc
        raise last_error

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
    overwrite: bool = False


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
        model = Easyeda3dModelImporter(data, download_raw_3d_model=False, api=api).output
        if model:
            model_name = model.name
    except (KeyError, TypeError, ValueError, IndexError, AttributeError):
        # A missing/malformed 3D association must not hide a valid part title.
        pass
    api.check_cancelled()
    return {'title': title, 'model': model_name}


def download_part(part: str, options: Options, api: NetworkApi,
                  progress: Callable[[str, str], None] | None = None) -> Result:
    if not options.formats or any(fmt not in ('STEP', 'WRL', 'OBJ') for fmt in options.formats):
        raise DownloadError('请选择 STEP、WRL 或 OBJ 格式')
    notify = progress or (lambda phase, title: None)
    notify('查询器件', '')
    data = api.get_cad_data_of_component(part)
    title = str(data.get('title') or '').strip()
    product_id = (data.get('szlcsc') or data.get('lcsc') or {}).get('id')
    store_url = f'https://item.szlcsc.com/{product_id}.html' if product_id else f'https://so.szlcsc.com/global.html?k={part}'
    result = Result(part, '失败', title=title, store_url=store_url)
    api.check_cancelled()
    folder = options.destination / f'{safe_filename(title or "未命名器件")}_{part}'
    result.folder = str(folder)
    completed, skipped, errors = [], [], []
    model, model_error = None, ''
    try:
        model = Easyeda3dModelImporter(data, download_raw_3d_model=False, api=api).output
    except (KeyError, TypeError, ValueError, IndexError):
        model_error = '器件模型信息缺失或格式不受支持'
    if not model:
        result.status = '失败' if model_error else '无模型'
        result.message = model_error or '官方库中没有关联的 3D 模型'
        return result
    result.model = model.name

    def save_payload(fmt: str, path: Path, payload: bytes):
        if atomic_write(path, payload, overwrite=options.overwrite):
            completed.append(fmt)
        else:
            skipped.append(fmt)
        result.files.append(str(path))

    basename = f'{part}_{safe_filename(model.name)}'
    paths = {fmt: folder / f'{basename}.{fmt.lower()}' for fmt in options.formats}
    needed = {fmt for fmt, path in paths.items()
              if options.overwrite or not path.is_file() or not path.stat().st_size}
    # STEP and OBJ are independent network transfers; WRL uses the same OBJ.
    # Never download one OBJ twice, including when it is unavailable or fails.
    with ThreadPoolExecutor(max_workers=2, thread_name_prefix='model-format') as pool:
        step = pool.submit(api.get_step_3d_model, model.uuid) if 'STEP' in needed else None
        obj = pool.submit(api.get_raw_3d_model_obj, model.uuid) if needed & {'OBJ', 'WRL'} else None
        for fmt in options.formats:
            api.check_cancelled()
            path = paths[fmt]
            if fmt not in needed:
                skipped.append(fmt)
                result.files.append(str(path))
                continue
            notify(f'下载 {fmt}', title)
            try:
                if fmt == 'STEP':
                    payload = step.result()
                else:
                    raw_obj = obj.result()
                    if fmt == 'OBJ':
                        payload = raw_obj.encode('utf-8') if raw_obj else None
                    elif raw_obj:
                        model.raw_obj = raw_obj
                        exporter = Exporter3dModelKicad(model)
                        payload = exporter.output.raw_wrl.encode('utf-8') if exporter.output else None
                    else:
                        payload = None
                api.check_cancelled()
                if not payload:
                    errors.append(f'{fmt} 不可用')
                else:
                    save_payload(fmt, path, payload)
            except Cancelled:
                raise
            except Exception as exc:
                errors.append(f'{fmt}：{exc}')
    if completed or skipped:
        result.status = '部分完成' if errors else ('已存在' if not completed else '成功')
        descriptions = []
        if completed:
            descriptions.append('已保存 ' + ' / '.join(completed))
        if skipped:
            descriptions.append('已存在 ' + ' / '.join(skipped))
        result.message = '；'.join(descriptions + errors)
        metadata = {
            'part': part, 'title': title, 'model': model.name if model else '', 'model_uuid': model.uuid if model else '',
            'source': SOURCE_LABEL, 'source_urls': ['https://lceda.cn/', 'https://easyeda.com/'],
            'store_url': store_url, 'downloaded_at': time.strftime('%Y-%m-%dT%H:%M:%S%z'),
            'upstream': UPSTREAM_URL, 'upstream_version': '1.0.1',
            'files': [Path(x).name for x in result.files],
            'library_notice': data.get('packageDetail', {}).get('dataStr', {}).get('head', {}).get('licence', ''),
            'symbol_library_notice': data.get('dataStr', {}).get('head', {}).get('licence', ''),
        }
        try:
            atomic_write(folder / 'model-info.json', json.dumps(metadata, ensure_ascii=False, indent=2).encode('utf-8'), overwrite=True)
        except OSError as exc:
            result.status = '部分完成'
            result.message += f'；来源信息保存失败：{exc}'
    else:
        result.message = '；'.join(errors) or '没有可保存的文件'
    return result


def download_batch(parts, options, cancelled=None, progress=None, on_result=None, api=None, workers=3):
    """Download with bounded concurrency; callbacks may finish out of row order."""
    if not parts:
        return []
    cancelled = cancelled or threading.Event()
    api = api or NetworkApi(cancelled, use_cache=not options.overwrite)
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
