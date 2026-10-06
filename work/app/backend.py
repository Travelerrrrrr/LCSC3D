"""Batch 3D downloads and Altium library exports from EasyEDA component data."""
from __future__ import annotations

import gzip
import json
import os
import re
import tempfile
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from easyeda2kicad.easyeda.easyeda_api import (
    API_ENDPOINT, ENDPOINT_3D_MODEL, ENDPOINT_3D_MODEL_STEP, EasyedaApi,
)
from easyeda2kicad.easyeda.easyeda_importer import Easyeda3dModelImporter
from easyeda2kicad.kicad.export_kicad_3d_model import Exporter3dModelKicad

from altium import ALTIUM_FORMATS, CONVERTER_COMMIT, CONVERTER_URL, export_libraries
from errors import Cancelled, DownloadError

UPSTREAM_URL = 'https://github.com/uPesy/easyeda2kicad.py'
SOURCE_LABEL = 'JLCEDA/EasyEDA 官方库'


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
    def __init__(self, cancelled: threading.Event | None = None):
        super().__init__()
        self.cancelled = cancelled or threading.Event()
        self.headers['Accept-Encoding'] = 'gzip'

    def check_cancelled(self):
        if self.cancelled.is_set():
            raise Cancelled()

    def fetch(self, url: str, optional: bool = False) -> bytes | None:
        for attempt in range(2):
            self.check_cancelled()
            request = urllib.request.Request(url, headers=self.headers)
            try:
                with urllib.request.urlopen(request, timeout=18, context=self.ssl_context) as response:
                    chunks = []
                    while True:
                        self.check_cancelled()
                        block = response.read(64 * 1024)
                        if not block:
                            break
                        chunks.append(block)
                    raw = b''.join(chunks)
                    return gzip.decompress(raw) if raw.startswith(b'\x1f\x8b') else raw
            except urllib.error.HTTPError as exc:
                if optional and exc.code in (404, 410):
                    return None
                if exc.code < 500 or attempt:
                    raise DownloadError(f'服务器返回 HTTP {exc.code}') from exc
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                self.check_cancelled()
                if attempt:
                    raise DownloadError(f'网络请求失败：{getattr(exc, "reason", exc)}') from exc
            if self.cancelled.wait(0.7):
                raise Cancelled()
        raise DownloadError('网络请求失败')

    def get_info_from_easyeda_api(self, lcsc_id: str) -> dict:
        raw = self.fetch(API_ENDPOINT.format(lcsc_id=lcsc_id))
        try:
            data = json.loads(raw or b'{}')
        except (ValueError, UnicodeDecodeError) as exc:
            raise DownloadError('接口返回了无法识别的数据，请稍后重试') from exc
        if not isinstance(data, dict) or not data.get('success') or not data.get('result'):
            raise DownloadError('未找到器件，请核对立创 C 编号')
        return data

    def get_raw_3d_model_obj(self, uuid: str) -> str | None:
        raw = self.fetch(ENDPOINT_3D_MODEL.format(uuid=uuid), optional=True)
        if raw is None:
            return None
        text = raw.decode('utf-8-sig')
        if '<html' in text[:300].lower() or '<!doctype' in text[:300].lower():
            raise DownloadError('OBJ 接口返回了网页，请稍后重试')
        return text

    def get_step_3d_model(self, uuid: str) -> bytes | None:
        raw = self.fetch(ENDPOINT_3D_MODEL_STEP.format(uuid=uuid), optional=True)
        if raw is not None and b'ISO-10303-21' not in raw[:2048]:
            raise DownloadError('STEP 接口未返回有效的 STEP 文件')
        return raw


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


def download_part(part: str, options: Options, api: NetworkApi,
                  progress: Callable[[str, str], None] | None = None) -> Result:
    if not options.formats or any(fmt not in ('STEP', 'WRL', 'OBJ', *ALTIUM_FORMATS) for fmt in options.formats):
        raise DownloadError('请至少选择一种受支持的导出格式')
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
    model_formats = tuple(fmt for fmt in options.formats if fmt not in ALTIUM_FORMATS)
    library_formats = tuple(fmt for fmt in options.formats if fmt in ALTIUM_FORMATS)
    completed, skipped, errors, notes = [], [], [], []
    model, model_error = None, ''
    if model_formats or 'PCBLIB' in library_formats:
        try:
            model = Easyeda3dModelImporter(data, download_raw_3d_model=False, api=api).output
        except (KeyError, TypeError, ValueError, IndexError):
            model_error = '器件模型信息缺失或格式不受支持'
        if model:
            result.model = model.name
        elif model_formats:
            errors.append(model_error or '官方库中没有关联的 3D 模型')

    def save_payload(fmt: str, path: Path, payload: bytes):
        if atomic_write(path, payload, overwrite=options.overwrite):
            completed.append(ALTIUM_FORMATS.get(fmt, fmt))
        else:
            skipped.append(ALTIUM_FORMATS.get(fmt, fmt))
        result.files.append(str(path))

    basename = f'{part}_{safe_filename(model.name)}' if model else part
    step_payload, step_attempted, step_error = None, False, ''
    raw_obj = None
    for fmt in model_formats if model else ():
        api.check_cancelled()
        path = folder / f'{basename}.{fmt.lower()}'
        if path.is_file() and not options.overwrite and path.stat().st_size:
            skipped.append(fmt)
            result.files.append(str(path))
            continue
        notify(f'下载 {fmt}', title)
        try:
            if fmt == 'STEP':
                step_attempted = True
                payload = api.get_step_3d_model(model.uuid)
                step_payload = payload
            else:
                if raw_obj is None:
                    raw_obj = api.get_raw_3d_model_obj(model.uuid)
                if fmt == 'OBJ':
                    payload = raw_obj.encode('utf-8') if raw_obj else None
                elif fmt == 'WRL':
                    if raw_obj:
                        model.raw_obj = raw_obj
                        exporter = Exporter3dModelKicad(model)
                        payload = exporter.output.raw_wrl.encode('utf-8') if exporter.output else None
                    else:
                        payload = None
                else:
                    raise DownloadError(f'不支持的格式：{fmt}')
            api.check_cancelled()
            if not payload:
                errors.append(f'{fmt} 不可用')
            else:
                save_payload(fmt, path, payload)
        except Cancelled:
            raise
        except Exception as exc:
            errors.append(f'{fmt}：{exc}')
            if fmt == 'STEP':
                step_error = str(exc)

    pending, library_details = [], {}
    for fmt in library_formats:
        path = folder / f'{part}.{ALTIUM_FORMATS[fmt]}'
        if path.is_file() and path.stat().st_size and not options.overwrite:
            skipped.append(ALTIUM_FORMATS[fmt])
            result.files.append(str(path))
        else:
            pending.append(fmt)
    if pending:
        if 'PCBLIB' in pending and model and not step_attempted:
            notify('获取封装 STEP', title)
            try:
                existing = folder / f'{basename}.step'
                if existing.is_file() and existing.stat().st_size and not options.overwrite:
                    step_payload = existing.read_bytes()
                    if b'ISO-10303-21' not in step_payload[:2048]:
                        step_payload = None
                if step_payload is None:
                    step_payload = api.get_step_3d_model(model.uuid)
            except Cancelled:
                raise
            except Exception as exc:
                step_error = str(exc)
        api.check_cancelled()
        notify('导出 AD 库', title)
        try:
            exported = export_libraries(data, part, tuple(pending), api.check_cancelled, step_payload)
            library_details = exported.details
            for fmt in pending:
                api.check_cancelled()
                if fmt in exported.payloads:
                    try:
                        save_payload(fmt, folder / f'{part}.{ALTIUM_FORMATS[fmt]}', exported.payloads[fmt])
                    except OSError as exc:
                        errors.append(f'{ALTIUM_FORMATS[fmt]}：{exc}')
                else:
                    errors.append(f'{ALTIUM_FORMATS[fmt]}：{exported.errors.get(fmt, "库文件导出失败")}')
            if 'PCBLIB' in exported.payloads:
                embedded = exported.details.get('footprint', {}).get('step_embedded')
                notes.append('封装已嵌入 STEP' if embedded else '封装已保存，未嵌入 STEP' + (f'：{step_error}' if step_error else '（官方库没有可用模型）'))
        except Cancelled:
            raise
        except Exception as exc:
            errors.append('AD 库：' + str(exc))
    if completed or skipped:
        result.status = '部分完成' if errors else ('已存在' if not completed else '成功')
        descriptions = []
        if completed:
            descriptions.append('已保存 ' + ' / '.join(completed))
        if skipped:
            descriptions.append('已存在 ' + ' / '.join(skipped))
        result.message = '；'.join(descriptions + errors + notes)
        metadata = {
            'part': part, 'title': title, 'model': model.name if model else '', 'model_uuid': model.uuid if model else '',
            'source': SOURCE_LABEL, 'source_urls': ['https://lceda.cn/', 'https://easyeda.com/'],
            'store_url': store_url, 'downloaded_at': time.strftime('%Y-%m-%dT%H:%M:%S%z'),
            'upstream': UPSTREAM_URL, 'upstream_version': '1.0.1',
            'files': [Path(x).name for x in result.files],
            'library_notice': data.get('packageDetail', {}).get('dataStr', {}).get('head', {}).get('licence', ''),
            'symbol_library_notice': data.get('dataStr', {}).get('head', {}).get('licence', ''),
        }
        if library_formats:
            metadata['altium'] = {'converter': CONVERTER_URL, 'commit': CONVERTER_COMMIT, 'exports': library_details}
        try:
            atomic_write(folder / 'model-info.json', json.dumps(metadata, ensure_ascii=False, indent=2).encode('utf-8'), overwrite=True)
        except OSError as exc:
            result.status = '部分完成'
            result.message += f'；来源信息保存失败：{exc}'
    else:
        if not library_formats and model is None and not model_error:
            result.status = '无模型'
        result.message = '；'.join(errors) or '没有可保存的文件'
    return result
