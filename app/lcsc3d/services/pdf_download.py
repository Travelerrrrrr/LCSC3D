"""Download only datasheets advertised by the domestic LCSC storefront."""
from concurrent.futures import Future, TimeoutError as FutureTimeout
from html.parser import HTMLParser
from pathlib import Path
import re
import threading
from urllib.parse import urljoin, urlsplit

from lcsc3d.core.app_logging import log_event, record_error, safe_part
from lcsc3d.core.errors import Cancelled, DownloadError
from lcsc3d.core.i18n import message
from lcsc3d.services.output_transaction import output_change


MAX_PDF_BYTES = 64 * 1024 * 1024
MISSING_REPORT = '缺少pdf元件型号.txt'


class PDFLinks(HTMLParser):
    def __init__(self, base):
        super().__init__()
        self.base, self.links = base, []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        value = attrs.get({'a': 'href', 'iframe': 'src', 'embed': 'src', 'object': 'data'}.get(tag, ''))
        if not value:
            return
        url = urljoin(self.base, value)
        parsed = urlsplit(url)
        if (parsed.scheme in ('http', 'https') and parsed.hostname and not parsed.username and not parsed.password
                and (parsed.path.lower().endswith('.pdf') or attrs.get('type') == 'application/pdf')):
            self.links.append(url)


def validate_pdf(payload):
    if (not re.match(rb'%PDF-[12]\.\d', payload[:1024].lstrip(b'\xef\xbb\xbf\x00\t\r\n '))
            or not re.search(rb'startxref\s+\d+\s+%%EOF\s*$', payload[-2048:])):
        raise DownloadError('数据手册不是完整的 PDF 文件，已停止保存')


class DatasheetBatch:
    """Bounded per-batch cache; concurrent parts share each URL request."""
    def __init__(self, cancelled):
        self.cancelled = cancelled
        self.cache = {}
        self.lock = threading.Lock()
        self.cache_bytes = 0

    def __enter__(self):
        return self

    def __exit__(self, *exception):
        self.cache.clear()

    def check(self):
        if self.cancelled.is_set():
            raise Cancelled()

    def product(self, part):
        from lcsc3d.services.store import StoreClient
        # A fresh memory-only client never loads a saved account/session.
        return StoreClient().datasheet_product(part, self.cancelled)

    def fetch(self, url, on_bytes):
        from lcsc3d.services.store import MemorySession, public_url
        session = MemorySession()
        visited = set()
        for _ in range(3):
            self.check()
            url = public_url(url)
            if not url or url in visited:
                raise DownloadError('数据手册链接无效或循环跳转')
            visited.add(url)
            payload, final = session.request(url, cancelled=self.cancelled, limit=MAX_PDF_BYTES,
                headers={'Accept': 'application/pdf,text/html;q=0.8,*/*;q=0.5'}, on_bytes=on_bytes)
            if not public_url(final):
                raise DownloadError('数据手册跳转地址无效')
            if b'%PDF-' in payload[:1024]:
                validate_pdf(payload)
                return payload
            # Follow an explicit PDF in a storefront-provided viewer page, never search the web.
            parser = PDFLinks(final)
            parser.feed(payload[:2 * 1024 * 1024].decode('utf-8', errors='replace'))
            links = list(dict.fromkeys(parser.links))
            if len(links) != 1:
                raise DownloadError('数据手册链接返回网页，未找到唯一的 PDF 下载地址')
            url = links[0]
        raise DownloadError('数据手册展示页跳转过多')

    def payload(self, url, on_bytes):
        with self.lock:
            future = self.cache.get(url)
            owner = future is None
            if owner:
                future = self.cache[url] = Future()
        if owner:
            try:
                data = self.fetch(url, on_bytes)
                self.check()
                validate_pdf(data)
                with self.lock:
                    future.set_result(data)
                    self.cache_bytes += len(data)
                    for key, item in list(self.cache.items()):
                        if self.cache_bytes <= 32 * 1024 * 1024 and len(self.cache) <= 64:
                            break
                        if item.done():
                            if item.exception() is None:
                                self.cache_bytes -= len(item.result())
                            self.cache.pop(key)
            except Exception as exc:
                with self.lock:
                    future.set_exception(exc)
                    # Failed entries have no reusable bytes; waiters still hold their future.
                    self.cache.pop(url, None)
        while True:
            self.check()
            try:
                return future.result(timeout=.1)
            except FutureTimeout:
                if future.done():
                    raise
                continue

    def download(self, part, root, notify=lambda *a: None, advance=lambda value: None):
        from lcsc3d.services.backend import Result, safe_filename, atomic_write
        result = Result(part, '失败', pdf_status='failed')
        try:
            self.check()
            notify('查询 PDF 数据手册', '')
            product = self.product(part)
            result.title = product.get('title', '') or part
            result.pdf_model = result.title
            result.brand = product.get('manufacturer', '')
            links = list(dict.fromkeys(product.get('datasheets', [])))
            advance(.08)
            if not links:
                result.status, result.pdf_status = '无数据手册', 'missing'
                result.message = '立创商城未提供 PDF 数据手册'
                log_event('WARNING', 'download.pdf_missing', part=safe_part(part), format='PDF')
                return result
            folder = Path(root) / 'PDF'
            result.folder = str(folder)
            stem = safe_filename(part + '_' + result.title) if result.title != part else part
            errors = []
            for index, url in enumerate(links):
                self.check()
                def progress(done, total):
                    self.check()
                    notify(message('下载 PDF：{0:.1f} MiB', done / 1048576) if not total else
                           message('下载 PDF：{0:.1f} / {1:.1f} MiB', done / 1048576, total / 1048576), result.title)
                    advance(.08 + .72 * (index + (min(done / total, 1) if total else 0)) / len(links))
                try:
                    payload = self.payload(url, progress)
                    self.check()
                    suffix = f'_{index + 1:02d}' if len(links) > 1 else ''
                    path = folder / (stem + suffix + '.pdf')
                    atomic_write(path, payload)
                    result.files.append(str(path))
                    log_event('INFO', 'download.pdf_saved', part=safe_part(part), format='PDF', bytes=len(payload))
                except Cancelled:
                    raise
                except Exception as exc:
                    record_error(exc, 'download.pdf_failed', part=safe_part(part), format='PDF', stage='download')
                    errors.append(str(exc))
            result.pdf_status = 'failed' if errors else 'success'
            result.status = '部分完成' if errors and result.files else '失败' if errors else '成功'
            result.completed_formats = [] if errors else ['PDF']
            result.export_errors = ['PDF：' + error for error in errors]
            result.message = '；'.join(([f'已保存 PDF {len(result.files)} 份'] if result.files else []) + result.export_errors)
            return result
        except Cancelled:
            raise
        except Exception as exc:
            record_error(exc, 'download.pdf_failed', part=safe_part(part), format='PDF', stage='lookup')
            result.message = 'PDF：' + str(exc)
            result.export_errors = [result.message]
            return result


def write_missing_report(results, root):
    from lcsc3d.services.backend import atomic_write
    path = Path(root) / 'PDF' / MISSING_REPORT
    missing = [row for row in results if row.pdf_status == 'missing']
    if missing:
        def clean(value):
            return ' '.join(str(value or '未提供').split())
        # Keep input order and deduplicate the requested model/brand pairs.
        rows = dict.fromkeys((clean(row.pdf_model or row.title or row.part), clean(row.brand)) for row in missing)
        body = '元件型号\t品牌\n' + ''.join(model + '\t' + brand + '\n' for model, brand in rows)
        atomic_write(path, body.encode('utf-8-sig'))
        for row in missing:
            row.pdf_report = str(path)
    elif path.exists():
        with output_change(path, None):
            path.unlink()
    log_event('INFO', 'download.pdf_report_written', count=len(missing), format='TXT')
