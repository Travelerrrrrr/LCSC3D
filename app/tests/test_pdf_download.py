"""Datasheet sources, mixed exports, missing reports and batch cancellation."""
import copy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lcsc3d.services import backend
from lcsc3d.services.pdf_download import DatasheetBatch, MISSING_REPORT, validate_pdf
from lcsc3d.services.store import StoreClient, StoreError, datasheet_urls
from lcsc3d.core.app_logging import configure_logging, close_logging, get_log_directory
from test_backend import STEP, OBJ


def pdf_bytes(label='test'):
    stream = b'BT /F1 12 Tf 20 20 Td (' + label.encode('ascii') + b') Tj ET'
    objects = [b'<< /Type /Catalog /Pages 2 0 R >>', b'<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
               b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] /Contents 4 0 R >>',
               b'<< /Length ' + str(len(stream)).encode() + b' >>\nstream\n' + stream + b'\nendstream']
    data, offsets = b'%PDF-1.4\n', [0]
    for index, value in enumerate(objects, 1):
        offsets.append(len(data))
        data += str(index).encode() + b' 0 obj\n' + value + b'\nendobj\n'
    xref = len(data)
    data += b'xref\n0 5\n0000000000 65535 f \n'
    data += b''.join(f'{offset:010d} 00000 n \n'.encode() for offset in offsets[1:])
    return data + b'trailer\n<< /Size 5 /Root 1 0 R >>\nstartxref\n' + str(xref).encode() + b'\n%%EOF\n'


def page(record):
    return ('<script id="__NEXT_DATA__" type="application/json">' + json.dumps(
        {'props': {'pageProps': {'webData': {'productRecord': record}}}}) + '</script>').encode()


class PDFTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        env = patch.dict(os.environ, LOCALAPPDATA=str(self.root / 'profile'))
        env.start(); self.addCleanup(env.stop)
        configure_logging('DEBUG', version='test'); self.addCleanup(close_logging)
        self.stop = threading.Event()
        self.api = backend.NetworkApi(self.stop, use_cache=False)
        self.api.get_cad_data_of_component = lambda part: json.loads(
            (Path(__file__).parent / 'fixtures' / (part + '.json')).read_text('utf-8'))
        self.api.get_step_3d_model = lambda uuid: STEP
        self.api.get_raw_3d_model_obj = lambda uuid: OBJ
        self.products = {part: dict(part=part, title='Model-' + part, manufacturer='测试品牌',
                                   datasheets=['https://atta.szlcsc.com/' + part + '.pdf'])
                         for part in ('C2040', 'C20197', 'C163691')}
        lookup = patch.object(DatasheetBatch, 'product', side_effect=lambda part: copy.deepcopy(self.products[part]))
        self.lookup = lookup.start(); self.addCleanup(lookup.stop)
        fetch = patch.object(DatasheetBatch, 'fetch', side_effect=self.fetch)
        self.fetch_mock = fetch.start(); self.addCleanup(fetch.stop)

    def fetch(self, url, progress):
        data = pdf_bytes()
        progress(len(data), len(data))
        return data

    def run_batch(self, formats=('PDF',), **options):
        return backend.download_batch(['C2040', 'C20197'], backend.Options(self.root/'exports', formats,
            bind_3d_models=False, **options), self.stop, api=self.api)

    def report(self):
        return self.root / 'exports/PDF' / MISSING_REPORT

    def test_pdf_only_avoids_eda_and_writes_original_bytes_with_stable_names(self):
        self.api.get_cad_data_of_component = lambda part: self.fail('PDF-only queried EDA')
        rows = self.run_batch(append_mode=True, project_path='invalid', ad_3d_layer=999)
        self.assertEqual([row.status for row in rows], ['成功', '成功'])
        for row in rows:
            path = self.root / 'exports/PDF' / f'{row.part}_Model-{row.part}.pdf'
            self.assertEqual(path.read_bytes(), pdf_bytes())
            self.assertEqual(row.files, [str(path)])
        self.assertFalse(self.report().exists())

    def test_missing_report_contains_only_model_and_brand_and_never_searches_other_sources(self):
        self.products['C20197'].update(title='芯片\t型号\nB', manufacturer='品牌\r\nA', datasheets=[])
        rows = self.run_batch()
        self.assertEqual([row.status for row in rows], ['成功', '无数据手册'])
        self.assertEqual(self.report().read_text('utf-8-sig'), '元件型号\t品牌\n芯片 型号 B\t品牌 A\n')
        self.assertEqual(rows[1].pdf_report, str(self.report()))
        self.assertEqual(self.fetch_mock.call_count, 1)

    def test_shared_url_is_fetched_once_and_keeps_each_parts_filename(self):
        self.products['C20197']['datasheets'] = self.products['C2040']['datasheets'][:]
        rows = self.run_batch()
        self.assertTrue(all(row.status == '成功' for row in rows))
        self.assertEqual(self.fetch_mock.call_count, 1)
        self.assertNotEqual(rows[0].files, rows[1].files)

    def test_multiple_storefront_datasheets_are_numbered_and_not_combined(self):
        self.products['C2040']['datasheets'].append('https://atta.szlcsc.com/second.pdf')
        rows = self.run_batch()
        self.assertEqual([Path(p).name for p in rows[0].files], ['C2040_Model-C2040_01.pdf','C2040_Model-C2040_02.pdf'])

    def test_lookup_failure_is_not_misreported_as_missing(self):
        self.lookup.side_effect = StoreError('request failed')
        rows = self.run_batch()
        self.assertTrue(all(row.status == '失败' and row.pdf_status == 'failed' for row in rows))
        self.assertFalse(self.report().exists())

    def test_invalid_or_truncated_pdf_never_replaces_existing_file(self):
        path = self.root / 'exports/PDF/C2040_Model-C2040.pdf'
        path.parent.mkdir(parents=True)
        path.write_bytes(pdf_bytes('previous'))
        for payload in (b'<html>login</html>', pdf_bytes()[:-10]):
            with self.subTest(payload=payload[:10]):
                self.fetch_mock.side_effect = lambda url, progress: payload
                rows = self.run_batch()
                self.assertTrue(all(row.status == '失败' for row in rows))
                self.assertEqual(path.read_bytes(), pdf_bytes('previous'))
                self.assertFalse(self.report().exists())

    def test_mixed_exports_keep_pdf_independent_of_model_and_library_availability(self):
        for formats, options in ((('PDF','STEP','OBJ'), {}), (('PDF','SCHLIB','PCBLIB'), {}),
                                (('PDF','SCHLIB','PCBLIB'), dict(merge_schlib=True,merge_pcblib=True)),
                                (('PDF','KICAD_SYMBOL','KICAD_FOOTPRINT'), dict(kicad_merge=True))):
            with self.subTest(formats=formats, options=options):
                self.products['C20197']['datasheets'] = []
                rows = self.run_batch(formats, **options)
                self.assertEqual([row.status for row in rows], ['成功','部分完成'], [r.message for r in rows])
                self.assertTrue(any(Path(p).suffix != '.pdf' for p in rows[1].files))
                self.assertEqual(rows[1].pdf_status, 'missing')
                self.assertIn('Model-C20197', self.report().read_text('utf-8-sig'))

    def test_eda_failure_does_not_prevent_pdf_download(self):
        self.api.get_cad_data_of_component = lambda part: (_ for _ in ()).throw(backend.DownloadError('no EDA'))
        rows = self.run_batch(('PDF','STEP'))
        self.assertTrue(all(row.status == '部分完成' and len(row.files)==1 for row in rows))

    def test_download_failure_does_not_prevent_eda_exports_or_enter_missing_report(self):
        self.fetch_mock.side_effect = StoreError('HTTP 404')
        rows = self.run_batch(('PDF','SCHLIB'))
        self.assertTrue(all(row.status == '部分完成' for row in rows))
        self.assertFalse(self.report().exists())

    def test_report_refresh_removes_stale_file_only_for_pdf_batches(self):
        self.products['C20197']['datasheets'] = []
        self.run_batch()
        before = self.report().read_bytes()
        self.run_batch(('STEP',))
        self.assertEqual(self.report().read_bytes(), before)
        self.products['C20197']['datasheets'] = ['https://atta.szlcsc.com/C20197.pdf']
        self.run_batch()
        self.assertFalse(self.report().exists())

    def test_cancellation_after_report_write_restores_pdf_and_previous_report(self):
        self.report().parent.mkdir(parents=True)
        self.report().write_bytes(b'previous report')
        path = self.report().parent / 'C2040_Model-C2040.pdf'
        path.write_bytes(pdf_bytes('previous'))
        self.products['C20197']['datasheets'] = []
        original = backend.atomic_write
        def write(path, data):
            original(path, data)
            if path.name == MISSING_REPORT:
                self.stop.set()
        with patch.object(backend, 'atomic_write', side_effect=write):
            rows = self.run_batch()
        self.assertTrue(all(row.status == '已取消' and not row.files and not row.pdf_status for row in rows))
        self.assertEqual(path.read_bytes(), pdf_bytes('previous'))
        self.assertEqual(self.report().read_bytes(), b'previous report')

    def test_report_write_failure_is_visible_and_logged(self):
        self.products['C20197']['datasheets'] = []
        original = backend.atomic_write
        def write(path, data):
            if path.name == MISSING_REPORT:
                raise PermissionError('PRIVATE report path')
            original(path, data)
        with patch.object(backend, 'atomic_write', side_effect=write):
            rows = self.run_batch()
        self.assertEqual([row.status for row in rows], ['部分完成','失败'])
        events = (get_log_directory() / 'LCSC3D.log').read_text('utf-8')
        self.assertIn('download.pdf_report_failed', events)
        self.assertNotIn('PRIVATE report path', events)

    def test_timeout_is_reported_without_waiting_forever_for_cached_future(self):
        self.fetch_mock.side_effect = TimeoutError('timed out')
        rows = self.run_batch()
        self.assertTrue(all(row.status=='失败' for row in rows))

    def test_cancel_after_removing_stale_report_restores_it_and_removes_new_pdfs(self):
        self.report().parent.mkdir(parents=True)
        self.report().write_bytes(b'old report')
        def completed(index, result):
            if not self.report().exists():
                self.stop.set()
        rows = backend.download_batch(['C2040','C20197'],backend.Options(self.root/'exports',('PDF',)),
            self.stop,api=self.api,on_result=completed)
        self.assertTrue(all(row.status=='已取消' for row in rows))
        self.assertEqual(self.report().read_bytes(),b'old report')
        self.assertEqual(list(self.report().parent.glob('*.pdf')),[])

    def test_partial_multiple_pdf_failure_retains_success_and_does_not_report_missing(self):
        self.products['C2040']['datasheets'].append('https://example.com/broken.pdf')
        def fetch(url, progress):
            if 'broken' in url:raise StoreError('HTTP 404')
            return pdf_bytes()
        self.fetch_mock.side_effect=fetch
        rows=self.run_batch()
        self.assertEqual(rows[0].status,'部分完成')
        self.assertEqual(len(rows[0].files),1)
        self.assertFalse(self.report().exists())

    def test_names_with_invalid_characters_and_long_unicode_stay_inside_pdf_folder(self):
        self.products['C2040']['title']='../中文:型号?/'+'😀'*150
        rows=self.run_batch()
        self.assertEqual(rows[0].status,'成功',rows[0].message)
        path=Path(rows[0].files[0])
        self.assertEqual(path.parent,self.root/'exports/PDF')
        self.assertLess(len(path.name.encode('utf-16-le'))//2,256)

    def test_empty_format_selection_still_returns_a_failure_result(self):
        rows=self.run_batch(())
        self.assertTrue(all(row.status=='失败' for row in rows))

    def test_official_source_never_uses_eda_fallback_and_excludes_certificates(self):
        client = StoreClient()
        record = dict(productCode='C2040', productId='1', productModel='MODEL', brandName='BRAND',
                      fileTypeVOList=[dict(fileType='certification_data_property',detailVOList=[dict(fileUrl='https://example.com/rohs.pdf')])])
        with patch.object(client, 'search_page', return_value={'items':[dict(part='C2040',product_id='1')]}), \
                patch.object(client.session, 'request', return_value=(page(record), 'https://item.szlcsc.com/1.html')), \
                patch.object(client, '_catalog_search') as eda:
            product = client.datasheet_product('C2040')
        eda.assert_not_called()
        self.assertEqual(product['datasheets'], [])
        self.assertEqual((product['title'],product['manufacturer']), ('MODEL','BRAND'))

    def test_official_multiple_links_are_deduplicated(self):
        data = {'productRecord': {'fileTypeVOList': [{'fileType':'pdf_property', 'detailVOList':[
            {'fileUrl':'/upload/public/pdf/a.pdf'}, {'linkAddress':'https://atta.szlcsc.com/upload/public/pdf/a.pdf'},
            {'linkAddress':'https://vendor.example/datasheet.pdf'}]}]}}
        self.assertEqual(datasheet_urls(data), ['https://atta.szlcsc.com/upload/public/pdf/a.pdf','https://vendor.example/datasheet.pdf'])


class PDFHTTPTests(unittest.TestCase):
    def test_real_http_redirect_viewer_progress_and_non_pdf_rejection(self):
        data = pdf_bytes()
        calls = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_GET(self):
                calls.append(self.path)
                if self.path == '/redirect':
                    self.send_response(302); self.send_header('Location','/viewer'); self.end_headers(); return
                payload = b'<embed src="/data.pdf" type="application/pdf">' if self.path == '/viewer' else data if self.path == '/data.pdf' else b'<html>no document</html>'
                self.send_response(200); self.send_header('Content-Length',str(len(payload))); self.end_headers(); self.wfile.write(payload)
        server = ThreadingHTTPServer(('127.0.0.1',0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        try:
            with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ,LOCALAPPDATA=directory):
                configure_logging('DEBUG', version='test')
                try:
                    with DatasheetBatch(threading.Event()) as batch, patch('lcsc3d.services.store.public_url', side_effect=lambda value:value):
                        samples=[]
                        base=f'http://127.0.0.1:{server.server_port}'
                        result=batch.payload(base+'/redirect', lambda done,total:samples.append((done,total)))
                        self.assertEqual(result,data)
                        self.assertIn((len(data),len(data)),samples)
                        with self.assertRaises(backend.DownloadError):
                            batch.payload(base+'/not-pdf', lambda *a:None)
                    self.assertEqual(calls[:3], ['/redirect','/viewer','/data.pdf'])
                finally: close_logging()
        finally:
            server.shutdown(); server.server_close(); thread.join(2)
